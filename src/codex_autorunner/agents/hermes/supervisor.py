from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Optional, Sequence

from ...core.config import HubConfig, RepoConfig
from ...core.utils import resolve_executable
from ...workspace import canonical_workspace_root
from ..acp import (
    ACPPermissionRequestEvent,
    ACPPromptHandle,
    ACPSubprocessSupervisor,
    ACPTurnTerminalEvent,
)
from ..managed_runtime import RuntimeLaunchMode, RuntimePreflightResult
from ..types import TerminalTurnResult

_logger = logging.getLogger(__name__)

HERMES_RUNTIME_ID = "hermes"
HERMES_ACP_COMMAND = "acp"
HERMES_SESSION_STATE_FILE_MODE: RuntimeLaunchMode = "session_state_file"
HERMES_APPROVAL_TIMEOUT_SECONDS = 300.0


class HermesSupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class HermesSessionHandle:
    session_id: str
    title: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


HermesApprovalHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class _HermesTurnState:
    session_id: str
    turn_id: str
    handle: ACPPromptHandle
    approval_mode: Optional[str] = None
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    pending_approval_task: Optional[asyncio.Future[Any]] = None
    stream_condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    stream_closed: bool = False


class HermesSupervisor:
    """Thin Hermes wrapper over the generic ACP subprocess supervisor."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        base_env: Optional[Mapping[str, str]] = None,
        initialize_params: Optional[dict[str, Any]] = None,
        request_timeout: Optional[float] = None,
        approval_handler: Optional[HermesApprovalHandler] = None,
        default_approval_decision: str = "cancel",
        approval_timeout_seconds: float = HERMES_APPROVAL_TIMEOUT_SECONDS,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not command:
            raise ValueError("Hermes command must not be empty")
        self._logger = logger or logging.getLogger(__name__)
        self._approval_handler = approval_handler
        self._default_approval_decision = _normalize_approval_decision(
            default_approval_decision,
            default="cancel",
        )
        self._approval_timeout_seconds = max(
            float(approval_timeout_seconds or 0.0), 0.0
        )
        self._acp = ACPSubprocessSupervisor(
            command,
            base_env=dict(base_env or {}),
            initialize_params=dict(initialize_params or {}),
            request_timeout=request_timeout,
            notification_handler=self._handle_acp_event,
            permission_handler=self._handle_permission_request,
            logger=self._logger,
        )
        self._turn_states: dict[tuple[str, str], _HermesTurnState] = {}
        self._session_turns: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._acp.get_client(workspace_root)

    async def create_session(
        self,
        workspace_root: Path,
        *,
        title: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> HermesSessionHandle:
        session = await self._acp.create_session(
            workspace_root,
            title=title,
            metadata=metadata,
        )
        return HermesSessionHandle(
            session_id=session.session_id,
            title=session.title,
            raw=dict(session.raw),
        )

    async def resume_session(
        self,
        workspace_root: Path,
        session_id: str,
    ) -> HermesSessionHandle:
        session = await self._acp.load_session(workspace_root, session_id)
        return HermesSessionHandle(
            session_id=session.session_id,
            title=session.title,
            raw=dict(session.raw),
        )

    async def list_sessions(self, workspace_root: Path) -> list[HermesSessionHandle]:
        sessions = await self._acp.list_sessions(workspace_root)
        return [
            HermesSessionHandle(
                session_id=session.session_id,
                title=session.title,
                raw=dict(session.raw),
            )
            for session in sessions
        ]

    async def start_turn(
        self,
        workspace_root: Path,
        session_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        approval_mode: Optional[str] = None,
    ) -> str:
        workspace = _workspace_key(workspace_root)
        handle = await self._acp.start_prompt(
            workspace_root,
            session_id,
            prompt,
            model=_normalize_optional_text(model),
        )
        previous_state: Optional[_HermesTurnState] = None
        async with self._lock:
            previous_turn_id = self._session_turns.get((workspace, session_id))
            if previous_turn_id:
                previous_state = self._turn_states.pop(
                    (workspace, previous_turn_id),
                    None,
                )
            state = _HermesTurnState(
                session_id=session_id,
                turn_id=handle.turn_id,
                handle=handle,
                approval_mode=_normalize_optional_text(approval_mode),
            )
            self._turn_states[(workspace, handle.turn_id)] = state
            self._session_turns[(workspace, session_id)] = handle.turn_id
        if previous_state is not None:
            await self._retire_turn_state(previous_state)
        for event in await self._acp.prompt_events_snapshot(
            workspace_root, handle.turn_id
        ):
            raw_notification = getattr(event, "raw_notification", None)
            if isinstance(raw_notification, dict):
                await self._append_raw_event(
                    state,
                    dict(raw_notification),
                    terminal=isinstance(event, ACPTurnTerminalEvent),
                )
        return handle.turn_id

    async def wait_for_turn(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        resolved_turn_id = await self._resolve_turn_id(
            workspace_root,
            session_id,
            turn_id,
        )
        state = await self._require_turn_state(workspace_root, resolved_turn_id)
        result = await state.handle.wait(timeout=timeout)
        errors = [result.error_message] if result.error_message else []
        raw_events = list(state.raw_events)
        return TerminalTurnResult(
            status=result.status,
            assistant_text=result.final_output,
            errors=errors,
            raw_events=raw_events,
        )

    async def stream_turn_events(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        resolved_turn_id = await self._resolve_turn_id(
            workspace_root,
            session_id,
            turn_id,
        )
        state = await self._require_turn_state(workspace_root, resolved_turn_id)
        next_index = 0
        while True:
            async with state.stream_condition:
                while next_index >= len(state.raw_events) and not state.stream_closed:
                    await state.stream_condition.wait()
                pending = list(state.raw_events[next_index:])
                next_index += len(pending)
                should_stop = state.stream_closed and next_index >= len(
                    state.raw_events
                )
            for event in pending:
                yield dict(event)
            if should_stop:
                break

    async def interrupt_turn(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: Optional[str],
    ) -> None:
        resolved_turn_id = await self._resolve_turn_id(
            workspace_root,
            session_id,
            turn_id,
        )
        state = await self._require_turn_state(workspace_root, resolved_turn_id)
        async with self._lock:
            pending_task = state.pending_approval_task
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass
            except Exception:
                self._logger.debug(
                    "Hermes approval task raised while cancelling turn %s",
                    resolved_turn_id,
                    exc_info=True,
                )
        await self._acp.cancel_prompt(workspace_root, session_id, resolved_turn_id)

    async def close_workspace(self, workspace_root: Path) -> None:
        workspace = _workspace_key(workspace_root)
        retired_states: list[_HermesTurnState] = []
        async with self._lock:
            for (state_workspace, _turn_id), state in list(self._turn_states.items()):
                if state_workspace == workspace:
                    retired_states.append(state)
            self._turn_states = {
                key: value
                for key, value in self._turn_states.items()
                if key[0] != workspace
            }
            self._session_turns = {
                key: value
                for key, value in self._session_turns.items()
                if key[0] != workspace
            }
        for state in retired_states:
            await self._retire_turn_state(state)
        await self._acp.close_workspace(workspace_root)

    async def close_all(self) -> None:
        retired_states: list[_HermesTurnState] = []
        async with self._lock:
            retired_states = list(self._turn_states.values())
            self._turn_states.clear()
            self._session_turns.clear()
        for state in retired_states:
            await self._retire_turn_state(state)
        await self._acp.close_all()

    async def _resolve_turn_id(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: Optional[str],
    ) -> str:
        normalized_turn_id = _normalize_optional_text(turn_id)
        if normalized_turn_id:
            return normalized_turn_id
        workspace = _workspace_key(workspace_root)
        async with self._lock:
            tracked_turn_id = self._session_turns.get((workspace, session_id))
        if tracked_turn_id:
            return tracked_turn_id
        raise HermesSupervisorError(
            f"No active Hermes turn tracked for session '{session_id}'"
        )

    async def _require_turn_state(
        self,
        workspace_root: Path,
        turn_id: str,
    ) -> _HermesTurnState:
        workspace = _workspace_key(workspace_root)
        async with self._lock:
            state = self._turn_states.get((workspace, turn_id))
        if state is None:
            raise HermesSupervisorError(f"Unknown Hermes turn '{turn_id}'")
        return state

    async def _wait_for_turn_state(
        self,
        workspace_root: Path,
        turn_id: str,
        *,
        timeout: float = 1.0,
    ) -> Optional[_HermesTurnState]:
        deadline = asyncio.get_running_loop().time() + max(timeout, 0.0)
        while True:
            try:
                return await self._require_turn_state(workspace_root, turn_id)
            except HermesSupervisorError:
                if asyncio.get_running_loop().time() >= deadline:
                    return None
                await asyncio.sleep(0.01)

    async def _append_raw_event(
        self,
        state: _HermesTurnState,
        payload: dict[str, Any],
        *,
        terminal: bool = False,
    ) -> None:
        async with state.stream_condition:
            state.raw_events.append(payload)
            if terminal:
                state.stream_closed = True
            state.stream_condition.notify_all()

    async def _retire_turn_state(self, state: _HermesTurnState) -> None:
        async with self._lock:
            pending_task = state.pending_approval_task
            state.pending_approval_task = None
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass
            except Exception:
                self._logger.debug(
                    "Hermes approval task raised during cleanup for turn %s",
                    state.turn_id,
                    exc_info=True,
                )
        async with state.stream_condition:
            state.stream_closed = True
            state.stream_condition.notify_all()

    async def _handle_acp_event(
        self,
        workspace_root: Path,
        event: Any,
    ) -> None:
        turn_id = _normalize_optional_text(getattr(event, "turn_id", None))
        if turn_id is None:
            return
        raw_notification = getattr(event, "raw_notification", None)
        if not isinstance(raw_notification, dict):
            return
        state = await self._wait_for_turn_state(workspace_root, turn_id)
        if state is None:
            return
        await self._append_raw_event(
            state,
            dict(raw_notification),
            terminal=isinstance(event, ACPTurnTerminalEvent),
        )

    async def _handle_permission_request(
        self,
        workspace_root: Path,
        event: ACPPermissionRequestEvent,
    ) -> Any:
        turn_id = _normalize_optional_text(event.turn_id)
        if turn_id is None:
            return "cancel"
        state = await self._wait_for_turn_state(workspace_root, turn_id)
        if state is None:
            return "cancel"
        approval_mode = _normalize_optional_text(state.approval_mode)
        if not _approval_policy_requires_prompt(approval_mode):
            decision = "accept"
            await self._record_approval_decision(
                state,
                event=event,
                decision=decision,
                reason="policy_auto_accept",
            )
            return decision

        request = _build_surface_approval_request(event)
        if self._approval_handler is None:
            decision = self._default_approval_decision
            await self._record_approval_decision(
                state,
                event=event,
                decision=decision,
                reason="default_fallback",
            )
            return decision

        task: asyncio.Future[Any] = asyncio.ensure_future(
            self._approval_handler(request)
        )
        async with self._lock:
            state.pending_approval_task = task
        try:
            if self._approval_timeout_seconds > 0:
                decision = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self._approval_timeout_seconds,
                )
            else:
                decision = await asyncio.shield(task)
        except asyncio.TimeoutError:
            task.cancel()
            decision = "cancel"
            await self._record_approval_decision(
                state,
                event=event,
                decision=decision,
                reason="timeout",
            )
            return decision
        except asyncio.CancelledError:
            decision = "cancel"
            await self._record_approval_decision(
                state,
                event=event,
                decision=decision,
                reason="cancelled",
            )
            return decision
        except Exception:
            task.cancel()
            self._logger.warning(
                "Hermes approval handler raised for session=%s turn=%s request=%s",
                state.session_id,
                state.turn_id,
                event.request_id,
                exc_info=True,
            )
            decision = "cancel"
            await self._record_approval_decision(
                state,
                event=event,
                decision=decision,
                reason="handler_error",
            )
            return decision
        finally:
            async with self._lock:
                if state.pending_approval_task is task:
                    state.pending_approval_task = None

        normalized = _normalize_approval_decision(decision, default="cancel")
        await self._record_approval_decision(
            state,
            event=event,
            decision=normalized,
            reason="handled",
        )
        return normalized

    async def _record_approval_decision(
        self,
        state: _HermesTurnState,
        *,
        event: ACPPermissionRequestEvent,
        decision: str,
        reason: str,
    ) -> None:
        await self._append_raw_event(
            state,
            {
                "method": "permission/decision",
                "params": {
                    "sessionId": state.session_id,
                    "turnId": state.turn_id,
                    "requestId": event.request_id,
                    "decision": decision,
                    "reason": reason,
                    "description": event.description,
                },
            },
        )
        self._logger.info(
            "Hermes approval decision: session=%s turn=%s request=%s decision=%s reason=%s",
            state.session_id,
            state.turn_id,
            event.request_id,
            decision,
            reason,
        )


def _normalize_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalize_approval_decision(value: Any, *, default: str) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return default
    lowered = normalized.lower()
    if lowered in {"accept", "accepted", "allow", "allowed", "approve", "approved"}:
        return "accept"
    if lowered in {"decline", "declined", "deny", "denied", "reject", "rejected"}:
        return "decline"
    if lowered in {"cancel", "cancelled", "canceled", "timeout", "timed_out"}:
        return "cancel"
    return default


def _approval_policy_requires_prompt(value: Optional[str]) -> bool:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return False
    return normalized.lower() not in {"never", "allow", "approved", "approve"}


def _build_surface_approval_request(
    event: ACPPermissionRequestEvent,
) -> dict[str, Any]:
    context = dict(event.context)
    params: dict[str, Any] = {
        "turnId": event.turn_id,
        "threadId": event.session_id,
        "sessionId": event.session_id,
        "requestId": event.request_id,
        "reason": event.description,
        "description": event.description,
        "context": context,
    }
    for key, value in context.items():
        params.setdefault(key, value)
    method = "permission/requested"
    if any(key in context for key in ("command", "tool", "toolCall")):
        method = "item/commandExecution/requestApproval"
    elif any(key in context for key in ("paths", "files", "fileChanges")):
        method = "item/fileChange/requestApproval"
    return {
        "id": event.request_id,
        "method": method,
        "params": params,
    }


def _workspace_key(workspace_root: Path) -> str:
    return str(canonical_workspace_root(workspace_root))


def build_hermes_supervisor_from_config(
    config: RepoConfig | HubConfig,
    *,
    approval_handler: Optional[HermesApprovalHandler] = None,
    default_approval_decision: str = "cancel",
    logger: Optional[logging.Logger] = None,
) -> Optional[HermesSupervisor]:
    try:
        binary = config.agent_binary("hermes").strip()
    except Exception:
        return None
    if not binary:
        return None
    return HermesSupervisor(
        [binary, HERMES_ACP_COMMAND],
        approval_handler=approval_handler,
        default_approval_decision=default_approval_decision,
        logger=logger,
    )


def hermes_binary_available(config: Optional[RepoConfig | HubConfig]) -> bool:
    if config is None:
        return False
    try:
        binary = config.agent_binary("hermes").strip()
    except Exception:
        return False
    if not binary:
        return False
    return resolve_executable(binary) is not None


def hermes_runtime_preflight(
    config: Optional[RepoConfig | HubConfig],
) -> RuntimePreflightResult:
    if config is None:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix="Set agents.hermes.binary in the repo or hub config.",
        )
    try:
        binary = config.agent_binary("hermes").strip()
    except Exception:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix="Set agents.hermes.binary in the repo or hub config.",
        )
    if not binary:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix="Set agents.hermes.binary in the repo or hub config.",
        )
    binary_path = resolve_executable(binary)
    if binary_path is None:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message=f"Hermes binary '{binary}' is not available on PATH.",
            fix="Install Hermes or update agents.hermes.binary to a working executable path.",
        )
    import subprocess

    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        version = None
    try:
        result = subprocess.run(
            [binary, HERMES_ACP_COMMAND, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        help_text = result.stdout + result.stderr
        if result.returncode not in (0, 1) or not help_text.strip():
            return RuntimePreflightResult(
                runtime_id=HERMES_RUNTIME_ID,
                status="incompatible",
                version=version,
                launch_mode=None,
                message="Hermes ACP mode is not supported by this binary.",
                fix="Install a Hermes build that supports the `hermes acp` command.",
            )
        if "--session-state-file" not in help_text:
            return RuntimePreflightResult(
                runtime_id=HERMES_RUNTIME_ID,
                status="incompatible",
                version=version,
                launch_mode=None,
                message="Hermes does not advertise --session-state-file support in `hermes acp --help`.",
                fix="Install a Hermes build that supports durable session state file launches.",
            )
    except Exception as exc:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="incompatible",
            version=version,
            launch_mode=None,
            message=f"Failed to probe Hermes ACP support: {exc}",
            fix="Ensure Hermes binary is executable and supports `hermes acp` command.",
        )
    return RuntimePreflightResult(
        runtime_id=HERMES_RUNTIME_ID,
        status="ready",
        version=version,
        launch_mode=HERMES_SESSION_STATE_FILE_MODE,
        message=f"Hermes {version or 'version unknown'} supports durable ACP sessions.",
    )


__all__ = [
    "HERMES_ACP_COMMAND",
    "HERMES_RUNTIME_ID",
    "HERMES_SESSION_STATE_FILE_MODE",
    "HermesSessionHandle",
    "HermesSupervisor",
    "HermesSupervisorError",
    "build_hermes_supervisor_from_config",
    "hermes_binary_available",
    "hermes_runtime_preflight",
]
