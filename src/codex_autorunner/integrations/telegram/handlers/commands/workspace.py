from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence

from .....agents.opencode.runtime import extract_session_id
from .....core.app_server_threads import PMA_KEY, PMA_OPENCODE_KEY
from .....core.flows import FlowStore
from .....core.flows.models import FlowRunStatus
from .....core.logging_utils import log_event
from .....core.state import now_iso
from .....core.utils import canonicalize_path, resolve_opencode_binary
from .....manifest import load_manifest
from ....app_server.client import (
    CodexAppServerClient,
)
from ...adapter import (
    TelegramCallbackQuery,
    TelegramMessage,
)
from ...config import AppServerUnavailableError
from ...constants import (
    AGENT_PICKER_PROMPT,
    BIND_PICKER_PROMPT,
    DEFAULT_AGENT,
    DEFAULT_AGENT_MODELS,
    DEFAULT_PAGE_SIZE,
    MAX_TOPIC_THREAD_HISTORY,
    RESUME_MISSING_IDS_LOG_LIMIT,
    RESUME_PICKER_PROMPT,
    RESUME_REFRESH_LIMIT,
    THREAD_LIST_MAX_PAGES,
    VALID_AGENT_VALUES,
)
from ...helpers import (
    _approval_age_seconds,
    _coerce_thread_list,
    _extract_first_user_preview,
    _extract_thread_id,
    _extract_thread_info,
    _extract_thread_list_cursor,
    _extract_thread_preview_parts,
    _format_missing_thread_label,
    _format_rate_limits,
    _format_resume_summary,
    _format_sandbox_policy,
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
from .shared import SharedHelpers

if TYPE_CHECKING:
    from ...state import TelegramTopicRecord


def _extract_opencode_session_path(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("directory", "path", "workspace_path", "workspacePath"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    properties = payload.get("properties")
    if isinstance(properties, dict):
        for key in ("directory", "path", "workspace_path", "workspacePath"):
            value = properties.get(key)
            if isinstance(value, str) and value:
                return value
    session = payload.get("session")
    if isinstance(session, dict):
        return _extract_opencode_session_path(session)
    return None


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


class WorkspaceCommands(SharedHelpers):
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
        return None, "Topic not bound. Use /bind <repo_id> or /bind <path>."

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
    ) -> str:
        def apply(record: "TelegramTopicRecord") -> None:
            record.agent = desired
            record.active_thread_id = None
            record.thread_ids.clear()
            record.thread_summaries.clear()
            record.pending_compact_seed = None
            record.pending_compact_seed_thread_id = None
            if not self._agent_supports_effort(desired):
                record.effort = None
            record.model = DEFAULT_AGENT_MODELS.get(desired)

        await self._router.update_topic(chat_id, thread_id, apply)
        if not self._agent_supports_resume(desired):
            return " (resume not supported)"
        return ""

    async def _handle_agent(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        record = await self._router.ensure_topic(message.chat_id, message.thread_id)
        current = self._effective_agent(record)
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        self._agent_options.pop(key, None)
        argv = self._parse_command_args(args)
        if not argv:
            availability = "available"
            if not self._opencode_available():
                availability = "missing binary"
            items = []
            for agent in ("codex", "opencode"):
                if agent not in VALID_AGENT_VALUES:
                    continue
                label = agent
                if agent == current:
                    label = f"{label} (current)"
                if agent == "opencode" and availability != "available":
                    label = f"{label} ({availability})"
                items.append((agent, label))
            state = SelectionState(items=items)
            keyboard = self._build_agent_keyboard(state)
            self._agent_options[key] = state
            self._touch_cache_timestamp("agent_options", key)
            await self._send_message(
                message.chat_id,
                self._selection_prompt(AGENT_PICKER_PROMPT, state),
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=keyboard,
            )
            return
        desired = normalize_agent(argv[0])
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await self._send_message(
                message.chat_id,
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
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
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if client is None:
            await self._send_message(
                message.chat_id,
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if desired == "opencode" and not self._opencode_available():
            await self._send_message(
                message.chat_id,
                "OpenCode binary not found. Install opencode or switch to /agent codex.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if desired == current:
            await self._send_message(
                message.chat_id,
                f"Agent already set to {current}.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        note = await self._apply_agent_change(
            message.chat_id, message.thread_id, desired
        )
        await self._send_message(
            message.chat_id,
            f"Agent set to {desired}{note}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

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
        if record and record.agent in VALID_AGENT_VALUES:
            return record.agent
        return DEFAULT_AGENT

    def _agent_supports_effort(self, agent: str) -> bool:
        return agent == "codex"

    def _agent_supports_resume(self, agent: str) -> bool:
        return agent in ("codex", "opencode")

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
        return await client.request("model/list", list_params)

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
            except Exception:
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
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        try:
            result = await client.thread_resume(thread_id)
        except Exception as exc:
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
        except Exception:
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
                    except Exception:
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
                normalized_agent = normalize_agent(info.get("agent"))
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

        return await self._router.update_topic(chat_id, thread_id, apply)

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
                prompt or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
                prompt or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
            except Exception as exc:
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
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        thread = await client.thread_start(record.workspace_path or "", agent=agent)
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
        except Exception:
            return []
        repo_ids = [repo.id for repo in manifest.repos if repo.enabled]
        return repo_ids

    def _resolve_workspace(self, arg: str) -> Optional[tuple[str, Optional[str]]]:
        arg = (arg or "").strip()
        if not arg:
            return None
        if self._manifest_path and self._hub_root:
            try:
                manifest = load_manifest(self._manifest_path, self._hub_root)
                repo = manifest.get(arg)
                if repo:
                    workspace = canonicalize_path(self._hub_root / repo.path)
                    return str(workspace), repo.id
            except Exception:
                pass
        path = Path(arg)
        if not path.is_absolute():
            path = canonicalize_path(self._config.root / path)
        else:
            try:
                path = canonicalize_path(path)
            except Exception:
                return None
        if path.exists():
            return str(path), None
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
        except Exception:
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
        except Exception as exc:
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
            state = SelectionState(items=items)
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
        workspace_path, resolved_repo_id = resolved
        chat_id, thread_id = _split_topic_key(key)
        scope = self._topic_scope_id(resolved_repo_id, workspace_path)
        await self._router.set_topic_scope(chat_id, thread_id, scope)
        await self._router.bind_topic(
            chat_id,
            thread_id,
            workspace_path,
            repo_id=resolved_repo_id,
            scope=scope,
        )
        workspace_id = self._workspace_id_for_path(workspace_path)

        def apply_bind_updates(record: TelegramTopicRecord) -> None:
            if workspace_id:
                record.workspace_id = workspace_id
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
        workspace_path, repo_id = resolved
        scope = self._topic_scope_id(repo_id, workspace_path)
        await self._router.set_topic_scope(message.chat_id, message.thread_id, scope)
        await self._router.bind_topic(
            message.chat_id,
            message.thread_id,
            workspace_path,
            repo_id=repo_id,
            scope=scope,
        )
        workspace_id = self._workspace_id_for_path(workspace_path)

        def apply_bind_updates(record: TelegramTopicRecord) -> None:
            if workspace_id:
                record.workspace_id = workspace_id
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

    async def _handle_new(self, message: TelegramMessage) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        pma_enabled = bool(record and record.pma_enabled)
        if pma_enabled:
            registry = getattr(self, "_hub_thread_registry", None)
            agent = self._effective_agent(record)
            pma_key = PMA_OPENCODE_KEY if agent == "opencode" else PMA_KEY
            if registry:
                registry.reset_thread(pma_key)
            await self._send_message(
                message.chat_id,
                "PMA session reset. Send a message to start a fresh PMA turn.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
            except Exception as exc:
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
                    "App server unavailable; try again or check logs.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if client is None:
                await self._send_message(
                    message.chat_id,
                    "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            thread = await client.thread_start(record.workspace_path, agent=agent)
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
        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        await self._send_message(
            message.chat_id,
            "\n".join(
                [
                    f"Started new thread {thread_id}.",
                    f"Directory: {record.workspace_path or 'unbound'}",
                    f"Agent: {agent}",
                    f"Model: {record.model or 'default'}",
                    f"Effort: {effort_label}",
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
        state = SelectionState(items=items)
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
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
                    "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
        agent = self._effective_agent(record)
        if not self._agent_supports_resume(agent):
            await self._send_message(
                message.chat_id,
                "Resume is only supported for the codex and opencode agents. Use /agent codex or /agent opencode to switch.",
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
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if client is None:
            await self._send_message(
                message.chat_id,
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
        except Exception as exc:
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
                if button_label:
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
                        if button_label:
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
                    if button_label:
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
        state = SelectionState(items=items, button_labels=button_labels)
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
            except Exception as exc:
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
        chat_id, thread_id_val = _split_topic_key(key)
        self._resume_options.pop(key, None)
        record = await self._router.get_topic(key)
        if record is not None and self._effective_agent(record) == "opencode":
            await self._resume_opencode_thread_by_id(key, thread_id, callback=callback)
            return
        if record is None or not record.workspace_path:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Topic not bound; use /bind before resuming.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
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
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "App server unavailable; try again or check logs.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if client is None:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Topic not bound; use /bind before resuming.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            result = await client.thread_resume(thread_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                topic_key=key,
                thread_id=thread_id,
                exc=exc,
            )
            await self._answer_callback(callback, "Resume failed")
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
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Topic not bound; use /bind before resuming.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if not isinstance(resumed_path, str):
            await self._answer_callback(callback, "Resume aborted")
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
        except Exception:
            await self._answer_callback(callback, "Resume aborted")
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
            await self._answer_callback(callback, "Resume aborted")
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
            await self._answer_callback(callback, "Resume aborted")
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
        updated_record = await self._apply_thread_result(
            chat_id,
            thread_id_val,
            result,
            active_thread_id=thread_id,
            overwrite_defaults=True,
        )
        await self._answer_callback(callback, "Resumed thread")
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
        chat_id, thread_id_val = _split_topic_key(key)
        self._resume_options.pop(key, None)
        record = await self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Topic not bound; use /bind before resuming.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        supervisor = getattr(self, "_opencode_supervisor", None)
        if supervisor is None:
            await self._answer_callback(callback, "Resume aborted")
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
            await self._answer_callback(callback, "Resume aborted")
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
            client = await supervisor.get_client(workspace_root)
            session = await client.get_session(thread_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.opencode.resume.failed",
                topic_key=key,
                thread_id=thread_id,
                exc=exc,
            )
            await self._answer_callback(callback, "Resume failed")
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
            except Exception:
                await self._answer_callback(callback, "Resume aborted")
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
                await self._answer_callback(callback, "Resume aborted")
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
            await self._answer_callback(callback, "Resume aborted")
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
        workspace_label = record.workspace_path or ("hub" if is_pma else "unbound")
        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        lines = []
        if is_pma:
            lines.append("Mode: PMA (hub)")
        lines.extend(
            [
                f"Workspace: {workspace_label}",
                f"Workspace ID: {record.workspace_id or 'unknown'}",
                f"Active thread: {record.active_thread_id or 'none'}",
                f"Active turn: {runtime.current_turn_id or 'none'}",
                f"Agent: {agent}",
                f"Resume: {'supported' if self._agent_supports_resume(agent) else 'unsupported'}",
                f"Model: {record.model or 'default'}",
                f"Effort: {effort_label}",
                f"Approval mode: {record.approval_mode}",
                f"Approval policy: {approval_policy or 'default'}",
                f"Sandbox policy: {_format_sandbox_policy(sandbox_policy)}",
            ]
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
        rate_limits = await self._read_rate_limits(record.workspace_path, agent=agent)
        lines.extend(_format_rate_limits(rate_limits))

        manifest_path = getattr(self, "_manifest_path", None)
        hub_root = getattr(self, "_hub_root", None)
        if is_pma and manifest_path and hub_root:
            try:
                manifest = load_manifest(manifest_path, hub_root)
                enabled_repos = [repo for repo in manifest.repos if repo.enabled]
                lines.append(
                    f"Hub repos: {len(enabled_repos)}/{len(manifest.repos)} enabled"
                )
                active_count = 0
                paused_count = 0
                for repo in manifest.repos:
                    if not repo.enabled:
                        continue
                    repo_root = (hub_root / repo.path).resolve()
                    db_path = repo_root / ".codex-autorunner" / "flows.db"
                    if not db_path.exists():
                        continue

                    store = FlowStore(db_path)
                    try:
                        store.initialize()
                        runs = store.list_flow_runs(flow_type="ticket_flow")
                        if runs:
                            latest = runs[0]
                            if latest.status.is_active():
                                active_count += 1
                            elif latest.status == FlowRunStatus.PAUSED:
                                paused_count += 1
                    except Exception:
                        pass
                    finally:
                        store.close()
                lines.append(f"Hub flows: {active_count} active, {paused_count} paused")
            except Exception:
                pass

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
                except Exception:
                    pass
                finally:
                    store.close()

        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
