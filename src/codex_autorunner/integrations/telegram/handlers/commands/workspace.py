from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence

import httpx

from .....agents.opencode.runtime import extract_session_id
from .....core.logging_utils import log_event
from .....core.state import now_iso
from .....core.utils import canonicalize_path, resolve_opencode_binary
from .....manifest import load_manifest
from ....app_server import is_missing_thread_error
from ....app_server.client import CodexAppServerClient, CodexAppServerError
from ....chat.agents import (
    build_agent_switch_state,
    chat_agent_supports_effort,
    chat_hermes_profile_options,
    format_chat_agent_selection,
    normalize_hermes_profile,
    resolve_chat_agent_and_profile,
    resolve_chat_runtime_agent,
)
from ....chat.constants import (
    APP_SERVER_UNAVAILABLE_MESSAGE,
    TOPIC_NOT_BOUND_MESSAGE,
    TOPIC_NOT_BOUND_RESUME_MESSAGE,
)
from ....chat.thread_summaries import _format_resume_timestamp
from ...adapter import (
    TelegramCallbackQuery,
    TelegramMessage,
)
from ...config import AppServerUnavailableError
from ...constants import (
    DEFAULT_AGENT,
    DEFAULT_PAGE_SIZE,
    MAX_TOPIC_THREAD_HISTORY,
    RESUME_MISSING_IDS_LOG_LIMIT,
    RESUME_PICKER_PROMPT,
    RESUME_REFRESH_LIMIT,
    THREAD_LIST_MAX_PAGES,
)
from ...helpers import (
    _coerce_thread_list,
    _extract_first_user_preview,
    _extract_thread_id,
    _extract_thread_info,
    _extract_thread_list_cursor,
    _extract_thread_preview_parts,
    _format_missing_thread_label,
    _format_resume_summary,
    _format_thread_preview,
    _local_workspace_threads,
    _page_slice,
    _partition_threads,
    _paths_compatible,
    _resume_thread_list_limit,
    _set_thread_summary,
    _split_topic_key,
    _thread_summary_preview,
    _with_conversation_id,
)
from ...state import APPROVAL_MODE_YOLO, normalize_agent
from ...types import SelectionState
from .agent_model_utils import (
    _extract_opencode_session_path,
    _handle_agent_command,
    _model_list_all_with_agent_compat,
)
from .agent_model_utils import (
    _send_agent_profile_picker as _send_telegram_agent_profile_picker,
)
from .shared import TelegramCommandSupportMixin
from .workspace_binding import WorkspaceBindingMixin
from .workspace_resume import WorkspaceResumeMixin
from .workspace_session_commands import WorkspaceSessionCommandsMixin
from .workspace_status import WorkspaceStatusMixin

if TYPE_CHECKING:
    from ...state import TelegramTopicRecord


@dataclass
class ResumeCommandArgs:
    """Parsed /resume command options."""

    trimmed: str
    remaining: list[str]
    show_unscoped: bool
    refresh: bool


@dataclass
class ResumeThreadData:
    """Thread listing details used to render the resume picker."""

    candidates: list[dict[str, Any]]
    entries_by_id: dict[str, dict[str, Any]]
    local_thread_ids: list[str]
    local_previews: dict[str, str]
    local_thread_topics: dict[str, set[str]]
    list_failed: bool
    threads: list[dict[str, Any]]
    unscoped_entries: list[dict[str, Any]]
    saw_path: bool


class WorkspaceCommands(
    TelegramCommandSupportMixin,
    WorkspaceBindingMixin,
    WorkspaceSessionCommandsMixin,
    WorkspaceResumeMixin,
    WorkspaceStatusMixin,
):
    def _process_monitor_root(
        self,
        record: Optional["TelegramTopicRecord"],
        *,
        allow_fallback: bool = False,
    ) -> Optional[Path]:
        if record is not None and getattr(record, "pma_enabled", False):
            hub_root = getattr(self, "_hub_root", None)
            if hub_root is not None:
                return Path(hub_root)
        if record is not None and record.workspace_path:
            return Path(record.workspace_path)
        if allow_fallback:
            config_root = getattr(getattr(self, "_config", None), "root", None)
            if config_root is not None:
                return Path(config_root)
        return None

    def _resolve_workspace_path(
        self,
        record: Optional["TelegramTopicRecord"],
        *,
        allow_pma: bool = False,
    ) -> tuple[Optional[str], Optional[str]]:
        if record and record.workspace_path:
            return record.workspace_path, None
        if allow_pma and record and record.pma_enabled:
            hub_root = getattr(self, "_hub_root", None)
            if hub_root is None:
                return None, "PMA unavailable; hub root not configured."
            return str(hub_root), None
        return None, TOPIC_NOT_BOUND_MESSAGE

    def _record_with_workspace_path(
        self,
        record: Optional["TelegramTopicRecord"],
        workspace_path: Optional[str],
    ) -> Optional["TelegramTopicRecord"]:
        if record is None or not workspace_path:
            return record
        if record.workspace_path == workspace_path:
            return record
        return dataclasses.replace(record, workspace_path=workspace_path)

    async def _apply_agent_change(
        self,
        chat_id: int,
        thread_id: Optional[int],
        desired: str,
        *,
        profile: object = None,
    ) -> str:
        switch_state = build_agent_switch_state(
            desired,
            profile,
            model_reset="agent_default",
            context=self,
        )

        def apply(record: "TelegramTopicRecord") -> None:
            record.agent = switch_state.agent
            record.agent_profile = switch_state.profile
            record.active_thread_id = None
            record.thread_ids.clear()
            record.thread_summaries.clear()
            record.pending_compact_seed = None
            record.pending_compact_seed_thread_id = None
            record.effort = switch_state.effort
            record.model = switch_state.model

        await self._router.update_topic(chat_id, thread_id, apply)
        if not self._agent_supports_resume(switch_state.agent):
            return " (resume not supported)"
        return ""

    async def _handle_agent(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        await _handle_agent_command(self, message, args)

    async def _send_agent_profile_picker(self, **kwargs: Any) -> None:
        await _send_telegram_agent_profile_picker(self, **kwargs)

    def _effective_policies(
        self, record: "TelegramTopicRecord"
    ) -> tuple[Optional[str], Optional[Any]]:
        approval_policy, sandbox_policy = self._config.defaults.policies_for_mode(
            record.approval_mode
        )
        if record.approval_policy is not None:
            approval_policy = record.approval_policy
        if record.sandbox_policy is not None:
            sandbox_policy = record.sandbox_policy
        return approval_policy, sandbox_policy

    def _effective_agent(self, record: Optional["TelegramTopicRecord"]) -> str:
        agent, _profile = self._effective_agent_state(record)
        return agent

    def _effective_agent_profile(
        self, record: Optional["TelegramTopicRecord"]
    ) -> Optional[str]:
        _agent, profile = self._effective_agent_state(record)
        return profile

    def _effective_agent_state(
        self, record: Optional["TelegramTopicRecord"]
    ) -> tuple[str, Optional[str]]:
        if record:
            return resolve_chat_agent_and_profile(
                record.agent,
                record.agent_profile,
                default=DEFAULT_AGENT,
                context=self,
            )
        return DEFAULT_AGENT, None

    def _effective_runtime_agent(self, record: Optional["TelegramTopicRecord"]) -> str:
        agent, profile = self._effective_agent_state(record)
        return resolve_chat_runtime_agent(
            agent,
            profile,
            default=DEFAULT_AGENT,
            context=self,
        )

    def _effective_agent_label(self, record: Optional["TelegramTopicRecord"]) -> str:
        agent, profile = self._effective_agent_state(record)
        return self._effective_agent_label_from_values(agent, profile)

    def _effective_agent_label_from_values(
        self,
        agent: str,
        profile: Optional[str],
    ) -> str:
        return format_chat_agent_selection(agent, profile)

    def _thread_start_kwargs(
        self,
        record: Optional["TelegramTopicRecord"] = None,
        *,
        agent: object = None,
        profile: object = None,
    ) -> dict[str, Any]:
        if record is not None:
            resolved_agent, resolved_profile = self._effective_agent_state(record)
        else:
            resolved_agent, resolved_profile = resolve_chat_agent_and_profile(
                agent,
                profile,
                default=DEFAULT_AGENT,
                context=self,
            )
        kwargs: dict[str, Any] = {"agent": resolved_agent}
        if resolved_profile is not None:
            kwargs["profile"] = resolved_profile
        return kwargs

    def _hermes_profile_options(self) -> tuple[Any, ...]:
        return chat_hermes_profile_options(self)

    def _normalize_hermes_profile(self, value: object) -> Optional[str]:
        return normalize_hermes_profile(value, context=self)

    def _agent_supports_effort(self, agent: str) -> bool:
        return chat_agent_supports_effort(agent, self)

    def _agent_supports_resume(self, agent: str) -> bool:
        return self._agent_supports_capability(agent, "durable_threads")

    def _agent_rate_limit_source(self, agent: str) -> Optional[str]:
        if agent == "codex":
            return "app_server"
        return None

    def _opencode_available(self) -> bool:
        opencode_command = self._config.opencode_command
        if opencode_command and resolve_opencode_binary(opencode_command[0]):
            return True
        binary = self._config.agent_binaries.get("opencode")
        if not binary:
            return False
        return resolve_opencode_binary(binary) is not None

    async def _fetch_model_list(
        self,
        record: Optional["TelegramTopicRecord"],
        *,
        agent: str,
        client: CodexAppServerClient,
        list_params: dict[str, Any],
    ) -> Any:
        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                from .....agents.opencode.supervisor import OpenCodeSupervisorError

                raise OpenCodeSupervisorError("OpenCode backend is not configured")
            workspace_root = self._canonical_workspace_root(
                record.workspace_path if record else None
            )
            if workspace_root is None:
                from .....agents.opencode.supervisor import OpenCodeSupervisorError

                raise OpenCodeSupervisorError("OpenCode workspace is unavailable")
            from .....agents.opencode.harness import OpenCodeHarness

            harness = OpenCodeHarness(supervisor)
            catalog = await harness.model_catalog(workspace_root)
            return [
                {
                    "id": model.id,
                    "displayName": model.display_name,
                }
                for model in catalog.models
            ]
        requested_agent = list_params.get("agent")
        if not isinstance(requested_agent, str) or not requested_agent:
            requested_agent = agent
        request_params = dict(list_params)
        request_params["agent"] = requested_agent
        return await _model_list_all_with_agent_compat(client, params=request_params)

    async def _verify_active_thread(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional["TelegramTopicRecord"]:
        agent = self._effective_agent(record)
        if agent == "opencode":
            if not record.active_thread_id:
                return record
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return await self._router.set_active_thread(
                    message.chat_id, message.thread_id, None
                )
            workspace_root = self._canonical_workspace_root(record.workspace_path)
            if workspace_root is None:
                return record
            try:
                client = await supervisor.get_client(workspace_root)
                await client.get_session(record.active_thread_id)
                return record
            except (OSError, RuntimeError, ValueError):
                return await self._router.set_active_thread(
                    message.chat_id, message.thread_id, None
                )
        if not self._agent_supports_resume(agent):
            return record
        thread_id = record.active_thread_id
        if not thread_id:
            return record
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                APP_SERVER_UNAVAILABLE_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if client is None:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        try:
            result = await client.thread_resume(thread_id)
        except (OSError, RuntimeError, ValueError, CodexAppServerError) as exc:
            if is_missing_thread_error(exc):
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.thread.verify_missing",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                )
                return await self._router.set_active_thread(
                    message.chat_id, message.thread_id, None
                )
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.thread.verify_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to verify the active thread; use /resume or /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        info = _extract_thread_info(result)
        resumed_path = info.get("workspace_path")
        if not isinstance(resumed_path, str):
            await self._send_message(
                message.chat_id,
                "Active thread missing workspace metadata; refusing to continue. "
                "Fix the app-server workspace reporting and try /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return await self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
        try:
            workspace_root = Path(record.workspace_path or "").expanduser().resolve()
            resumed_root = Path(resumed_path).expanduser().resolve()
        except OSError:
            await self._send_message(
                message.chat_id,
                "Active thread has invalid workspace metadata; refusing to continue. "
                "Fix the app-server workspace reporting and try /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return await self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
        if not _paths_compatible(workspace_root, resumed_root):
            log_event(
                self._logger,
                logging.INFO,
                "telegram.thread.workspace_mismatch",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                workspace_path=str(workspace_root),
                resumed_path=str(resumed_root),
            )
            await self._send_message(
                message.chat_id,
                "Active thread belongs to a different workspace; refusing to continue. "
                "Fix the app-server workspace reporting and try /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return await self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
        return await self._apply_thread_result(
            message.chat_id, message.thread_id, result, active_thread_id=thread_id
        )

    async def _find_thread_conflict(self, thread_id: str, *, key: str) -> Optional[str]:
        return await self._store.find_active_thread(thread_id, exclude_key=key)

    async def _handle_thread_conflict(
        self,
        message: TelegramMessage,
        thread_id: str,
        conflict_key: str,
    ) -> None:
        log_event(
            self._logger,
            logging.WARNING,
            "telegram.thread.conflict",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=thread_id,
            conflict_topic=conflict_key,
        )
        await self._send_message(
            message.chat_id,
            "That Codex thread is already active in another topic. "
            "Use /new here or continue in the other topic.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _apply_thread_result(
        self,
        chat_id: int,
        thread_id: Optional[int],
        result: Any,
        *,
        active_thread_id: Optional[str] = None,
        overwrite_defaults: bool = False,
        sync_binding: bool = True,
    ) -> "TelegramTopicRecord":
        info = _extract_thread_info(result)
        if active_thread_id is None:
            active_thread_id = info.get("thread_id")
        user_preview, assistant_preview = _extract_thread_preview_parts(result)
        last_used_at = now_iso()

        def apply(record: "TelegramTopicRecord") -> None:
            if active_thread_id:
                record.active_thread_id = active_thread_id
                if active_thread_id in record.thread_ids:
                    record.thread_ids.remove(active_thread_id)
                record.thread_ids.insert(0, active_thread_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
                _set_thread_summary(
                    record,
                    active_thread_id,
                    user_preview=user_preview,
                    assistant_preview=assistant_preview,
                    last_used_at=last_used_at,
                    workspace_path=info.get("workspace_path"),
                    rollout_path=info.get("rollout_path"),
                )
            incoming_workspace = info.get("workspace_path")
            if isinstance(incoming_workspace, str) and incoming_workspace:
                if record.workspace_path:
                    try:
                        current_root = canonicalize_path(Path(record.workspace_path))
                        incoming_root = canonicalize_path(Path(incoming_workspace))
                    except OSError:
                        current_root = None
                        incoming_root = None
                    if (
                        current_root is None
                        or incoming_root is None
                        or not _paths_compatible(current_root, incoming_root)
                    ):
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.workspace.mismatch",
                            workspace_path=record.workspace_path,
                            incoming_workspace_path=incoming_workspace,
                        )
                    else:
                        record.workspace_path = incoming_workspace
                else:
                    record.workspace_path = incoming_workspace
                record.workspace_id = self._workspace_id_for_path(record.workspace_path)
            if info.get("rollout_path"):
                record.rollout_path = info["rollout_path"]
            if info.get("agent") and (overwrite_defaults or record.agent is None):
                normalized_agent = normalize_agent(info.get("agent"), context=self)
                if normalized_agent:
                    record.agent = normalized_agent
            if info.get("model") and (overwrite_defaults or record.model is None):
                record.model = info["model"]
            if info.get("effort") and (overwrite_defaults or record.effort is None):
                record.effort = info["effort"]
            if info.get("summary") and (overwrite_defaults or record.summary is None):
                record.summary = info["summary"]
            allow_thread_policies = record.approval_mode != APPROVAL_MODE_YOLO
            if (
                allow_thread_policies
                and info.get("approval_policy")
                and (overwrite_defaults or record.approval_policy is None)
            ):
                record.approval_policy = info["approval_policy"]
            if (
                allow_thread_policies
                and info.get("sandbox_policy")
                and (overwrite_defaults or record.sandbox_policy is None)
            ):
                record.sandbox_policy = info["sandbox_policy"]

        updated = await self._router.update_topic(chat_id, thread_id, apply)
        if (
            sync_binding
            and updated is not None
            and not bool(getattr(updated, "pma_enabled", False))
            and isinstance(active_thread_id, str)
            and active_thread_id
            and isinstance(updated.workspace_path, str)
            and updated.workspace_path
        ):
            from .execution import _sync_telegram_thread_binding

            await _sync_telegram_thread_binding(
                self,
                surface_key=await self._resolve_topic_key(chat_id, thread_id),
                workspace_root=Path(updated.workspace_path),
                agent=self._effective_runtime_agent(updated),
                repo_id=(
                    updated.repo_id.strip()
                    if isinstance(updated.repo_id, str) and updated.repo_id.strip()
                    else None
                ),
                resource_kind=(
                    updated.resource_kind.strip()
                    if isinstance(updated.resource_kind, str)
                    and updated.resource_kind.strip()
                    else None
                ),
                resource_id=(
                    updated.resource_id.strip()
                    if isinstance(updated.resource_id, str)
                    and updated.resource_id.strip()
                    else None
                ),
                backend_thread_id=active_thread_id,
                mode="repo",
                pma_enabled=False,
            )
        return updated

    async def _require_bound_record(
        self,
        message: TelegramMessage,
        *,
        prompt: Optional[str] = None,
        allow_pma: bool = False,
    ) -> Optional["TelegramTopicRecord"]:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        if record is None:
            await self._send_message(
                message.chat_id,
                prompt or TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if record.workspace_path:
            await self._refresh_workspace_id(key, record)
            return record
        if allow_pma and record.pma_enabled:
            hub_root = getattr(self, "_hub_root", None)
            if hub_root is None:
                await self._send_message(
                    message.chat_id,
                    "PMA unavailable; hub root not configured.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
            return record
        if not record.workspace_path:
            await self._send_message(
                message.chat_id,
                prompt or TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        return record

    async def _ensure_thread_id(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional[str]:
        thread_id = record.active_thread_id
        if thread_id:
            key = await self._resolve_topic_key(message.chat_id, message.thread_id)
            conflict_key = await self._find_thread_conflict(thread_id, key=key)
            if conflict_key:
                await self._router.set_active_thread(
                    message.chat_id, message.thread_id, None
                )
                await self._handle_thread_conflict(message, thread_id, conflict_key)
                return None
            verified = await self._verify_active_thread(message, record)
            if not verified:
                return None
            record = verified
            thread_id = record.active_thread_id
            if thread_id:
                return thread_id
        agent = self._effective_agent(record)
        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
            workspace_root = self._canonical_workspace_root(record.workspace_path)
            if workspace_root is None:
                await self._send_message(
                    message.chat_id,
                    "Workspace unavailable.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
            try:
                opencode_client = await supervisor.get_client(workspace_root)
                session = await opencode_client.create_session(
                    directory=str(workspace_root)
                )
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.opencode.session.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not session_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None

            def apply(record: "TelegramTopicRecord") -> None:
                record.active_thread_id = session_id
                if session_id in record.thread_ids:
                    record.thread_ids.remove(session_id)
                record.thread_ids.insert(0, session_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
                _set_thread_summary(
                    record,
                    session_id,
                    last_used_at=now_iso(),
                    workspace_path=record.workspace_path,
                    rollout_path=record.rollout_path,
                )

            await self._router.update_topic(message.chat_id, message.thread_id, apply)
            return session_id
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                APP_SERVER_UNAVAILABLE_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if client is None:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        thread = await client.thread_start(
            record.workspace_path or "",
            **self._thread_start_kwargs(record),
        )
        if not await self._require_thread_workspace(
            message, record.workspace_path, thread, action="thread_start"
        ):
            return None
        thread_id = _extract_thread_id(thread)
        if not thread_id:
            await self._send_message(
                message.chat_id,
                "Failed to start a new thread.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        await self._apply_thread_result(
            message.chat_id,
            message.thread_id,
            thread,
            active_thread_id=thread_id,
        )
        return thread_id

    def _list_manifest_repos(self) -> list[str]:
        if not self._manifest_path or not self._hub_root:
            return []
        try:
            manifest = load_manifest(self._manifest_path, self._hub_root)
        except (OSError, ValueError):
            return []
        repo_ids = [repo.id for repo in manifest.repos if repo.enabled]
        return repo_ids

    def _resolve_workspace(
        self, arg: str
    ) -> Optional[tuple[str, Optional[str], Optional[str], Optional[str]]]:
        arg = (arg or "").strip()
        if not arg:
            return None
        hub_client = getattr(self, "_hub_client", None)
        if hub_client is not None:
            try:
                from concurrent.futures import ThreadPoolExecutor

                from .....core.hub_control_plane import AgentWorkspaceListRequest

                request = AgentWorkspaceListRequest()

                def _fetch() -> Any:
                    return asyncio.run(hub_client.list_agent_workspaces(request))

                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(_fetch)
                    response = future.result(timeout=10)
                for descriptor in response.workspaces:
                    workspace_id = descriptor.workspace_id
                    workspace_path = descriptor.workspace_root
                    if not workspace_id or not workspace_path:
                        continue
                    if workspace_id != arg:
                        continue
                    return (
                        str(canonicalize_path(Path(workspace_path))),
                        None,
                        "agent_workspace",
                        workspace_id,
                    )
            except (OSError, ValueError, RuntimeError, Exception):
                self._logger.debug(
                    "resolve_workspace: hub_client lookup failed", exc_info=True
                )
        if self._manifest_path and self._hub_root:
            try:
                manifest = load_manifest(self._manifest_path, self._hub_root)
                repo = manifest.get(arg)
                if repo:
                    workspace = canonicalize_path(self._hub_root / repo.path)
                    return str(workspace), repo.id, "repo", repo.id
            except (OSError, ValueError):
                self._logger.debug(
                    "resolve_workspace: manifest lookup failed", exc_info=True
                )
        path = Path(arg)
        if not path.is_absolute():
            path = canonicalize_path(self._config.root / path)
        else:
            try:
                path = canonicalize_path(path)
            except OSError:
                return None
        if path.exists():
            return str(path), None, None, None
        return None

    async def _require_thread_workspace(
        self,
        message: TelegramMessage,
        expected_workspace: Optional[str],
        result: Any,
        *,
        action: str,
    ) -> bool:
        if not expected_workspace:
            return True
        info = _extract_thread_info(result)
        incoming = info.get("workspace_path")
        if not isinstance(incoming, str) or not incoming:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.thread.workspace_missing",
                action=action,
                expected_workspace=expected_workspace,
            )
            await self._send_message(
                message.chat_id,
                "App server did not return a workspace for this thread. "
                "Refusing to continue; fix the app-server workspace reporting and "
                "try /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return False
        try:
            expected_root = Path(expected_workspace).expanduser().resolve()
            incoming_root = Path(incoming).expanduser().resolve()
        except OSError:
            expected_root = None
            incoming_root = None
        if (
            expected_root is None
            or incoming_root is None
            or not _paths_compatible(expected_root, incoming_root)
        ):
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.thread.workspace_mismatch",
                action=action,
                expected_workspace=expected_workspace,
                incoming_workspace=incoming,
            )
            await self._send_message(
                message.chat_id,
                "App server returned a thread for a different workspace. "
                "Refusing to continue; fix the app-server workspace reporting and "
                "try /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return False
        return True

    async def _handle_opencode_resume(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        *,
        key: str,
        show_unscoped: bool,
        refresh: bool,
    ) -> None:
        if refresh:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.opencode.resume.refresh_ignored",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            )
        local_thread_ids: list[str] = []
        local_previews: dict[str, str] = {}
        local_thread_topics: dict[str, set[str]] = {}
        store_state = None
        if show_unscoped:
            store_state = await self._store.load()
            (
                local_thread_ids,
                local_previews,
                local_thread_topics,
            ) = _local_workspace_threads(
                store_state, record.workspace_path, current_key=key
            )
            for thread_id in record.thread_ids:
                local_thread_topics.setdefault(thread_id, set()).add(key)
                if thread_id not in local_thread_ids:
                    local_thread_ids.append(thread_id)
                cached_preview = _thread_summary_preview(record, thread_id)
                if cached_preview:
                    local_previews.setdefault(thread_id, cached_preview)
            allowed_thread_ids: set[str] = set()
            for thread_id in local_thread_ids:
                if thread_id in record.thread_ids:
                    allowed_thread_ids.add(thread_id)
                    continue
                for topic_key in local_thread_topics.get(thread_id, set()):
                    topic_record = (
                        store_state.topics.get(topic_key) if store_state else None
                    )
                    if topic_record and topic_record.agent == "opencode":
                        allowed_thread_ids.add(thread_id)
                        break
            if allowed_thread_ids:
                local_thread_ids = [
                    thread_id
                    for thread_id in local_thread_ids
                    if thread_id in allowed_thread_ids
                ]
                local_previews = {
                    thread_id: preview
                    for thread_id, preview in local_previews.items()
                    if thread_id in allowed_thread_ids
                }
            else:
                local_thread_ids = []
                local_previews = {}
        else:
            for thread_id in record.thread_ids:
                local_thread_ids.append(thread_id)
                cached_preview = _thread_summary_preview(record, thread_id)
                if cached_preview:
                    local_previews.setdefault(thread_id, cached_preview)
        if not local_thread_ids:
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "No previous OpenCode threads found for this topic. "
                    "Use /new to start one.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        items: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for thread_id in local_thread_ids:
            if thread_id in seen_ids:
                continue
            seen_ids.add(thread_id)
            preview = local_previews.get(thread_id)
            label = _format_missing_thread_label(thread_id, preview)
            items.append((thread_id, label))
        state = SelectionState(
            items=items,
            requester_user_id=(
                str(message.from_user_id) if message.from_user_id is not None else None
            ),
        )
        keyboard = self._build_resume_keyboard(state)
        self._resume_options[key] = state
        self._touch_cache_timestamp("resume_options", key)
        await self._send_message(
            message.chat_id,
            self._selection_prompt(RESUME_PICKER_PROMPT, state),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _handle_resume(self, message: TelegramMessage, args: str) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        parsed_args = self._parse_resume_args(args)
        if await self._handle_resume_shortcuts(key, message, parsed_args):
            return
        record = await self._router.get_topic(key)
        record = await self._ensure_resume_record(message, record, allow_pma=True)
        if record is None:
            return
        if record.pma_enabled and not parsed_args.show_unscoped:
            parsed_args = ResumeCommandArgs(
                trimmed=parsed_args.trimmed,
                remaining=parsed_args.remaining,
                show_unscoped=True,
                refresh=parsed_args.refresh,
            )
        if self._effective_agent(record) == "opencode":
            await self._handle_opencode_resume(
                message,
                record,
                key=key,
                show_unscoped=parsed_args.show_unscoped,
                refresh=parsed_args.refresh,
            )
            return
        client = await self._get_resume_client(message, record)
        if client is None:
            return
        thread_data = await self._gather_resume_threads(
            message,
            record,
            client,
            key=key,
            show_unscoped=parsed_args.show_unscoped,
        )
        if thread_data is None:
            return
        await self._render_resume_picker(
            message,
            record,
            key,
            parsed_args,
            thread_data,
            client,
        )

    def _parse_resume_args(self, args: str) -> ResumeCommandArgs:
        """Parse /resume arguments into structured values."""
        argv = self._parse_command_args(args)
        trimmed = args.strip()
        show_unscoped = False
        refresh = False
        remaining: list[str] = []
        for arg in argv:
            lowered = arg.lower()
            if lowered in ("--all", "all", "--unscoped", "unscoped"):
                show_unscoped = True
                continue
            if lowered in ("--refresh", "refresh"):
                refresh = True
                continue
            remaining.append(arg)
        if argv:
            trimmed = " ".join(remaining).strip()
        return ResumeCommandArgs(
            trimmed=trimmed,
            remaining=remaining,
            show_unscoped=show_unscoped,
            refresh=refresh,
        )

    async def _handle_resume_shortcuts(
        self, key: str, message: TelegramMessage, args: ResumeCommandArgs
    ) -> bool:
        """Handle numeric or explicit thread selections before listing threads."""
        trimmed = args.trimmed
        if trimmed.isdigit():
            state = self._resume_options.get(key)
            if state:
                page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
                choice = int(trimmed)
                if 0 < choice <= len(page_items):
                    thread_id = page_items[choice - 1][0]
                    await self._resume_thread_by_id(key, thread_id)
                    return True
        if trimmed and not trimmed.isdigit():
            if args.remaining and args.remaining[0].lower() in ("list", "ls"):
                return False
            await self._resume_thread_by_id(key, trimmed)
            return True
        return False

    async def _ensure_resume_record(
        self,
        message: TelegramMessage,
        record: Optional["TelegramTopicRecord"],
        *,
        allow_pma: bool = False,
    ) -> Optional["TelegramTopicRecord"]:
        """Validate resume preconditions and return the topic record."""
        if record is None:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if not record.workspace_path:
            if allow_pma and record.pma_enabled:
                workspace_path, error = self._resolve_workspace_path(
                    record, allow_pma=True
                )
                if workspace_path is None:
                    await self._send_message(
                        message.chat_id,
                        error or "PMA unavailable; hub root not configured.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return None
                record = self._record_with_workspace_path(record, workspace_path)
            else:
                await self._send_message(
                    message.chat_id,
                    TOPIC_NOT_BOUND_MESSAGE,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
        agent = self._effective_agent(record)
        if not self._agent_supports_resume(agent):
            supported_agents = set(
                self._agents_supporting_capability("durable_threads")
            )
            supported = ", ".join(sorted(supported_agents))
            agent_label = self._agent_display_name(agent)
            await self._send_message(
                message.chat_id,
                (
                    f"Resume is unavailable for {agent_label}. "
                    "The active agent must support durable threads."
                    + (f" Available agents: {supported}." if supported else "")
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        return record

    async def _get_resume_client(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional[CodexAppServerClient]:
        """Resolve the app server client for the topic workspace."""
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await self._send_message(
                message.chat_id,
                error or TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        try:
            client = await self._client_for_workspace(workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                APP_SERVER_UNAVAILABLE_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if client is None:
            await self._send_message(
                message.chat_id,
                error or TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        return client

    async def _gather_resume_threads(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        client: CodexAppServerClient,
        *,
        key: str,
        show_unscoped: bool,
    ) -> Optional[ResumeThreadData]:
        """Collect local and remote threads for the resume picker."""
        if not show_unscoped and not record.thread_ids:
            await self._send_message(
                message.chat_id,
                "No previous threads found for this topic. Use /new to start one.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        threads: list[dict[str, Any]] = []
        list_failed = False
        local_thread_ids: list[str] = []
        local_previews: dict[str, str] = {}
        local_thread_topics: dict[str, set[str]] = {}
        if show_unscoped:
            store_state = await self._store.load()
            (
                local_thread_ids,
                local_previews,
                local_thread_topics,
            ) = _local_workspace_threads(
                store_state, record.workspace_path, current_key=key
            )
            for thread_id in record.thread_ids:
                local_thread_topics.setdefault(thread_id, set()).add(key)
                if thread_id not in local_thread_ids:
                    local_thread_ids.append(thread_id)
                cached_preview = _thread_summary_preview(record, thread_id)
                if cached_preview:
                    local_previews.setdefault(thread_id, cached_preview)
        limit = _resume_thread_list_limit(record.thread_ids)
        needed_ids = (
            None if show_unscoped or not record.thread_ids else set(record.thread_ids)
        )
        try:
            threads, _ = await self._list_threads_paginated(
                client,
                limit=limit,
                max_pages=THREAD_LIST_MAX_PAGES,
                needed_ids=needed_ids,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            list_failed = True
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            if show_unscoped and not local_thread_ids:
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "Failed to list threads; check logs for details.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
        entries_by_id: dict[str, dict[str, Any]] = {}
        for entry in threads:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                entries_by_id[entry_id] = entry
        candidates: list[dict[str, Any]] = []
        unscoped_entries: list[dict[str, Any]] = []
        saw_path = False
        if show_unscoped:
            if threads:
                filtered, unscoped_entries, saw_path = _partition_threads(
                    threads, record.workspace_path
                )
                seen_ids = {
                    entry.get("id")
                    for entry in filtered
                    if isinstance(entry.get("id"), str)
                }
                candidates = filtered + [
                    entry
                    for entry in unscoped_entries
                    if entry.get("id") not in seen_ids
                ]
            if not candidates and not local_thread_ids:
                if unscoped_entries and not saw_path:
                    await self._send_message(
                        message.chat_id,
                        _with_conversation_id(
                            "No workspace-tagged threads available. Use /resume --all to list "
                            "unscoped threads.",
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                        ),
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return None
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "No previous threads found for this workspace. "
                        "If threads exist, update the app-server to include cwd metadata or use /new.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
        return ResumeThreadData(
            candidates=candidates,
            entries_by_id=entries_by_id,
            local_thread_ids=local_thread_ids,
            local_previews=local_previews,
            local_thread_topics=local_thread_topics,
            list_failed=list_failed,
            threads=threads,
            unscoped_entries=unscoped_entries,
            saw_path=saw_path,
        )

    async def _render_resume_picker(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        key: str,
        args: ResumeCommandArgs,
        thread_data: ResumeThreadData,
        client: CodexAppServerClient,
    ) -> None:
        """Build and send the resume picker from gathered thread data."""
        entries_by_id = thread_data.entries_by_id
        local_thread_ids = thread_data.local_thread_ids
        local_previews = thread_data.local_previews
        local_thread_topics = thread_data.local_thread_topics
        missing_ids: list[str] = []
        if args.show_unscoped:
            for thread_id in local_thread_ids:
                if thread_id not in entries_by_id:
                    missing_ids.append(thread_id)
        else:
            for thread_id in record.thread_ids:
                if thread_id not in entries_by_id:
                    missing_ids.append(thread_id)
        if args.refresh and missing_ids:
            refreshed = await self._refresh_thread_summaries(
                client,
                missing_ids,
                topic_keys_by_thread=(
                    local_thread_topics if args.show_unscoped else None
                ),
                default_topic_key=key,
            )
            if refreshed:
                if args.show_unscoped:
                    store_state = await self._store.load()
                    (
                        local_thread_ids,
                        local_previews,
                        local_thread_topics,
                    ) = _local_workspace_threads(
                        store_state, record.workspace_path, current_key=key
                    )
                    for thread_id in record.thread_ids:
                        local_thread_topics.setdefault(thread_id, set()).add(key)
                        if thread_id not in local_thread_ids:
                            local_thread_ids.append(thread_id)
                        cached_preview = _thread_summary_preview(record, thread_id)
                        if cached_preview:
                            local_previews.setdefault(thread_id, cached_preview)
                else:
                    record = await self._router.get_topic(key) or record
        items: list[tuple[str, str]] = []
        button_labels: dict[str, str] = {}
        seen_item_ids: set[str] = set()
        if args.show_unscoped:
            for entry in thread_data.candidates:
                candidate_id = entry.get("id")
                if not isinstance(candidate_id, str) or not candidate_id:
                    continue
                if candidate_id in seen_item_ids:
                    continue
                seen_item_ids.add(candidate_id)
                label = _format_thread_preview(entry)
                button_label = _extract_first_user_preview(entry)
                timestamp = _format_resume_timestamp(entry)
                if timestamp and button_label:
                    button_labels[candidate_id] = f"{timestamp} · {button_label}"
                elif timestamp:
                    button_labels[candidate_id] = timestamp
                elif button_label:
                    button_labels[candidate_id] = button_label
                if label == "(no preview)":
                    cached_preview = local_previews.get(candidate_id)
                    if cached_preview:
                        label = cached_preview
                items.append((candidate_id, label))
            for thread_id in local_thread_ids:
                if thread_id in seen_item_ids:
                    continue
                seen_item_ids.add(thread_id)
                cached_preview = local_previews.get(thread_id)
                label = (
                    cached_preview
                    if cached_preview
                    else _format_missing_thread_label(thread_id, None)
                )
                items.append((thread_id, label))
        else:
            if record.thread_ids:
                for thread_id in record.thread_ids:
                    entry_data = entries_by_id.get(thread_id)
                    if entry_data is None:
                        cached_preview = _thread_summary_preview(record, thread_id)
                        label = _format_missing_thread_label(thread_id, cached_preview)
                    else:
                        label = _format_thread_preview(entry_data)
                        button_label = _extract_first_user_preview(entry_data)
                        timestamp = _format_resume_timestamp(entry_data)
                        if timestamp and button_label:
                            button_labels[thread_id] = f"{timestamp} · {button_label}"
                        elif timestamp:
                            button_labels[thread_id] = timestamp
                        elif button_label:
                            button_labels[thread_id] = button_label
                        if label == "(no preview)":
                            cached_preview = _thread_summary_preview(record, thread_id)
                            if cached_preview:
                                label = cached_preview
                    items.append((thread_id, label))
            else:
                for entry in entries_by_id.values():
                    entry_id = entry.get("id")
                    if not isinstance(entry_id, str) or not entry_id:
                        continue
                    label = _format_thread_preview(entry)
                    button_label = _extract_first_user_preview(entry)
                    timestamp = _format_resume_timestamp(entry)
                    if timestamp and button_label:
                        button_labels[entry_id] = f"{timestamp} · {button_label}"
                    elif timestamp:
                        button_labels[entry_id] = timestamp
                    elif button_label:
                        button_labels[entry_id] = button_label
                    items.append((entry_id, label))
        if missing_ids:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.resume.missing_thread_metadata",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                stored_count=len(record.thread_ids),
                listed_count=(
                    len(entries_by_id)
                    if not args.show_unscoped
                    else len(thread_data.threads)
                ),
                missing_ids=missing_ids[:RESUME_MISSING_IDS_LOG_LIMIT],
                list_failed=thread_data.list_failed,
            )
        if not items:
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "No resumable threads found.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        state = SelectionState(
            items=items,
            button_labels=button_labels,
            requester_user_id=(
                str(message.from_user_id) if message.from_user_id is not None else None
            ),
        )
        keyboard = self._build_resume_keyboard(state)
        self._resume_options[key] = state
        self._touch_cache_timestamp("resume_options", key)
        await self._send_message(
            message.chat_id,
            self._selection_prompt(RESUME_PICKER_PROMPT, state),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _refresh_thread_summaries(
        self,
        client: CodexAppServerClient,
        thread_ids: Sequence[str],
        *,
        topic_keys_by_thread: Optional[dict[str, set[str]]] = None,
        default_topic_key: Optional[str] = None,
    ) -> set[str]:
        refreshed: set[str] = set()
        if not thread_ids:
            return refreshed
        unique_ids: list[str] = []
        seen: set[str] = set()
        for thread_id in thread_ids:
            if not isinstance(thread_id, str) or not thread_id:
                continue
            if thread_id in seen:
                continue
            seen.add(thread_id)
            unique_ids.append(thread_id)
            if len(unique_ids) >= RESUME_REFRESH_LIMIT:
                break
        for thread_id in unique_ids:
            try:
                result = await client.thread_resume(thread_id)
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.resume.refresh_failed",
                    thread_id=thread_id,
                    exc=exc,
                )
                continue
            user_preview, assistant_preview = _extract_thread_preview_parts(result)
            info = _extract_thread_info(result)
            workspace_path = info.get("workspace_path")
            rollout_path = info.get("rollout_path")
            if (
                user_preview is None
                and assistant_preview is None
                and workspace_path is None
                and rollout_path is None
            ):
                continue
            last_used_at = now_iso() if user_preview or assistant_preview else None

            def apply(
                record: TelegramTopicRecord,
                *,
                thread_id: str = thread_id,
                user_preview: Optional[str] = user_preview,
                assistant_preview: Optional[str] = assistant_preview,
                last_used_at: Optional[str] = last_used_at,
                workspace_path: Optional[str] = workspace_path,
                rollout_path: Optional[str] = rollout_path,
            ) -> None:
                _set_thread_summary(
                    record,
                    thread_id,
                    user_preview=user_preview,
                    assistant_preview=assistant_preview,
                    last_used_at=last_used_at,
                    workspace_path=workspace_path,
                    rollout_path=rollout_path,
                )

            keys = (
                topic_keys_by_thread.get(thread_id)
                if topic_keys_by_thread is not None
                else None
            )
            if keys:
                for key in keys:
                    await self._store.update_topic(key, apply)
            elif default_topic_key:
                await self._store.update_topic(default_topic_key, apply)
            else:
                continue
            refreshed.add(thread_id)
        return refreshed

    async def _list_threads_paginated(
        self,
        client: CodexAppServerClient,
        *,
        limit: int,
        max_pages: int,
        needed_ids: Optional[set[str]] = None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        entries: list[dict[str, Any]] = []
        found_ids: set[str] = set()
        seen_ids: set[str] = set()
        cursor: Optional[str] = None
        page_count = max(1, max_pages)
        for _ in range(page_count):
            payload = await client.thread_list(cursor=cursor, limit=limit)
            page_entries = _coerce_thread_list(payload)
            for entry in page_entries:
                if not isinstance(entry, dict):
                    continue
                thread_id = entry.get("id")
                if isinstance(thread_id, str):
                    if thread_id in seen_ids:
                        continue
                    seen_ids.add(thread_id)
                    found_ids.add(thread_id)
                entries.append(entry)
            if needed_ids is not None and needed_ids.issubset(found_ids):
                break
            cursor = _extract_thread_list_cursor(payload)
            if not cursor:
                break
        return entries, found_ids

    async def _resume_thread_by_id(
        self,
        key: str,
        thread_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        callback_answered = False

        async def _answer_once(text: str) -> None:
            nonlocal callback_answered
            if callback_answered or callback is None:
                return
            await self._answer_callback(callback, text)
            callback_answered = True

        chat_id, thread_id_val = _split_topic_key(key)
        self._resume_options.pop(key, None)
        record = await self._router.get_topic(key)
        if record is not None and self._effective_agent(record) == "opencode":
            await self._resume_opencode_thread_by_id(key, thread_id, callback=callback)
            return
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    error or TOPIC_NOT_BOUND_RESUME_MESSAGE,
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        record = self._record_with_workspace_path(record, workspace_path)
        try:
            await _answer_once("Resuming...")
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=chat_id,
                thread_id=thread_id_val,
                exc=exc,
            )
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    APP_SERVER_UNAVAILABLE_MESSAGE,
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if client is None:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    TOPIC_NOT_BOUND_RESUME_MESSAGE,
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            result = await client.thread_resume(thread_id)
        except (OSError, RuntimeError, ValueError, CodexAppServerError) as exc:
            if is_missing_thread_error(exc):
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.resume.missing_thread",
                    topic_key=key,
                    thread_id=thread_id,
                )

                def clear_stale(record: "TelegramTopicRecord") -> None:
                    if record.active_thread_id == thread_id:
                        record.active_thread_id = None
                    if thread_id in record.thread_ids:
                        record.thread_ids.remove(thread_id)
                    record.thread_summaries.pop(thread_id, None)

                await self._store.update_topic(key, clear_stale)
                await _answer_once("Thread missing")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Thread no longer exists. Cleared stale state; use /new to start a fresh thread.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                topic_key=key,
                thread_id=thread_id,
                exc=exc,
            )
            await _answer_once("Resume failed")
            chat_id, thread_id_val = _split_topic_key(key)
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Failed to resume thread; check logs for details.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        info = _extract_thread_info(result)
        resumed_path = info.get("workspace_path")
        if record is None or not record.workspace_path:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    TOPIC_NOT_BOUND_RESUME_MESSAGE,
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if not isinstance(resumed_path, str):
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread metadata missing workspace path; resume aborted to avoid cross-worktree mixups.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            workspace_root = Path(record.workspace_path).expanduser().resolve()
            resumed_root = Path(resumed_path).expanduser().resolve()
        except OSError:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread workspace path is invalid; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if not _paths_compatible(workspace_root, resumed_root):
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread belongs to a different workspace; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        conflict_key = await self._find_thread_conflict(thread_id, key=key)
        if conflict_key:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread is already active in another topic; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.conflict",
                topic_key=key,
                thread_id=thread_id,
                conflict_topic=conflict_key,
            )
            return
        sync_binding = True
        if (
            getattr(self, "_hub_root", None) is not None
            or getattr(self._config, "root", None) is not None
        ):
            try:
                from .execution import _resolve_telegram_managed_thread

                (
                    _orchestration_service,
                    managed_thread,
                ) = await _resolve_telegram_managed_thread(
                    self,
                    surface_key=key,
                    workspace_root=workspace_root,
                    agent=self._effective_runtime_agent(record),
                    agent_profile=self._effective_agent_profile(record),
                    repo_id=(
                        record.repo_id.strip()
                        if isinstance(record.repo_id, str) and record.repo_id.strip()
                        else None
                    ),
                    resource_kind=(
                        record.resource_kind.strip()
                        if isinstance(record.resource_kind, str)
                        and record.resource_kind.strip()
                        else None
                    ),
                    resource_id=(
                        record.resource_id.strip()
                        if isinstance(record.resource_id, str)
                        and record.resource_id.strip()
                        else None
                    ),
                    mode="repo",
                    pma_enabled=False,
                    backend_thread_id=thread_id,
                    allow_new_thread=True,
                )
                if managed_thread is None:
                    raise RuntimeError("managed thread resolution returned no thread")
                sync_binding = False
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                ConnectionError,
            ) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.resume.binding_failed",
                    topic_key=key,
                    thread_id=thread_id,
                    exc=exc,
                )
                await _answer_once("Resume failed")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Failed to rebind the managed thread; check logs for details.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
        updated_record = await self._apply_thread_result(
            chat_id,
            thread_id_val,
            result,
            active_thread_id=thread_id,
            overwrite_defaults=True,
            sync_binding=sync_binding,
        )
        await _answer_once("Resumed thread")
        message = _format_resume_summary(
            thread_id,
            result,
            workspace_path=updated_record.workspace_path,
            model=updated_record.model,
            effort=updated_record.effort,
        )
        await self._finalize_selection(key, callback, message)

    async def _resume_opencode_thread_by_id(
        self,
        key: str,
        thread_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        callback_answered = False

        async def _answer_once(text: str) -> None:
            nonlocal callback_answered
            if callback_answered or callback is None:
                return
            await self._answer_callback(callback, text)
            callback_answered = True

        chat_id, thread_id_val = _split_topic_key(key)
        self._resume_options.pop(key, None)
        record = await self._router.get_topic(key)
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    error or TOPIC_NOT_BOUND_RESUME_MESSAGE,
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        record = self._record_with_workspace_path(record, workspace_path)
        supervisor = getattr(self, "_opencode_supervisor", None)
        if supervisor is None:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        workspace_root = self._canonical_workspace_root(record.workspace_path)
        if workspace_root is None:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Workspace unavailable; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            await _answer_once("Resuming...")
            client = await supervisor.get_client(workspace_root)
            session = await client.get_session(thread_id)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            if self._is_missing_opencode_session_error(exc):
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.resume.missing_thread",
                    topic_key=key,
                    thread_id=thread_id,
                    agent="opencode",
                )

                def clear_stale(record: "TelegramTopicRecord") -> None:
                    if record.active_thread_id == thread_id:
                        record.active_thread_id = None
                    if thread_id in record.thread_ids:
                        record.thread_ids.remove(thread_id)
                    record.thread_summaries.pop(thread_id, None)

                await self._store.update_topic(key, clear_stale)
                await _answer_once("Thread missing")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Thread no longer exists. Cleared stale state; use /new to start a fresh thread.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.opencode.resume.failed",
                topic_key=key,
                thread_id=thread_id,
                exc=exc,
            )
            await _answer_once("Resume failed")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Failed to resume OpenCode thread; check logs for details.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        resumed_path = _extract_opencode_session_path(session)
        if resumed_path:
            try:
                workspace_root = Path(record.workspace_path).expanduser().resolve()
                resumed_root = Path(resumed_path).expanduser().resolve()
            except OSError:
                await _answer_once("Resume aborted")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Thread workspace path is invalid; resume aborted.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
            if not _paths_compatible(workspace_root, resumed_root):
                await _answer_once("Resume aborted")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Thread belongs to a different workspace; resume aborted.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
        conflict_key = await self._find_thread_conflict(thread_id, key=key)
        if conflict_key:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread is already active in another topic; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.conflict",
                topic_key=key,
                thread_id=thread_id,
                conflict_topic=conflict_key,
            )
            return

        def apply(record: "TelegramTopicRecord") -> None:
            record.active_thread_id = thread_id
            if thread_id in record.thread_ids:
                record.thread_ids.remove(thread_id)
            record.thread_ids.insert(0, thread_id)
            if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
            _set_thread_summary(
                record,
                thread_id,
                last_used_at=now_iso(),
                workspace_path=record.workspace_path,
                rollout_path=record.rollout_path,
            )

        updated_record = await self._router.update_topic(chat_id, thread_id_val, apply)
        if updated_record is not None and updated_record.workspace_path:
            from .execution import _sync_telegram_thread_binding

            pma_enabled = bool(updated_record.pma_enabled)
            await _sync_telegram_thread_binding(
                self,
                surface_key=key,
                workspace_root=Path(updated_record.workspace_path),
                agent=self._effective_runtime_agent(updated_record),
                repo_id=(
                    updated_record.repo_id.strip()
                    if isinstance(updated_record.repo_id, str)
                    and updated_record.repo_id.strip()
                    else None
                ),
                resource_kind=(
                    updated_record.resource_kind.strip()
                    if isinstance(updated_record.resource_kind, str)
                    and updated_record.resource_kind.strip()
                    else None
                ),
                resource_id=(
                    updated_record.resource_id.strip()
                    if isinstance(updated_record.resource_id, str)
                    and updated_record.resource_id.strip()
                    else None
                ),
                backend_thread_id=None if pma_enabled else thread_id,
                mode="pma" if pma_enabled else "repo",
                pma_enabled=pma_enabled,
            )
        await self._answer_callback(callback, "Resumed thread")
        summary = None
        if updated_record is not None:
            summary = updated_record.thread_summaries.get(thread_id)
        entry: dict[str, Any] = {}
        if summary is not None:
            entry = {
                "user_preview": summary.user_preview,
                "assistant_preview": summary.assistant_preview,
            }
        message = _format_resume_summary(
            thread_id,
            entry,
            workspace_path=updated_record.workspace_path if updated_record else None,
            model=updated_record.model if updated_record else None,
            effort=updated_record.effort if updated_record else None,
        )
        await self._finalize_selection(key, callback, message)
