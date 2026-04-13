from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from os.path import basename
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Optional, Sequence

from ...core.acp_lifecycle import (
    should_close_turn_buffer as _should_close_acp_turn_buffer,
)
from ...core.config import HubConfig, RepoConfig
from ...core.logging_utils import log_event
from ...core.orchestration.turn_event_buffer import TurnEventBuffer
from ...core.text_utils import _normalize_optional_text
from ...core.time_utils import now_iso
from ...core.utils import resolve_executable
from ...workspace import canonical_workspace_root
from ..acp import (
    ACPAdvertisedCommand,
    ACPPermissionRequestEvent,
    ACPPromptHandle,
    ACPSessionCapabilities,
    ACPSubprocessSupervisor,
    ACPTurnTerminalEvent,
)
from ..managed_runtime import RuntimePreflightResult
from ..types import TerminalTurnResult

_logger = logging.getLogger(__name__)

HERMES_RUNTIME_ID = "hermes"
HERMES_ACP_COMMAND = "acp"
HERMES_APPROVAL_TIMEOUT_SECONDS = 300.0


def _extract_session_summary(payload: Mapping[str, Any]) -> Optional[str]:
    for key in ("summary", "subtitle", "description"):
        value = _normalize_optional_text(payload.get(key))
        if value:
            return value
    return None


class HermesSupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class HermesSessionHandle:
    session_id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


HermesApprovalHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class _HermesTurnState:
    session_id: str
    turn_id: str
    handle: ACPPromptHandle
    approval_mode: Optional[str] = None
    pending_approval_task: Optional[asyncio.Future[Any]] = None
    event_buffer: TurnEventBuffer = field(default_factory=TurnEventBuffer)
    last_event_method: Optional[str] = None
    last_session_update_kind: Optional[str] = None
    last_progress_at: Optional[str] = None


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
        self._command = tuple(str(part) for part in command)
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

    @property
    def launch_command(self) -> tuple[str, ...]:
        return self._command

    async def ensure_ready(self, workspace_root: Path) -> None:
        started_at = time.monotonic()
        log_event(
            self._logger,
            logging.INFO,
            "hermes.runtime.ensure_ready_requested",
            workspace_root=_workspace_key(workspace_root),
            launch_command=list(self._command),
        )
        await self._acp.get_client(workspace_root)
        log_event(
            self._logger,
            logging.INFO,
            "hermes.runtime.ready",
            workspace_root=_workspace_key(workspace_root),
            launch_command=list(self._command),
            elapsed_ms=_elapsed_ms(started_at),
        )

    async def create_session(
        self,
        workspace_root: Path,
        *,
        title: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> HermesSessionHandle:
        started_at = time.monotonic()
        log_event(
            self._logger,
            logging.INFO,
            "hermes.session.create_requested",
            workspace_root=_workspace_key(workspace_root),
            title=title,
            launch_command=list(self._command),
        )
        session = await self._acp.create_session(
            workspace_root,
            title=title,
            metadata=metadata,
        )
        log_event(
            self._logger,
            logging.INFO,
            "hermes.session.created",
            workspace_root=_workspace_key(workspace_root),
            session_id=session.session_id,
            title=title,
            launch_command=list(self._command),
            elapsed_ms=_elapsed_ms(started_at),
        )
        return HermesSessionHandle(
            session_id=session.session_id,
            title=session.title,
            summary=_extract_session_summary(session.raw),
            raw=dict(session.raw),
        )

    async def resume_session(
        self,
        workspace_root: Path,
        session_id: str,
    ) -> HermesSessionHandle:
        started_at = time.monotonic()
        log_event(
            self._logger,
            logging.INFO,
            "hermes.session.resume_requested",
            workspace_root=_workspace_key(workspace_root),
            session_id=session_id,
            launch_command=list(self._command),
        )
        session = await self._acp.load_session(workspace_root, session_id)
        log_event(
            self._logger,
            logging.INFO,
            "hermes.session.resumed",
            workspace_root=_workspace_key(workspace_root),
            session_id=session.session_id,
            launch_command=list(self._command),
            elapsed_ms=_elapsed_ms(started_at),
        )
        return HermesSessionHandle(
            session_id=session.session_id,
            title=session.title,
            summary=_extract_session_summary(session.raw),
            raw=dict(session.raw),
        )

    async def list_sessions(self, workspace_root: Path) -> list[HermesSessionHandle]:
        sessions = await self._acp.list_sessions(workspace_root)
        return [
            HermesSessionHandle(
                session_id=session.session_id,
                title=session.title,
                summary=_extract_session_summary(session.raw),
                raw=dict(session.raw),
            )
            for session in sessions
        ]

    async def fork_session(
        self,
        workspace_root: Path,
        session_id: str,
        *,
        title: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> HermesSessionHandle | None:
        started_at = time.monotonic()
        log_event(
            self._logger,
            logging.INFO,
            "hermes.session.fork_requested",
            workspace_root=_workspace_key(workspace_root),
            source_session_id=session_id,
            title=title,
            launch_command=list(self._command),
        )
        result = await self._acp.fork_session(
            workspace_root,
            session_id,
            title=title,
            metadata=metadata,
        )
        if not result.supported:
            log_event(
                self._logger,
                logging.INFO,
                "hermes.session.fork_unsupported",
                workspace_root=_workspace_key(workspace_root),
                source_session_id=session_id,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return None
        handle = HermesSessionHandle(
            session_id=result.session_id or "",
            title=result.title,
            raw=dict(result.raw),
        )
        log_event(
            self._logger,
            logging.INFO,
            "hermes.session.forked",
            workspace_root=_workspace_key(workspace_root),
            source_session_id=session_id,
            forked_session_id=handle.session_id,
            elapsed_ms=_elapsed_ms(started_at),
        )
        return handle

    async def set_session_model(
        self,
        workspace_root: Path,
        session_id: str,
        model_id: str,
    ) -> bool:
        started_at = time.monotonic()
        log_event(
            self._logger,
            logging.INFO,
            "hermes.session.set_model_requested",
            workspace_root=_workspace_key(workspace_root),
            session_id=session_id,
            model_id=model_id,
        )
        result = await self._acp.set_session_model(workspace_root, session_id, model_id)
        log_event(
            self._logger,
            logging.INFO,
            "hermes.session.set_model_completed",
            workspace_root=_workspace_key(workspace_root),
            session_id=session_id,
            model_id=model_id,
            supported=result.supported,
            elapsed_ms=_elapsed_ms(started_at),
        )
        return result.supported

    async def set_session_mode(
        self,
        workspace_root: Path,
        session_id: str,
        mode: str,
    ) -> bool:
        started_at = time.monotonic()
        log_event(
            self._logger,
            logging.INFO,
            "hermes.session.set_mode_requested",
            workspace_root=_workspace_key(workspace_root),
            session_id=session_id,
            mode=mode,
        )
        result = await self._acp.set_session_mode(workspace_root, session_id, mode)
        log_event(
            self._logger,
            logging.INFO,
            "hermes.session.set_mode_completed",
            workspace_root=_workspace_key(workspace_root),
            session_id=session_id,
            mode=mode,
            supported=result.supported,
            elapsed_ms=_elapsed_ms(started_at),
        )
        return result.supported

    async def advertised_commands(
        self,
        workspace_root: Path,
    ) -> list[ACPAdvertisedCommand]:
        return await self._acp.advertised_commands(workspace_root)

    async def session_capabilities(
        self,
        workspace_root: Path,
    ) -> ACPSessionCapabilities:
        return await self._acp.session_capabilities(workspace_root)

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
        started_at = time.monotonic()
        log_event(
            self._logger,
            logging.INFO,
            "hermes.turn.start_requested",
            workspace_root=workspace,
            session_id=session_id,
            approval_mode=_normalize_optional_text(approval_mode),
            model=_normalize_optional_text(model),
            launch_command=list(self._command),
        )
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
                previous_state = self._turn_states.get(
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
            await self._cancel_pending_approval_task(previous_state)
        for event in await self._acp.prompt_events_snapshot(
            workspace_root, handle.turn_id
        ):
            raw_notification = getattr(event, "raw_notification", None)
            if isinstance(raw_notification, dict):
                await self._append_raw_event(
                    state,
                    dict(raw_notification),
                    terminal=_should_close_turn_buffer(event),
                )
        log_event(
            self._logger,
            logging.INFO,
            "hermes.turn.started",
            workspace_root=workspace,
            session_id=session_id,
            turn_id=handle.turn_id,
            approval_mode=_normalize_optional_text(approval_mode),
            model=_normalize_optional_text(model),
            launch_command=list(self._command),
            elapsed_ms=_elapsed_ms(started_at),
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
        started_at = time.monotonic()
        try:
            result = await state.handle.wait(timeout=timeout)
        except asyncio.TimeoutError:
            log_event(
                self._logger,
                logging.WARNING,
                "hermes.turn.wait_timeout",
                workspace_root=_workspace_key(workspace_root),
                session_id=session_id,
                turn_id=resolved_turn_id,
                timeout_seconds=timeout,
                elapsed_ms=_elapsed_ms(started_at),
                last_event_method=state.last_event_method,
                last_runtime_method=state.last_event_method,
                last_session_update_kind=state.last_session_update_kind,
                last_progress_at=state.last_progress_at,
            )
            raise
        await self._sync_prompt_snapshot_into_event_buffer(workspace_root, state)
        await state.event_buffer.close()
        errors = [result.error_message] if result.error_message else []
        raw_events = state.event_buffer.snapshot()
        log_event(
            self._logger,
            logging.INFO,
            "hermes.turn.completed",
            workspace_root=_workspace_key(workspace_root),
            session_id=session_id,
            turn_id=resolved_turn_id,
            status=result.status,
            elapsed_ms=_elapsed_ms(started_at),
            raw_event_count=len(raw_events),
            last_event_method=state.last_event_method,
            last_runtime_method=state.last_event_method,
            last_session_update_kind=state.last_session_update_kind,
            last_progress_at=state.last_progress_at,
            error_message=result.error_message,
        )
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
        async for event in state.event_buffer.tail():
            yield event

    async def list_turn_events_snapshot(self, turn_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            for (_, state_turn_id), state in self._turn_states.items():
                if state_turn_id == turn_id:
                    return state.event_buffer.snapshot()
        return []

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
            except (RuntimeError, TypeError, ValueError, AttributeError, OSError):
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
        state.last_event_method = str(payload.get("method") or "").strip() or None
        state.last_progress_at = now_iso()
        params = payload.get("params")
        if (
            isinstance(params, dict)
            and str(payload.get("method") or "") == "session/update"
        ):
            update = params.get("update")
            if isinstance(update, dict):
                state.last_session_update_kind = _normalize_optional_text(
                    update.get("sessionUpdate") or update.get("session_update")
                )
        await state.event_buffer.append(payload)
        if terminal:
            await state.event_buffer.close()

    async def _sync_prompt_snapshot_into_event_buffer(
        self,
        workspace_root: Path,
        state: _HermesTurnState,
    ) -> None:
        existing_events = state.event_buffer.snapshot()
        prompt_events = await self._acp.prompt_events_snapshot(
            workspace_root, state.turn_id
        )
        for event in prompt_events:
            raw_notification = getattr(event, "raw_notification", None)
            if not isinstance(raw_notification, dict):
                continue
            payload = dict(raw_notification)
            if payload in existing_events:
                continue
            await self._append_raw_event(
                state,
                payload,
                terminal=_should_close_turn_buffer(event),
            )
            existing_events.append(payload)

    async def _retire_turn_state(self, state: _HermesTurnState) -> None:
        await self._cancel_pending_approval_task(state)
        await state.event_buffer.close()

    async def _cancel_pending_approval_task(self, state: _HermesTurnState) -> None:
        async with self._lock:
            pending_task = state.pending_approval_task
            state.pending_approval_task = None
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass
            except (RuntimeError, TypeError, ValueError, AttributeError, OSError):
                self._logger.debug(
                    "Hermes approval task raised during cleanup for turn %s",
                    state.turn_id,
                    exc_info=True,
                )

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
            terminal=_should_close_turn_buffer(event),
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
        except (
            RuntimeError,
            TypeError,
            ValueError,
            AttributeError,
            OSError,
            ConnectionError,
        ):  # intentional: user-provided approval handler may raise arbitrary errors
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


def _should_close_turn_buffer(event: Any) -> bool:
    if not isinstance(event, ACPTurnTerminalEvent):
        return False
    return _should_close_acp_turn_buffer(event.method, event.payload)


def _workspace_key(workspace_root: Path) -> str:
    return str(canonical_workspace_root(workspace_root))


def _elapsed_ms(started_at: float) -> int:
    return max(int((time.monotonic() - started_at) * 1000), 0)


def _configured_hermes_binary(
    config: RepoConfig | HubConfig,
    *,
    agent_id: str,
    profile: Optional[str] = None,
) -> Optional[str]:
    try:
        try:
            return config.agent_binary(agent_id, profile=profile).strip()
        except TypeError as exc:
            if "profile" not in str(exc):
                raise
            return config.agent_binary(agent_id).strip()
    except (
        KeyError,
        AttributeError,
        ValueError,
        TypeError,
        RuntimeError,
    ):  # intentional: config lookup may raise various errors
        return None


def _resolve_hermes_launch(
    config: RepoConfig | HubConfig,
    *,
    agent_id: str,
    profile: Optional[str] = None,
) -> tuple[list[str], str]:
    normalized_agent_id = str(agent_id or "").strip().lower() or HERMES_RUNTIME_ID
    normalized_profile = _normalize_optional_text(profile)
    configured_binary = _configured_hermes_binary(
        config,
        agent_id=normalized_agent_id,
        profile=normalized_profile,
    )
    configured_name = basename(configured_binary) if configured_binary else ""
    if (
        normalized_agent_id != HERMES_RUNTIME_ID
        and normalized_profile is None
        and configured_name == normalized_agent_id
    ):
        base_binary = _configured_hermes_binary(config, agent_id=HERMES_RUNTIME_ID)
        if base_binary:
            return [
                base_binary,
                "-p",
                normalized_agent_id,
                HERMES_ACP_COMMAND,
            ], base_binary
    if configured_binary:
        return [configured_binary, HERMES_ACP_COMMAND], configured_binary
    raise KeyError(normalized_agent_id)


def build_hermes_supervisor_from_config(
    config: RepoConfig | HubConfig,
    *,
    agent_id: str = "hermes",
    profile: Optional[str] = None,
    approval_handler: Optional[HermesApprovalHandler] = None,
    default_approval_decision: str = "cancel",
    logger: Optional[logging.Logger] = None,
) -> Optional[HermesSupervisor]:
    try:
        command, _binary = _resolve_hermes_launch(
            config,
            agent_id=agent_id,
            profile=profile,
        )
    except (
        KeyError,
        AttributeError,
        ValueError,
        TypeError,
        RuntimeError,
    ):  # intentional: config lookup may raise various errors
        return None
    return HermesSupervisor(
        command,
        approval_handler=approval_handler,
        default_approval_decision=default_approval_decision,
        logger=logger,
    )


def hermes_binary_available(
    config: Optional[RepoConfig | HubConfig],
    *,
    agent_id: str = "hermes",
    profile: Optional[str] = None,
) -> bool:
    if config is None:
        return False
    try:
        _command, binary = _resolve_hermes_launch(
            config,
            agent_id=agent_id,
            profile=profile,
        )
    except (KeyError, AttributeError, ValueError, TypeError, RuntimeError):
        return False
    if not binary:
        return False
    return resolve_executable(binary) is not None


def hermes_runtime_preflight(
    config: Optional[RepoConfig | HubConfig],
    *,
    agent_id: str = "hermes",
    profile: Optional[str] = None,
) -> RuntimePreflightResult:
    normalized_agent_id = str(agent_id or "").strip().lower() or HERMES_RUNTIME_ID
    normalized_profile = str(profile or "").strip().lower()
    binary_key = (
        f"agents.{normalized_agent_id}.profiles.{normalized_profile}.binary"
        if normalized_profile
        else f"agents.{normalized_agent_id}.binary"
    )
    if config is None:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix=f"Set {binary_key} in the repo or hub config.",
        )
    try:
        command, binary = _resolve_hermes_launch(
            config,
            agent_id=normalized_agent_id,
            profile=normalized_profile or None,
        )
    except (
        KeyError,
        AttributeError,
        ValueError,
        TypeError,
        RuntimeError,
    ):  # intentional: config lookup may raise various errors
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix=f"Set {binary_key} in the repo or hub config.",
        )
    if not binary:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix=f"Set {binary_key} in the repo or hub config.",
        )
    binary_path = resolve_executable(binary)
    if binary_path is None:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message=f"Hermes binary '{binary}' is not available on PATH.",
            fix=f"Install Hermes or update {binary_key} to a working executable path.",
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
    except (OSError, subprocess.TimeoutExpired):
        version = None
    try:
        result = subprocess.run(
            [*command, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        help_text = result.stdout + result.stderr
        if result.returncode not in (0, 1) or not help_text.strip():
            return RuntimePreflightResult(
                runtime_id=normalized_agent_id,
                status="incompatible",
                version=version,
                launch_mode=None,
                message="Hermes ACP mode is not supported by this binary.",
                fix="Install a Hermes build that supports the `hermes acp` command.",
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="incompatible",
            version=version,
            launch_mode=None,
            message=f"Failed to probe Hermes ACP support: {exc}",
            fix="Ensure Hermes binary is executable and supports `hermes acp` command.",
        )
    return RuntimePreflightResult(
        runtime_id=normalized_agent_id,
        status="ready",
        version=version,
        launch_mode=None,
        message=(
            f"Hermes {version or 'version unknown'} supports ACP mode and "
            "uses Hermes-native durable sessions."
        ),
    )


__all__ = [
    "HERMES_ACP_COMMAND",
    "HERMES_RUNTIME_ID",
    "HermesSessionHandle",
    "HermesSupervisor",
    "HermesSupervisorError",
    "build_hermes_supervisor_from_config",
    "hermes_binary_available",
    "hermes_runtime_preflight",
]
