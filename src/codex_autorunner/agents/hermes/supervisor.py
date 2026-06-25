from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from os.path import basename
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Literal,
    Mapping,
    Optional,
    Sequence,
)

from ...core.acp_lifecycle import (
    active_turn_matches as _active_turn_matches,
)
from ...core.acp_lifecycle import (
    should_close_turn_buffer as _should_close_acp_turn_buffer,
)
from ...core.config import HubConfig, RepoConfig
from ...core.logging_utils import log_event
from ...core.orchestration.assistant_output_assembly import (
    AssistantOutputAssembler,
    AssistantOutputEvent,
)
from ...core.orchestration.turn_event_buffer import TurnEventBuffer
from ...core.text_utils import _normalize_optional_text
from ...core.time_utils import now_iso
from ...core.utils import resolve_executable
from ...workspace import canonical_workspace_root
from ..acp import (
    ACPAdvertisedCommand,
    ACPMessageEvent,
    ACPOutputDeltaEvent,
    ACPPermissionRequestEvent,
    ACPPromptHandle,
    ACPSessionCapabilities,
    ACPSubprocessSupervisor,
    ACPTurnTerminalEvent,
)
from ..types import TerminalTurnResult

_logger = logging.getLogger(__name__)

HERMES_RUNTIME_ID = "hermes"


@dataclass(frozen=True)
class RuntimePreflightResult:
    runtime_id: str
    status: str
    version: Optional[str]
    launch_mode: Optional[str]
    message: str
    fix: str


HERMES_ACP_COMMAND = "acp"
HERMES_APPROVAL_TIMEOUT_SECONDS = 300.0


def _prepend_path_entries(entries: Sequence[str], path: str) -> str:
    merged: list[str] = []
    for value in entries:
        if value and value not in merged:
            merged.append(value)
    for value in path.split(os.pathsep):
        if value and value not in merged:
            merged.append(value)
    return os.pathsep.join(merged)


def _hermes_launch_path_entries(command: Sequence[str]) -> list[str]:
    if not command:
        return []
    binary = str(command[0] or "").strip()
    if not binary:
        return []
    resolved = resolve_executable(binary)
    candidate: Optional[Path] = Path(resolved) if resolved else None
    if candidate is None:
        raw_candidate = Path(binary).expanduser()
        if raw_candidate.is_absolute() and raw_candidate.exists():
            candidate = raw_candidate
    if candidate is None or not candidate.exists():
        return []
    return [str(candidate.parent)]


def _build_hermes_base_env(
    command: Sequence[str],
    *,
    base_env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    extra_paths = _hermes_launch_path_entries(command)
    if not base_env and not extra_paths:
        return {}
    env = os.environ.copy()
    if base_env:
        env.update({str(key): str(value) for key, value in base_env.items()})
    if extra_paths:
        env["PATH"] = _prepend_path_entries(extra_paths, env.get("PATH", ""))
    return env


def _extract_session_summary(payload: Mapping[str, Any]) -> Optional[str]:
    for key in ("summary", "subtitle", "description"):
        value = _normalize_optional_text(payload.get(key))
        if value:
            return value
    return None


def _extract_acp_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def _assistant_output_event_from_acp_notification(
    raw_event: Mapping[str, Any],
) -> Optional[AssistantOutputEvent]:
    method = str(raw_event.get("method") or "").strip()
    params = raw_event.get("params")
    if not isinstance(params, Mapping):
        return None
    turn_id = _normalize_optional_text(params.get("turnId") or params.get("turn_id"))
    if method == "session/update":
        update = params.get("update")
        if not isinstance(update, Mapping):
            return None
        if (
            _normalize_optional_text(
                update.get("sessionUpdate") or update.get("session_update")
            )
            != "agent_message_chunk"
        ):
            return None
        text = _extract_acp_content_text(update.get("content"))
        if not text:
            return None
        raw_kind = str(params.get("assistantOutputKind") or "").strip()
        kind: Literal["delta", "snapshot"]
        if raw_kind == "delta":
            kind = "delta"
        else:
            kind = "snapshot"
        return AssistantOutputEvent(
            kind=kind,
            text=text,
            scope=turn_id,
            preserve_word_boundaries=kind == "snapshot",
        )
    if method in {"prompt/message", "turn/message"}:
        message_text = _normalize_optional_text(
            params.get("message") or params.get("text")
        )
        if not message_text:
            return None
        return AssistantOutputEvent(
            kind="final_message", text=message_text, scope=turn_id
        )
    return None


async def _assistant_text_from_turn_events(
    raw_events: Sequence[Mapping[str, Any]],
) -> str:
    """Reduce only this turn's ACP stream into assistant text.

    Hermes terminal `finalOutput` may be transcript-level for a durable session.
    `session/update` chunks are scoped to the active prompt, so they are the
    safer current-turn source when present.
    """

    assembler = AssistantOutputAssembler()
    for raw_event in raw_events:
        event = _assistant_output_event_from_acp_notification(raw_event)
        if event is not None and event.kind in {"delta", "snapshot"}:
            assembler.note(event)
    return assembler.stream_text.strip()


def _text_without_whitespace(value: str) -> str:
    return "".join(str(value or "").split())


def _terminal_text_looks_cumulative(final_text: str, stream_text: str) -> bool:
    final_compact = _text_without_whitespace(final_text)
    stream_compact = _text_without_whitespace(stream_text)
    return bool(
        final_compact
        and stream_compact
        and final_compact.endswith(stream_compact)
        and final_compact != stream_compact
    )


def _formatted_current_turn_output(
    *,
    final_output: str,
    stream_output: str,
) -> str:
    """Prefer terminal formatting while keeping only the current streamed turn.

    Hermes ACP exposes two imperfect views: `session/update` chunks are scoped
    to the active turn but may be tokenizer fragments with poor spacing, while
    terminal `finalOutput` preserves formatting but may include prior transcript
    text. The compact suffix match lets the stream prove the current-turn scope
    before we trim terminal formatting. When it cannot prove scope, keep
    terminal formatting rather than returning damaged stream text.
    """

    final_text = str(final_output or "")
    stream_text = str(stream_output or "")
    if not final_text.strip():
        return stream_text.strip()
    if not stream_text.strip():
        return final_text.strip()
    final_stripped = final_text.strip()
    stream_stripped = stream_text.strip()
    if final_stripped == stream_stripped:
        return final_stripped

    stream_compact = _text_without_whitespace(stream_stripped)
    if not stream_compact:
        return final_stripped
    compact_suffix_reversed: list[str] = []
    target_length = len(stream_compact)
    for index in range(len(final_text) - 1, -1, -1):
        char = final_text[index]
        if char.isspace():
            continue
        compact_suffix_reversed.append(char)
        if len(compact_suffix_reversed) > target_length:
            break
        if (
            len(compact_suffix_reversed) == target_length
            and "".join(reversed(compact_suffix_reversed)) == stream_compact
        ):
            return final_text[index:].strip()
    if _terminal_text_looks_cumulative(final_stripped, stream_stripped):
        return stream_stripped
    if stream_stripped and "\n\n" in final_stripped:
        prior_turn, terminal_tail = final_stripped.rsplit("\n\n", 1)
        prior_turn_compact = _text_without_whitespace(prior_turn)
        if (
            terminal_tail.strip()
            and prior_turn_compact
            and prior_turn_compact not in stream_compact
            and not prior_turn_compact.startswith(stream_compact)
        ):
            return terminal_tail.strip()
    return final_stripped


def _replace_session_update_content_text(content: Any, text: str) -> Any:
    if isinstance(content, dict):
        updated = dict(content)
        updated["text"] = text
        return updated
    return {"type": "text", "text": text}


def _canonical_acp_notification(event: Any) -> Optional[dict[str, Any]]:
    raw_notification = getattr(event, "raw_notification", None)
    if not isinstance(raw_notification, dict):
        return None
    payload = dict(raw_notification)
    params = dict(payload.get("params") or {})
    payload["params"] = params
    method = str(payload.get("method") or "").strip()
    if isinstance(event, ACPOutputDeltaEvent):
        params["assistantOutputKind"] = event.assembly_kind or "delta"
        if method == "session/update":
            update = dict(params.get("update") or {})
            update["content"] = _replace_session_update_content_text(
                update.get("content"),
                event.delta,
            )
            params["update"] = update
        else:
            params["delta"] = event.delta
            params["text"] = event.delta
    elif isinstance(event, ACPMessageEvent):
        params["assistantOutputKind"] = event.assembly_kind or "final_message"
        params["message"] = event.message
        params["text"] = event.message
    elif isinstance(event, ACPTurnTerminalEvent):
        params["assistantOutputKind"] = event.assembly_kind or "final_message"
        params["finalOutput"] = event.final_output
    return payload


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
    closed: bool = False


@dataclass(frozen=True)
class _HermesLifecycleSnapshot:
    runtime_kind: str
    server_scope: str
    handle_id: str
    workspace_root: str
    pid: Optional[int]
    pgid: Optional[int]
    base_url: Optional[str]
    active_prompts: int
    started: bool
    healthy: bool
    last_used_at: float
    state_dir: Optional[str]


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
        self._base_env = _build_hermes_base_env(
            self._command,
            base_env=base_env,
        )
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
            base_env=self._base_env,
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

    @property
    def launch_env(self) -> dict[str, str]:
        """Environment passed to the ACP subprocess (PATH additions for profile wrappers)."""
        return dict(self._base_env)

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
            payload = _canonical_acp_notification(event)
            if payload is not None:
                await self._append_raw_event(
                    state,
                    payload,
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
        workspace = _workspace_key(workspace_root)
        started_at = time.monotonic()
        state = await self._require_turn_state(workspace_root, resolved_turn_id)
        try:
            result = await state.handle.wait(timeout=timeout)
        except asyncio.TimeoutError:
            log_event(
                self._logger,
                logging.WARNING,
                "hermes.turn.wait_timeout",
                workspace_root=workspace,
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
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "hermes.turn.wait_error",
                workspace_root=workspace,
                session_id=session_id,
                turn_id=resolved_turn_id,
                elapsed_ms=_elapsed_ms(started_at),
                last_event_method=state.last_event_method,
                last_runtime_method=state.last_event_method,
                last_session_update_kind=state.last_session_update_kind,
                last_progress_at=state.last_progress_at,
                detail=str(exc) or exc.__class__.__name__,
            )
            raise
        await self._sync_prompt_snapshot_into_event_buffer(workspace_root, state)
        await state.event_buffer.close()
        async with self._lock:
            state.closed = True
            if _active_turn_matches(
                active_turns=self._session_turns,
                session_id=(workspace, session_id),
                turn_id=resolved_turn_id,
            ):
                self._session_turns.pop((workspace, session_id), None)
        errors = [result.error_message] if result.error_message else []
        raw_events = state.event_buffer.snapshot()
        assistant_text = result.final_output
        try:
            stream_assistant_text = await _assistant_text_from_turn_events(raw_events)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "hermes.turn.stream_output_reduce_failed",
                workspace_root=workspace,
                session_id=session_id,
                turn_id=resolved_turn_id,
                exc=exc,
            )
        else:
            if stream_assistant_text:
                assistant_text = _formatted_current_turn_output(
                    final_output=result.final_output,
                    stream_output=stream_assistant_text,
                )
        log_event(
            self._logger,
            logging.INFO,
            "hermes.turn.completed",
            workspace_root=workspace,
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
            assistant_text=assistant_text,
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

    async def list_turn_events_snapshot(
        self,
        turn_id: str,
        *,
        after_id: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            for (_, state_turn_id), state in self._turn_states.items():
                if state_turn_id == turn_id:
                    return state.event_buffer.snapshot(after_id=after_id, limit=limit)
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

    async def lifecycle_snapshot(self) -> tuple[Any, ...]:
        acp_snapshots = await self._acp.lifecycle_snapshot()
        async with self._lock:
            active_by_workspace: dict[str, int] = {}
            for workspace, _turn_id in self._turn_states:
                active_by_workspace[workspace] = (
                    active_by_workspace.get(workspace, 0) + 1
                )
        return tuple(
            _HermesLifecycleSnapshot(
                runtime_kind="hermes",
                server_scope="workspace",
                handle_id=snapshot.handle_id,
                workspace_root=snapshot.workspace_root,
                pid=snapshot.pid,
                pgid=snapshot.pgid,
                base_url=snapshot.base_url,
                active_prompts=active_by_workspace.get(snapshot.workspace_root, 0),
                started=snapshot.started,
                healthy=snapshot.healthy,
                last_used_at=snapshot.last_used_at,
                state_dir=snapshot.state_dir,
            )
            for snapshot in acp_snapshots
        )

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
            tracked_state = (
                self._turn_states.get((workspace, tracked_turn_id))
                if tracked_turn_id
                else None
            )
            if (
                tracked_turn_id is not None
                and tracked_state is not None
                and not tracked_state.closed
            ):
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
            payload = _canonical_acp_notification(event)
            if payload is None:
                continue
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
        payload = _canonical_acp_notification(event)
        if payload is None:
            return
        state = await self._wait_for_turn_state(workspace_root, turn_id)
        if state is None:
            return
        await self._append_raw_event(
            state,
            payload,
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
    method = getattr(event, "method", None)
    payload = getattr(event, "payload", None)
    if not isinstance(method, str) or not isinstance(payload, Mapping):
        return False
    return _should_close_acp_turn_buffer(method, payload)


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
        normalized_agent_id == HERMES_RUNTIME_ID
        and normalized_profile is not None
        and configured_binary
    ):
        base_binary = _configured_hermes_binary(config, agent_id=HERMES_RUNTIME_ID)
        if base_binary and configured_binary == base_binary:
            return [
                configured_binary,
                "-p",
                normalized_profile,
                HERMES_ACP_COMMAND,
            ], configured_binary
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
        fix="",
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
