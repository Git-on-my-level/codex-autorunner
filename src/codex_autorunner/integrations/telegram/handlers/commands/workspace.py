from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence

import httpx

from .....agents.opencode.runtime import extract_session_id
from .....core.flows import FlowStore
from .....core.flows.models import FlowRunStatus
from .....core.git_utils import GitError, reset_branch_from_origin_main
from .....core.logging_utils import log_event
from .....core.pma_context import clear_pma_prompt_state_sessions
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
from ....chat.session_messages import (
    build_branch_reset_started_lines,
    build_fresh_session_started_lines,
    build_reset_state_lines,
    build_thread_detail_lines,
)
from ....chat.status_diagnostics import (
    StatusBlockContext,
    build_status_block_lines,
)
from ....chat.thread_summaries import _format_resume_timestamp
from ...adapter import (
    TelegramCallbackQuery,
    TelegramMessage,
)
from ...collaboration_helpers import (
    collaboration_summary_lines,
    evaluate_collaboration_summary,
)
from ...config import AppServerUnavailableError
from ...constants import (
    BIND_PICKER_PROMPT,
    DEFAULT_AGENT,
    DEFAULT_PAGE_SIZE,
    MAX_TOPIC_THREAD_HISTORY,
    RESUME_MISSING_IDS_LOG_LIMIT,
    RESUME_PICKER_PROMPT,
    RESUME_REFRESH_LIMIT,
    THREAD_LIST_MAX_PAGES,
)
from ...helpers import (
    _approval_age_seconds,
    _clear_thread_mirror,
    _coerce_thread_list,
    _extract_first_user_preview,
    _extract_thread_id,
    _extract_thread_info,
    _extract_thread_list_cursor,
    _extract_thread_preview_parts,
    _format_missing_thread_label,
    _format_resume_summary,
    _format_thread_preview,
    _format_token_usage,
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


def _telegram_status_base_lines(
    *,
    message: TelegramMessage,
    record: "TelegramTopicRecord",
    runtime: Any,
    command_policy: Any,
    plain_text_policy: Any,
) -> list[str]:
    workspace_label = record.workspace_path or (
        "hub" if record.pma_enabled else "unbound"
    )
    queue = getattr(runtime, "queue", None) if runtime is not None else None
    pending_queue = queue.pending() if queue is not None else 0
    lines: list[str] = []
    if record.pma_enabled:
        lines.append("Mode: PMA (hub)")
        if record.pma_prev_workspace_path:
            lines.append(f"Previous binding: {record.pma_prev_workspace_path}")
            lines.append("Use /pma off to restore previous binding.")
    elif record.workspace_path:
        lines.append("Mode: workspace")
        lines.append("Topic is bound.")
    lines.extend(
        [
            f"Workspace: {workspace_label}",
            f"Workspace ID: {record.workspace_id or 'unknown'}",
            f"Active thread: {record.active_thread_id or 'none'}",
            f"Active turn: {runtime.current_turn_id or 'none'}",
            f"Queued requests: {pending_queue}",
            *collaboration_summary_lines(
                message,
                command_result=command_policy,
                plain_text_result=plain_text_policy,
            ),
        ]
    )
    if pending_queue:
        lines.append("Queued messages include Cancel and Interrupt + Send buttons.")
    return lines


class WorkspaceCommands(TelegramCommandSupportMixin):
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
                import asyncio
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

    async def _handle_repos(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        if not self._manifest_path or not self._hub_root:
            await self._send_message(
                message.chat_id,
                "Hub manifest not configured.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        try:
            manifest = load_manifest(self._manifest_path, self._hub_root)
        except (OSError, ValueError) as exc:
            await self._send_message(
                message.chat_id,
                f"Failed to load manifest: {exc}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        lines = ["Repositories:"]
        for repo in manifest.repos:
            if not repo.enabled:
                continue
            lines.append(f"- `{repo.id}` ({repo.path})")

        lines.append("\nUse /bind <repo_id> to switch context.")

        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            parse_mode="Markdown",
        )

    async def _handle_bind(self, message: TelegramMessage, args: str) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        if not args:
            options = self._list_manifest_repos()
            if not options:
                await self._send_message(
                    message.chat_id,
                    "Usage: /bind <repo_id> or /bind <path>.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            items = [(repo_id, repo_id) for repo_id in options]
            state = SelectionState(
                items=items,
                requester_user_id=(
                    str(message.from_user_id)
                    if message.from_user_id is not None
                    else None
                ),
            )
            keyboard = self._build_bind_keyboard(state)
            self._bind_options[key] = state
            self._touch_cache_timestamp("bind_options", key)
            await self._send_message(
                message.chat_id,
                self._selection_prompt(BIND_PICKER_PROMPT, state),
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=keyboard,
            )
            return
        await self._bind_topic_with_arg(key, args, message)

    async def _bind_topic_by_repo_id(
        self,
        key: str,
        repo_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        self._bind_options.pop(key, None)
        resolved = self._resolve_workspace(repo_id)
        if resolved is None:
            await self._answer_callback(callback, "Repo not found")
            await self._finalize_selection(key, callback, "Repo not found.")
            return
        workspace_path, resolved_repo_id, resource_kind, resource_id = resolved
        chat_id, thread_id = _split_topic_key(key)
        scope = self._topic_scope_id(resolved_repo_id, workspace_path)
        await self._router.set_topic_scope(chat_id, thread_id, scope)
        resolved_workspace_id = (
            resource_id
            if resource_kind == "agent_workspace"
            else self._workspace_id_for_path(workspace_path)
        )
        await self._router.bind_topic(
            chat_id,
            thread_id,
            workspace_path,
            repo_id=resolved_repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_id=resolved_workspace_id,
            scope=scope,
        )

        def apply_bind_updates(record: TelegramTopicRecord) -> None:
            record.resource_kind = resource_kind
            record.resource_id = resource_id
            if resolved_workspace_id:
                record.workspace_id = resolved_workspace_id
            # Mutual exclusion: Binding to a repo disables PMA mode.
            record.pma_enabled = False

        await self._router.update_topic(
            chat_id,
            thread_id,
            apply_bind_updates,
            scope=scope,
        )
        await self._answer_callback(callback, "Bound to repo")
        await self._finalize_selection(
            key,
            callback,
            f"Bound to {resolved_repo_id or workspace_path}.",
        )

    async def _bind_topic_with_arg(
        self, key: str, arg: str, message: TelegramMessage
    ) -> None:
        self._bind_options.pop(key, None)
        resolved = self._resolve_workspace(arg)
        if resolved is None:
            await self._send_message(
                message.chat_id,
                "Unknown repo or path. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        workspace_path, repo_id, resource_kind, resource_id = resolved
        scope = self._topic_scope_id(repo_id, workspace_path)
        await self._router.set_topic_scope(message.chat_id, message.thread_id, scope)
        resolved_workspace_id = (
            resource_id
            if resource_kind == "agent_workspace"
            else self._workspace_id_for_path(workspace_path)
        )
        await self._router.bind_topic(
            message.chat_id,
            message.thread_id,
            workspace_path,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_id=resolved_workspace_id,
            scope=scope,
        )

        def apply_bind_updates(record: TelegramTopicRecord) -> None:
            record.resource_kind = resource_kind
            record.resource_id = resource_id
            if resolved_workspace_id:
                record.workspace_id = resolved_workspace_id
            # Mutual exclusion: Binding to a repo disables PMA mode.
            record.pma_enabled = False

        await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            apply_bind_updates,
            scope=scope,
        )
        await self._send_message(
            message.chat_id,
            f"Bound to {repo_id or workspace_path}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_reset(self, message: TelegramMessage) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        pma_enabled = bool(record and record.pma_enabled)
        if pma_enabled:
            registry = getattr(self, "_hub_thread_registry", None)
            if registry and hasattr(self, "_pma_registry_key"):
                pma_key = self._pma_registry_key(record, message)
                if pma_key:
                    registry.reset_thread(pma_key)
                    hub_root = getattr(self, "_hub_root", None)
                    if hub_root is not None:
                        try:
                            clear_pma_prompt_state_sessions(
                                Path(hub_root), keys=(pma_key,)
                            )
                        except OSError as exc:
                            log_event(
                                self._logger,
                                logging.WARNING,
                                "telegram.pma.prompt_state.clear_failed",
                                topic_key=key,
                                chat_id=message.chat_id,
                                thread_id=message.thread_id,
                                pma_key=pma_key,
                                exc=exc,
                            )
            if getattr(self._config, "root", None) is not None and callable(
                getattr(self, "_spawn_task", None)
            ):
                from .execution import _reset_telegram_thread_binding

                hub_root = getattr(self, "_hub_root", None)
                if hub_root is not None and record is not None:
                    try:
                        await _reset_telegram_thread_binding(
                            self,
                            surface_key=key,
                            workspace_root=canonicalize_path(Path(hub_root)),
                            agent=self._effective_runtime_agent(record),
                            agent_profile=self._effective_agent_profile(record),
                            repo_id=(
                                record.repo_id.strip()
                                if isinstance(record.repo_id, str)
                                and record.repo_id.strip()
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
                            mode="pma",
                            pma_enabled=True,
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.pma.reset.managed_thread_reset_failed",
                            topic_key=key,
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            exc=exc,
                        )
                        await self._send_message(
                            message.chat_id,
                            "Failed to reset PMA thread; check logs for details.",
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                        )
                        return
                    update_topic = getattr(self._router, "update_topic", None)
                    if callable(update_topic):
                        await update_topic(
                            message.chat_id,
                            message.thread_id,
                            _clear_thread_mirror,
                        )
            await self._send_message(
                message.chat_id,
                "\n".join(
                    build_reset_state_lines(
                        mode_label="PMA",
                        actor_label=self._effective_agent_label(record),
                        state_label="fresh state",
                    )
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
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
                return
            workspace_root = self._canonical_workspace_root(record.workspace_path)
            if workspace_root is None:
                await self._send_message(
                    message.chat_id,
                    "Workspace unavailable.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                client = await supervisor.get_client(workspace_root)
                session = await client.create_session(directory=str(workspace_root))
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
                    "Failed to reset OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not session_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to reset OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

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
            thread_id = session_id
        else:
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
                return
            if client is None:
                await self._send_message(
                    message.chat_id,
                    TOPIC_NOT_BOUND_MESSAGE,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                thread = await client.thread_start(
                    record.workspace_path,
                    **self._thread_start_kwargs(record),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.reset.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    workspace_path=record.workspace_path,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Failed to reset thread; check logs for details.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if not await self._require_thread_workspace(
                message, record.workspace_path, thread, action="thread_start"
            ):
                return
            thread_id = _extract_thread_id(thread)
            if not thread_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to reset thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._apply_thread_result(
                message.chat_id, message.thread_id, thread, active_thread_id=thread_id
            )
        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        await self._send_message(
            message.chat_id,
            "\n".join(
                [
                    *build_reset_state_lines(
                        mode_label="repo",
                        actor_label=self._effective_agent_label(record),
                        state_label="fresh state",
                    ),
                    *build_thread_detail_lines(
                        headline=f"Started new thread `{thread_id}`.",
                        actor_label=self._effective_agent_label(record),
                        effort=effort_label,
                    ),
                ]
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_new(self, message: TelegramMessage) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        pma_enabled = bool(record and record.pma_enabled)
        if pma_enabled:
            registry = getattr(self, "_hub_thread_registry", None)
            if registry and hasattr(self, "_pma_registry_key"):
                pma_key = self._pma_registry_key(record, message)
                if pma_key:
                    registry.reset_thread(pma_key)
                    hub_root = getattr(self, "_hub_root", None)
                    if hub_root is not None:
                        try:
                            clear_pma_prompt_state_sessions(
                                Path(hub_root), keys=(pma_key,)
                            )
                        except OSError as exc:
                            log_event(
                                self._logger,
                                logging.WARNING,
                                "telegram.pma.prompt_state.clear_failed",
                                topic_key=key,
                                chat_id=message.chat_id,
                                thread_id=message.thread_id,
                                pma_key=pma_key,
                                exc=exc,
                            )
            if getattr(self._config, "root", None) is not None and callable(
                getattr(self, "_spawn_task", None)
            ):
                from .execution import _reset_telegram_thread_binding

                hub_root = getattr(self, "_hub_root", None)
                if hub_root is not None and record is not None:
                    try:
                        await _reset_telegram_thread_binding(
                            self,
                            surface_key=key,
                            workspace_root=canonicalize_path(Path(hub_root)),
                            agent=self._effective_runtime_agent(record),
                            agent_profile=self._effective_agent_profile(record),
                            repo_id=(
                                record.repo_id.strip()
                                if isinstance(record.repo_id, str)
                                and record.repo_id.strip()
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
                            mode="pma",
                            pma_enabled=True,
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.pma.new.managed_thread_reset_failed",
                            topic_key=key,
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            exc=exc,
                        )
                        await self._send_message(
                            message.chat_id,
                            "Failed to reset PMA session; check logs for details.",
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                        )
                        return
                    update_topic = getattr(self._router, "update_topic", None)
                    if callable(update_topic):
                        await update_topic(
                            message.chat_id,
                            message.thread_id,
                            _clear_thread_mirror,
                        )
            await self._send_message(
                message.chat_id,
                "\n".join(
                    build_fresh_session_started_lines(
                        mode_label="PMA",
                        actor_label=self._effective_agent_label(record),
                        state_label="new thread ready",
                    )
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        agent = self._effective_agent(record)
        from .execution import _sync_telegram_thread_binding

        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            workspace_root = self._canonical_workspace_root(record.workspace_path)
            if workspace_root is None:
                await self._send_message(
                    message.chat_id,
                    "Workspace unavailable.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                client = await supervisor.get_client(workspace_root)
                session = await client.create_session(directory=str(workspace_root))
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
                return
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not session_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

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
            thread_id = session_id
            await _sync_telegram_thread_binding(
                self,
                surface_key=key,
                workspace_root=workspace_root,
                agent=self._effective_runtime_agent(record),
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
                backend_thread_id=thread_id,
                mode="repo",
                pma_enabled=False,
                replace_existing=True,
            )
        else:
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
                return
            if client is None:
                await self._send_message(
                    message.chat_id,
                    TOPIC_NOT_BOUND_MESSAGE,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                thread = await client.thread_start(
                    record.workspace_path,
                    **self._thread_start_kwargs(record),
                )
            except (OSError, RuntimeError, ValueError, CodexAppServerError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.new.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    workspace_path=record.workspace_path,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new thread; check logs for details.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if not await self._require_thread_workspace(
                message, record.workspace_path, thread, action="thread_start"
            ):
                return
            thread_id = _extract_thread_id(thread)
            if not thread_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._apply_thread_result(
                message.chat_id, message.thread_id, thread, active_thread_id=thread_id
            )
            await _sync_telegram_thread_binding(
                self,
                surface_key=key,
                workspace_root=Path(record.workspace_path),
                agent=self._effective_runtime_agent(record),
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
                backend_thread_id=thread_id,
                mode="repo",
                pma_enabled=False,
                replace_existing=True,
            )
        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        await self._send_message(
            message.chat_id,
            "\n".join(
                [
                    *build_fresh_session_started_lines(
                        mode_label="repo",
                        actor_label=self._effective_agent_label(record),
                        state_label="new thread ready",
                    ),
                    *build_thread_detail_lines(
                        headline=f"Started new thread `{thread_id}`.",
                        workspace_path=record.workspace_path,
                        actor_label=self._effective_agent_label(record),
                        model=record.model or "default",
                        effort=effort_label,
                    ),
                ]
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_newt(self, message: TelegramMessage) -> None:
        import re

        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        pma_enabled = bool(record and record.pma_enabled)
        if pma_enabled:
            await self._send_message(
                message.chat_id,
                "/newt is not available in PMA mode. Use /new instead.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        workspace_root = self._canonical_workspace_root(record.workspace_path)
        if workspace_root is None:
            await self._send_message(
                message.chat_id,
                "Workspace unavailable.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        safe_chat_id = re.sub(r"[^a-zA-Z0-9]+", "-", str(message.chat_id)).strip("-")
        if not safe_chat_id:
            safe_chat_id = "chat"
        if message.thread_id is None:
            thread_identity = f"msg-{message.message_id}-upd-{message.update_id}"
        else:
            thread_identity = f"thread-{message.thread_id}"
        safe_thread_id = re.sub(r"[^a-zA-Z0-9]+", "-", thread_identity).strip("-")
        if not safe_thread_id:
            safe_thread_id = "unscoped"
        branch_suffix = hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()[
            :10
        ]
        branch_name = f"thread-chat-{safe_chat_id}-{safe_thread_id}-{branch_suffix}"
        had_previous = bool(record.active_thread_id)

        try:
            default_branch = await asyncio.to_thread(
                reset_branch_from_origin_main,
                workspace_root,
                branch_name,
            )
        except GitError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.newt.branch_reset.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                branch=branch_name,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                f"Failed to reset branch `{branch_name}` from origin default branch: {exc}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        setup_command_count = 0
        hub_client = getattr(self, "_hub_client", None)
        if hub_client is not None:
            repo_id_hint = (
                record.repo_id.strip()
                if isinstance(record.repo_id, str) and record.repo_id.strip()
                else None
            )
            try:
                from .....core.hub_control_plane import WorkspaceSetupCommandRequest

                cp_result = await hub_client.run_workspace_setup_commands(
                    WorkspaceSetupCommandRequest(
                        workspace_root=str(workspace_root),
                        repo_id_hint=repo_id_hint,
                    )
                )
                setup_command_count = cp_result.setup_command_count
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.newt.setup.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    workspace_path=str(workspace_root),
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    f"Reset branch `{branch_name}` to `origin/{default_branch}` but setup commands failed: {exc}",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

        def apply(record: "TelegramTopicRecord") -> None:
            record.active_thread_id = None
            record.thread_ids = []
            record.thread_summaries = {}
            record.rollout_path = None
            record.pending_compact_seed = None
            record.pending_compact_seed_thread_id = None

        await self._router.update_topic(message.chat_id, message.thread_id, apply)

        agent = self._effective_agent(record)
        from .execution import _sync_telegram_thread_binding

        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; cannot start session.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                client = await supervisor.get_client(workspace_root)
                session = await client.create_session(directory=str(workspace_root))
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.newt.opencode.session.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Reset branch but failed to start OpenCode session.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not session_id:
                await self._send_message(
                    message.chat_id,
                    "Reset branch but failed to start OpenCode session.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

            def apply_session(record: "TelegramTopicRecord") -> None:
                record.active_thread_id = session_id
                if session_id in record.thread_ids:
                    record.thread_ids.remove(session_id)
                record.thread_ids.insert(0, session_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]

            await self._router.update_topic(
                message.chat_id, message.thread_id, apply_session
            )
            thread_id = session_id
            await _sync_telegram_thread_binding(
                self,
                surface_key=key,
                workspace_root=workspace_root,
                agent=self._effective_runtime_agent(record),
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
                backend_thread_id=thread_id,
                mode="repo",
                pma_enabled=False,
                replace_existing=True,
            )
        else:
            try:
                client = await self._client_for_workspace(str(workspace_root))
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
                    "Reset branch but app server unavailable. Try sending a message.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if client is None:
                await self._send_message(
                    message.chat_id,
                    "Reset branch but workspace unavailable.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                thread = await client.thread_start(
                    str(workspace_root),
                    **self._thread_start_kwargs(record),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.newt.thread_start.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Reset branch but failed to start thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            thread_id = _extract_thread_id(thread)
            if not thread_id:
                await self._send_message(
                    message.chat_id,
                    "Reset branch but failed to start thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._apply_thread_result(
                message.chat_id, message.thread_id, thread, active_thread_id=thread_id
            )
            await _sync_telegram_thread_binding(
                self,
                surface_key=key,
                workspace_root=workspace_root,
                agent=self._effective_runtime_agent(record),
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
                backend_thread_id=thread_id,
                mode="repo",
                pma_enabled=False,
                replace_existing=True,
            )

        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        message_lines = [
            *build_branch_reset_started_lines(
                branch_name=branch_name,
                default_branch=default_branch,
                mode_label="repo",
                actor_label=self._effective_agent_label(record),
                state_label=(
                    "cleared previous thread" if had_previous else "new thread ready"
                ),
                setup_command_count=setup_command_count or None,
            ),
            *build_thread_detail_lines(
                headline=f"Started new thread `{thread_id}`.",
                workspace_path=str(workspace_root),
                actor_label=self._effective_agent_label(record),
                model=record.model or "default",
                effort=effort_label,
            ),
        ]
        await self._send_message(
            message.chat_id,
            "\n".join(message_lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_archive(self, message: TelegramMessage) -> None:
        from .....core.archive import (
            archive_workspace_for_fresh_start,
            resolve_workspace_archive_target,
        )

        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        if bool(record and record.pma_enabled):
            await self._send_message(
                message.chat_id,
                "/archive is not available in PMA mode. Use /new instead.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        workspace_root = self._canonical_workspace_root(record.workspace_path)
        if workspace_root is None:
            await self._send_message(
                message.chat_id,
                "Workspace unavailable.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        manifest_path = (
            self._hub_root / ".codex-autorunner" / "manifest.yml"
            if self._hub_root is not None
            else None
        )
        try:
            target = resolve_workspace_archive_target(
                workspace_root,
                hub_root=self._hub_root,
                manifest_path=manifest_path,
            )
            result = await asyncio.to_thread(
                archive_workspace_for_fresh_start,
                hub_root=self._hub_root,
                base_repo_root=target.base_repo_root,
                base_repo_id=target.base_repo_id,
                worktree_repo_root=workspace_root,
                worktree_repo_id=target.workspace_repo_id,
                branch=None,
                worktree_of=target.worktree_of,
                note="Telegram /archive",
                source_path=target.source_path,
            )
        except ValueError as exc:
            await self._send_message(
                message.chat_id,
                str(exc),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        except Exception as exc:  # intentional: top-level error handler
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.archive_state.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                workspace_path=record.workspace_path,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Archive failed; check logs for details.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        if getattr(self, "_hub_root", None) is not None and record is not None:
            from .execution import _reset_telegram_thread_binding

            try:
                await _reset_telegram_thread_binding(
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
                )
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.archive.managed_thread_reset_failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    workspace_path=str(workspace_root),
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Archive completed, but preparing a fresh managed thread failed.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

        await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            _clear_thread_mirror,
        )
        await self._send_message(
            message.chat_id,
            "\n".join(
                [
                    (
                        f"Archived workspace state to snapshot `{result.snapshot_id}`."
                        if result.snapshot_id
                        else "Workspace CAR state was already clean."
                    ),
                    f"Archived paths: {', '.join(result.archived_paths) or 'none'}",
                    (
                        f"Archived {len(result.archived_thread_ids)} managed thread"
                        f"{'' if len(result.archived_thread_ids) == 1 else 's'}."
                    ),
                    "The binding remains active for fresh work.",
                ]
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

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

                _orchestration_service, managed_thread = (
                    await _resolve_telegram_managed_thread(
                        self,
                        surface_key=key,
                        workspace_root=workspace_root,
                        agent=self._effective_runtime_agent(record),
                        agent_profile=self._effective_agent_profile(record),
                        repo_id=(
                            record.repo_id.strip()
                            if isinstance(record.repo_id, str)
                            and record.repo_id.strip()
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

    async def _handle_status(
        self, message: TelegramMessage, _args: str = "", runtime: Optional[Any] = None
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.ensure_topic(message.chat_id, message.thread_id)
        await self._refresh_workspace_id(key, record)
        if runtime is None:
            runtime = self._router.runtime_for(key)
        approval_policy, sandbox_policy = self._effective_policies(record)
        agent = self._effective_agent(record)
        is_pma = bool(getattr(record, "pma_enabled", False))
        command_policy, plain_text_policy = evaluate_collaboration_summary(
            self,
            message,
            command_text="/status",
        )
        lines = _telegram_status_base_lines(
            message=message,
            record=record,
            runtime=runtime,
            command_policy=command_policy,
            plain_text_policy=plain_text_policy,
        )
        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        rate_limits = await self._read_rate_limits(record.workspace_path, agent=agent)
        lines.extend(
            build_status_block_lines(
                StatusBlockContext(
                    agent=agent,
                    resume=(
                        "supported"
                        if self._agent_supports_resume(agent)
                        else "unsupported"
                    ),
                    model=record.model or "default",
                    effort=effort_label,
                    approval_mode=record.approval_mode,
                    approval_policy=approval_policy or "default",
                    sandbox_policy=sandbox_policy,
                    rate_limits=rate_limits,
                )
            )
        )
        pending = await self._store.pending_approvals_for_key(key)
        if pending:
            lines.append(f"Pending approvals: {len(pending)}")
            if len(pending) == 1:
                age = _approval_age_seconds(pending[0].created_at)
                age_label = f"{age}s" if isinstance(age, int) else "unknown age"
                lines.append(f"Pending request: {pending[0].request_id} ({age_label})")
            else:
                preview = ", ".join(item.request_id for item in pending[:3])
                suffix = "" if len(pending) <= 3 else "..."
                lines.append(f"Pending requests: {preview}{suffix}")
        if record.summary:
            lines.append(f"Summary: {record.summary}")
        if record.active_thread_id:
            token_usage = self._token_usage_by_thread.get(record.active_thread_id)
            lines.extend(_format_token_usage(token_usage))

        manifest_path = getattr(self, "_manifest_path", None)
        hub_root = getattr(self, "_hub_root", None)
        if is_pma:
            if hub_root:
                lines.append(f"Hub root: {hub_root}")
            if manifest_path:
                lines.append(f"Manifest: {manifest_path}")
            registry = getattr(self, "_hub_thread_registry", None)
            if registry and hasattr(self, "_pma_registry_key"):
                try:
                    pma_key = self._pma_registry_key(record, message)
                    pma_thread_id = registry.get_thread_id(pma_key) if pma_key else None
                    # Show thread scoping
                    require_topics = getattr(self._config, "require_topics", False)
                    scoping = "per-topic" if require_topics else "global (per hub)"
                    lines.append(f"PMA thread: {pma_thread_id or 'none'} ({scoping})")
                except (OSError, RuntimeError, ValueError, KeyError):
                    self._logger.debug(
                        "status: pma registry lookup failed", exc_info=True
                    )
            if hub_root:
                try:
                    pma_dir = hub_root / ".codex-autorunner" / "pma"
                    inbox_dir = pma_dir / "inbox"
                    outbox_dir = pma_dir / "outbox"
                    inbox_count = (
                        len(
                            [
                                path
                                for path in inbox_dir.iterdir()
                                if path.is_file() and not path.name.startswith(".")
                            ]
                        )
                        if inbox_dir.exists()
                        else 0
                    )
                    outbox_count = (
                        len(
                            [
                                path
                                for path in outbox_dir.iterdir()
                                if path.is_file() and not path.name.startswith(".")
                            ]
                        )
                        if outbox_dir.exists()
                        else 0
                    )
                    lines.append(
                        f"PMA files: inbox {inbox_count}, outbox {outbox_count}"
                    )
                except OSError:
                    self._logger.debug("status: pma file count failed", exc_info=True)
        if is_pma and manifest_path and hub_root:
            try:
                manifest = load_manifest(manifest_path, hub_root)
                enabled_repos = [repo for repo in manifest.repos if repo.enabled]
                lines.append(
                    f"Hub repos: {len(enabled_repos)}/{len(manifest.repos)} enabled"
                )
                active_count = 0
                paused_count = 0
                idle_count = 0
                active_repos: list[str] = []
                paused_repos: list[str] = []
                for repo in manifest.repos:
                    if not repo.enabled:
                        continue
                    repo_root = (hub_root / repo.path).resolve()
                    db_path = repo_root / ".codex-autorunner" / "flows.db"
                    if not db_path.exists():
                        idle_count += 1
                        continue

                    store = FlowStore(db_path)
                    try:
                        store.initialize()
                        runs = store.list_flow_runs(flow_type="ticket_flow")
                        if runs:
                            latest = runs[0]
                            if latest.status.is_active():
                                active_count += 1
                                active_repos.append(repo.id)
                            elif latest.status == FlowRunStatus.PAUSED:
                                paused_count += 1
                                paused_repos.append(repo.id)
                            else:
                                idle_count += 1
                        else:
                            idle_count += 1
                    except (OSError, RuntimeError, ValueError):
                        self._logger.debug(
                            "status: flow store query failed for repo", exc_info=True
                        )
                    finally:
                        store.close()
                lines.append(
                    f"Hub flows: {active_count} active, {paused_count} paused, {idle_count} idle"
                )
                if active_repos:
                    preview = ", ".join(active_repos[:5])
                    suffix = "" if len(active_repos) <= 5 else "..."
                    lines.append(f"Active repos: {preview}{suffix}")
                if paused_repos:
                    preview = ", ".join(paused_repos[:5])
                    suffix = "" if len(paused_repos) <= 5 else "..."
                    lines.append(f"Paused repos: {preview}{suffix}")
            except Exception:  # intentional: best-effort
                self._logger.debug(
                    "status: hub repo status aggregation failed", exc_info=True
                )

        if not record.workspace_path and not is_pma:
            lines.append("Use /bind <repo_id> or /bind <path>.")

        if record.workspace_path and not is_pma:
            repo_root = Path(record.workspace_path)
            db_path = repo_root / ".codex-autorunner" / "flows.db"
            if db_path.exists():
                store = FlowStore(db_path)
                try:
                    store.initialize()
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                    if runs:
                        latest = runs[0]
                        if latest.status.is_active():
                            lines.append(
                                f"Active Flow: {latest.status.value} (run {latest.id})"
                            )
                        elif latest.status == FlowRunStatus.PAUSED:
                            lines.append(f"Active Flow: PAUSED (run {latest.id})")
                except (OSError, RuntimeError, ValueError):
                    self._logger.debug(
                        "status: flow store query failed for workspace", exc_info=True
                    )
                finally:
                    store.close()

        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
