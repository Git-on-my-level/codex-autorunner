from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from ..acp_lifecycle import (
    analyze_acp_lifecycle_message,
)
from ..acp_lifecycle import (
    extract_error_message as _extract_error_message,
)
from ..acp_lifecycle import (
    extract_message_text as _extract_message_text,
)
from ..acp_lifecycle import (
    extract_output_delta as _extract_output_delta,
)
from ..time_utils import now_iso
from .codex_item_normalizers import (
    extract_agent_message_text as _shared_extract_agent_message_text,
)
from .codex_item_normalizers import (
    extract_codex_usage,
    is_commentary_agent_message,
)
from .codex_item_normalizers import (
    merge_runtime_raw_events as _merge_runtime_raw_events,
)
from .execution_history import ExecutionCheckpoint, ExecutionCheckpointSignal
from .stream_text_merge import merge_assistant_stream_text

RuntimeThreadOutcomeStatus = Literal["ok", "error", "interrupted"]
RuntimeThreadCompletionSource = Literal[
    "interrupt",
    "missing_backend_ids",
    "prompt_return",
    "reconciled_failure",
    "stream_terminal_event",
    "timeout",
    "transport_error",
]
_SUCCESSFUL_COMPLETION_STATUSES = frozenset(
    {"ok", "completed", "complete", "done", "success", "succeeded", "idle"}
)
_INTERRUPTED_COMPLETION_STATUSES = frozenset(
    {"interrupted", "cancelled", "canceled", "aborted"}
)
_DEFAULT_INTERRUPTED_ERROR = "Runtime thread interrupted"


@dataclass(frozen=True)
class RuntimeThreadTerminalSignal:
    source: str
    status: RuntimeThreadOutcomeStatus
    timestamp: str


@dataclass(frozen=True)
class RuntimeThreadOutcome:
    """Collected outcome of one runtime-thread execution before persistence."""

    status: RuntimeThreadOutcomeStatus
    assistant_text: str
    error: Optional[str]
    backend_thread_id: str
    backend_turn_id: Optional[str]
    raw_events: tuple[Any, ...] = ()
    completion_source: RuntimeThreadCompletionSource = "prompt_return"
    terminal_signals: tuple[RuntimeThreadTerminalSignal, ...] = ()
    transport_request_return_timestamp: Optional[str] = None
    last_progress_timestamp: Optional[str] = None
    failure_cause: Optional[str] = None


@dataclass
class _RawEventInspection:
    assistant_message_text: Optional[str] = None
    assistant_stream_text: Optional[str] = None
    failure_message: Optional[str] = None
    terminal_signal: Optional[RuntimeThreadTerminalSignal] = None
    token_usage: Optional[dict[str, Any]] = None
    runtime_method: Optional[str] = None


@dataclass
class RuntimeTurnTerminalStateMachine:
    """Authoritative runtime-turn terminal reconciler for orchestration."""

    backend_thread_id: str
    backend_turn_id: Optional[str]
    last_assistant_text: str = ""
    transport_status: Optional[str] = None
    transport_errors: tuple[str, ...] = ()
    transport_request_return_timestamp: Optional[str] = None
    last_progress_timestamp: Optional[str] = None
    failure_cause: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    last_runtime_method: Optional[str] = None
    raw_events: list[Any] = field(default_factory=list)
    terminal_signals: list[RuntimeThreadTerminalSignal] = field(default_factory=list)
    _terminal_signal_keys: set[tuple[str, RuntimeThreadOutcomeStatus]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _terminal_signal_event: asyncio.Event = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._terminal_signal_event = asyncio.Event()

    def terminal_signal_waiter(self) -> asyncio.Event:
        return self._terminal_signal_event

    def note_raw_event(
        self, raw_event: Any, *, timestamp: Optional[str] = None
    ) -> None:
        event_timestamp = timestamp or now_iso()
        self.raw_events.append(raw_event)
        self.last_progress_timestamp = event_timestamp
        inspection = _inspect_raw_event(raw_event, timestamp=event_timestamp)
        if inspection.runtime_method:
            self.last_runtime_method = inspection.runtime_method
        if inspection.assistant_stream_text:
            self.last_assistant_text = merge_assistant_stream_text(
                self.last_assistant_text,
                inspection.assistant_stream_text,
            )
        if inspection.assistant_message_text:
            self.last_assistant_text = inspection.assistant_message_text
        if inspection.failure_message:
            self.failure_cause = inspection.failure_message
        if inspection.token_usage:
            self.token_usage = dict(inspection.token_usage)
        if inspection.terminal_signal is not None:
            self._note_terminal_signal(inspection.terminal_signal)

    def note_transport_result(
        self,
        result: Any,
        *,
        timestamp: Optional[str] = None,
    ) -> None:
        event_timestamp = timestamp or now_iso()
        self.transport_request_return_timestamp = event_timestamp
        self.transport_status = str(getattr(result, "status", "") or "").strip().lower()
        self.transport_errors = tuple(
            str(error or "").strip()
            for error in (getattr(result, "errors", ()) or ())
            if str(error or "").strip()
        )
        assistant_text = str(getattr(result, "assistant_text", "") or "")
        if assistant_text.strip():
            self.last_assistant_text = assistant_text
        merged_raw_events = _merge_runtime_raw_events(
            self.raw_events,
            list(getattr(result, "raw_events", ()) or ()),
        )
        if len(merged_raw_events) > len(self.raw_events):
            new_events = merged_raw_events[len(self.raw_events) :]
            self.raw_events = merged_raw_events
            for raw_event in new_events:
                inspection = _inspect_raw_event(raw_event, timestamp=event_timestamp)
                if inspection.assistant_stream_text:
                    self.last_assistant_text = merge_assistant_stream_text(
                        self.last_assistant_text,
                        inspection.assistant_stream_text,
                    )
                if inspection.assistant_message_text:
                    self.last_assistant_text = inspection.assistant_message_text
                if inspection.failure_message:
                    self.failure_cause = inspection.failure_message
                if inspection.token_usage:
                    self.token_usage = dict(inspection.token_usage)
                if inspection.terminal_signal is not None:
                    self._note_terminal_signal(inspection.terminal_signal)
        if self.transport_errors and not self.failure_cause:
            self.failure_cause = self.transport_errors[0]

    def build_missing_backend_ids_outcome(self, error: str) -> RuntimeThreadOutcome:
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=error,
            backend_thread_id=self.backend_thread_id,
            backend_turn_id=self.backend_turn_id,
            raw_events=tuple(self.raw_events),
            completion_source="missing_backend_ids",
            terminal_signals=tuple(self.terminal_signals),
            transport_request_return_timestamp=self.transport_request_return_timestamp,
            last_progress_timestamp=self.last_progress_timestamp,
            failure_cause=error,
        )

    def build_timeout_outcome(self, error: str) -> RuntimeThreadOutcome:
        timestamp = now_iso()
        self.failure_cause = error
        self._note_terminal_signal(
            RuntimeThreadTerminalSignal(
                source="timeout",
                status="error",
                timestamp=timestamp,
            )
        )
        return self._build_outcome(
            status="error",
            assistant_text="",
            error=error,
            completion_source="timeout",
        )

    def build_interrupted_outcome(self, error: str) -> RuntimeThreadOutcome:
        timestamp = now_iso()
        self.failure_cause = error
        self._note_terminal_signal(
            RuntimeThreadTerminalSignal(
                source="interrupt",
                status="interrupted",
                timestamp=timestamp,
            )
        )
        return self._build_outcome(
            status="interrupted",
            assistant_text="",
            error=error,
            completion_source="interrupt",
        )

    def build_transport_exception_outcome(
        self,
        error: str,
    ) -> RuntimeThreadOutcome:
        self.failure_cause = error
        if self._saw_successful_terminal_signal() and self.last_assistant_text.strip():
            return self._build_outcome(
                status="ok",
                assistant_text=self.last_assistant_text,
                error=None,
                completion_source="reconciled_failure",
            )
        return self._build_outcome(
            status="error",
            assistant_text="",
            error=error,
            completion_source="transport_error",
        )

    def build_outcome(self, execution_error_message: str) -> RuntimeThreadOutcome:
        status = self.transport_status or ""
        assistant_text = self.last_assistant_text
        detail = next(iter(self.transport_errors), "") or self.failure_cause or None
        successful_transport = status in _SUCCESSFUL_COMPLETION_STATUSES
        interrupted_transport = status in _INTERRUPTED_COMPLETION_STATUSES
        failed_transport = (
            bool(status) and not successful_transport and not interrupted_transport
        )
        successful_terminal = self._saw_successful_terminal_signal()
        latest_terminal = self._latest_terminal_signal()

        if self.transport_request_return_timestamp is None:
            if latest_terminal is not None and latest_terminal.status == "ok":
                return self._build_outcome(
                    status="ok",
                    assistant_text=assistant_text,
                    error=None,
                    completion_source="stream_terminal_event",
                )
            if latest_terminal is not None and latest_terminal.status == "interrupted":
                return self._build_outcome(
                    status="interrupted",
                    assistant_text="",
                    error=detail or _DEFAULT_INTERRUPTED_ERROR,
                    completion_source="stream_terminal_event",
                )
            return self._build_outcome(
                status="error",
                assistant_text="",
                error=detail or execution_error_message,
                completion_source="stream_terminal_event",
            )

        if self.transport_errors:
            if interrupted_transport:
                return self._build_outcome(
                    status="interrupted",
                    assistant_text="",
                    error=detail or _DEFAULT_INTERRUPTED_ERROR,
                    completion_source="prompt_return",
                )
            if successful_terminal and assistant_text.strip():
                return self._build_outcome(
                    status="ok",
                    assistant_text=assistant_text,
                    error=None if successful_transport else detail or None,
                    completion_source=(
                        "reconciled_failure"
                        if not successful_transport
                        else "prompt_return"
                    ),
                )
            if failed_transport:
                return self._build_outcome(
                    status="error",
                    assistant_text="",
                    error=detail or execution_error_message,
                    completion_source="prompt_return",
                )
            if assistant_text.strip():
                return self._build_outcome(
                    status="ok",
                    assistant_text=assistant_text,
                    error=detail or None,
                    completion_source="prompt_return",
                )
            return self._build_outcome(
                status="error",
                assistant_text="",
                error=detail or execution_error_message,
                completion_source="prompt_return",
            )

        if interrupted_transport:
            return self._build_outcome(
                status="interrupted",
                assistant_text="",
                error=self.failure_cause,
                completion_source="interrupt",
            )
        if failed_transport:
            return self._build_outcome(
                status="error",
                assistant_text="",
                error=detail or execution_error_message,
                completion_source="prompt_return",
            )
        return self._build_outcome(
            status="ok",
            assistant_text=assistant_text,
            error=None,
            completion_source="prompt_return",
        )

    def _saw_successful_terminal_signal(self) -> bool:
        return any(signal.status == "ok" for signal in self.terminal_signals)

    def _latest_terminal_signal(self) -> Optional[RuntimeThreadTerminalSignal]:
        if not self.terminal_signals:
            return None
        return self.terminal_signals[-1]

    def _note_terminal_signal(self, signal: RuntimeThreadTerminalSignal) -> None:
        key = (signal.source, signal.status)
        if key in self._terminal_signal_keys:
            return
        self._terminal_signal_keys.add(key)
        self.terminal_signals.append(signal)
        self._terminal_signal_event.set()

    def _build_outcome(
        self,
        *,
        status: RuntimeThreadOutcomeStatus,
        assistant_text: str,
        error: Optional[str],
        completion_source: RuntimeThreadCompletionSource,
    ) -> RuntimeThreadOutcome:
        return RuntimeThreadOutcome(
            status=status,
            assistant_text=assistant_text,
            error=error,
            backend_thread_id=self.backend_thread_id,
            backend_turn_id=self.backend_turn_id,
            raw_events=tuple(self.raw_events),
            completion_source=completion_source,
            terminal_signals=tuple(self.terminal_signals),
            transport_request_return_timestamp=self.transport_request_return_timestamp,
            last_progress_timestamp=self.last_progress_timestamp,
            failure_cause=self.failure_cause,
        )

    def build_checkpoint(
        self,
        *,
        execution_id: Optional[str] = None,
        thread_target_id: Optional[str] = None,
        status: Optional[str] = None,
        completion_source: Optional[str] = None,
        assistant_text: Optional[str] = None,
        projection_event_cursor: int = 0,
        trace_manifest_id: Optional[str] = None,
    ) -> ExecutionCheckpoint:
        effective_text = str(
            self.last_assistant_text if assistant_text is None else assistant_text
        )
        latest_terminal = self._latest_terminal_signal()
        checkpoint_status = (
            str(status or "").strip()
            or str(self.transport_status or "").strip()
            or (latest_terminal.status if latest_terminal is not None else "running")
        )
        return ExecutionCheckpoint(
            status=checkpoint_status or "running",
            execution_id=execution_id,
            thread_target_id=thread_target_id,
            backend_thread_id=self.backend_thread_id or None,
            backend_turn_id=self.backend_turn_id or None,
            completion_source=completion_source,
            assistant_text_preview=_checkpoint_preview(effective_text),
            assistant_char_count=len(effective_text),
            last_runtime_method=self.last_runtime_method,
            last_progress_at=self.last_progress_timestamp,
            transport_status=self.transport_status,
            transport_request_return_timestamp=self.transport_request_return_timestamp,
            token_usage=(
                dict(self.token_usage) if isinstance(self.token_usage, dict) else None
            ),
            failure_cause=self.failure_cause,
            raw_event_count=len(self.raw_events),
            projection_event_cursor=max(int(projection_event_cursor or 0), 0),
            terminal_signals=tuple(
                ExecutionCheckpointSignal(
                    source=signal.source,
                    status=signal.status,
                    timestamp=signal.timestamp,
                )
                for signal in self.terminal_signals
            ),
            trace_manifest_id=trace_manifest_id,
        )


def _inspect_raw_event(
    raw_event: Any,
    *,
    timestamp: str,
) -> _RawEventInspection:
    if not isinstance(raw_event, dict):
        return _RawEventInspection()
    message = raw_event.get("message")
    payload = message if isinstance(message, dict) else raw_event
    method = str(payload.get("method") or "").strip()
    params = payload.get("params")
    if not isinstance(params, dict):
        params = payload if isinstance(payload, dict) else {}
    if not method:
        return _RawEventInspection()

    assistant_message_text = None
    assistant_stream_text = None
    failure_message = None
    terminal_signal = None
    token_usage = _extract_usage(params)
    method_lower = method.lower()
    lifecycle = analyze_acp_lifecycle_message(payload)

    if method in {"message.completed", "message.updated"}:
        role = _extract_message_role(params)
        if role != "user":
            assistant_message_text = _extract_message_text(params)
    elif method in {"prompt/message", "turn/message"}:
        assistant_message_text = lifecycle.assistant_text
    elif lifecycle.runtime_terminal_status is not None:
        assistant_message_text = lifecycle.assistant_text or None
        terminal_signal = RuntimeThreadTerminalSignal(
            source=method,
            status=lifecycle.runtime_terminal_status,
            timestamp=timestamp,
        )
        if lifecycle.runtime_terminal_status in {"error", "interrupted"}:
            failure_message = lifecycle.error_message or _extract_error_message(
                params,
                default="",
            )
    elif method in {"turn/failed", "turn/error", "error"}:
        failure_message = _extract_error_message(params)
        terminal_signal = RuntimeThreadTerminalSignal(
            source=method,
            status="error",
            timestamp=timestamp,
        )
    elif method == "item/completed":
        item = params.get("item")
        if (
            isinstance(item, dict)
            and str(item.get("type") or "").strip() == "agentMessage"
            and not is_commentary_agent_message(item)
        ):
            assistant_message_text = _shared_extract_agent_message_text(item) or None

    if assistant_message_text is None and (
        method
        in {
            "prompt/output",
            "prompt/delta",
            "prompt/progress",
            "turn/progress",
            "item/agentMessage/delta",
            "message.delta",
            "turn/streamDelta",
        }
        or "outputdelta" in method_lower
    ):
        assistant_stream_text = _extract_output_delta(params)
    if assistant_stream_text is None and method == "session/update":
        update = params.get("update")
        if isinstance(update, dict):
            update_kind = str(lifecycle.session_update_kind or "").strip()
            if update_kind == "agent_message_chunk":
                assistant_stream_text = _extract_output_delta(update)

    return _RawEventInspection(
        assistant_message_text=assistant_message_text,
        assistant_stream_text=assistant_stream_text,
        failure_message=failure_message,
        terminal_signal=terminal_signal,
        token_usage=token_usage,
        runtime_method=method,
    )


def _extract_usage(params: dict[str, Any]) -> Optional[dict[str, Any]]:
    return extract_codex_usage(params)


def _checkpoint_preview(value: str, limit: int = 240) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _extract_message_role(params: dict[str, Any]) -> str:
    role = params.get("role")
    if isinstance(role, str):
        return role.strip().lower()
    message = params.get("message")
    if isinstance(message, dict):
        role = message.get("role")
        if isinstance(role, str):
            return role.strip().lower()
    return ""


__all__ = [
    "ExecutionCheckpoint",
    "RuntimeThreadCompletionSource",
    "RuntimeThreadOutcome",
    "RuntimeThreadOutcomeStatus",
    "RuntimeThreadTerminalSignal",
    "RuntimeTurnTerminalStateMachine",
]
