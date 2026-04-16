from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .....agents.opencode.runtime import extract_session_id
from .....core.git_utils import reset_branch_from_origin_main  # noqa: F401
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
)
from ...adapter import TelegramMessage
from ...config import AppServerUnavailableError
from ...constants import (
    DEFAULT_AGENT,
    MAX_TOPIC_THREAD_HISTORY,
)
from ...helpers import (
    _extract_thread_id,
    _extract_thread_info,
    _extract_thread_preview_parts,
    _paths_compatible,
    _set_thread_summary,
)
from ...state import APPROVAL_MODE_YOLO, normalize_agent
from .agent_model_utils import (
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
