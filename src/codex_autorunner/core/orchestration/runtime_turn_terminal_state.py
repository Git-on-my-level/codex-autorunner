from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from ..time_utils import now_iso
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
        if inspection.assistant_stream_text:
            self.last_assistant_text = merge_assistant_stream_text(
                self.last_assistant_text,
                inspection.assistant_stream_text,
            )
        if inspection.assistant_message_text:
            self.last_assistant_text = inspection.assistant_message_text
        if inspection.failure_message:
            self.failure_cause = inspection.failure_message
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
        successful_terminal = self._saw_successful_terminal_signal()

        if self.transport_request_return_timestamp is None:
            if successful_terminal:
                return self._build_outcome(
                    status="ok",
                    assistant_text=assistant_text,
                    error=None,
                    completion_source="stream_terminal_event",
                )
            return self._build_outcome(
                status="error",
                assistant_text="",
                error=detail or execution_error_message,
                completion_source="stream_terminal_event",
            )

        if self.transport_errors:
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

        if status in _INTERRUPTED_COMPLETION_STATUSES:
            return self._build_outcome(
                status="interrupted",
                assistant_text="",
                error=self.failure_cause,
                completion_source="interrupt",
            )
        if status and not successful_transport:
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


def _merge_runtime_raw_events(
    streamed_raw_events: list[Any],
    result_raw_events: list[Any],
) -> list[Any]:
    streamed = list(streamed_raw_events or [])
    result = list(result_raw_events or [])
    if not streamed:
        return result
    if not result:
        return streamed
    streamed_keys = [_runtime_raw_event_key(item) for item in streamed]
    result_keys = [_runtime_raw_event_key(item) for item in result]
    max_overlap = min(len(streamed_keys), len(result_keys))
    for overlap in range(max_overlap, 0, -1):
        if streamed_keys[-overlap:] == result_keys[:overlap]:
            return streamed + result[overlap:]
    return streamed + result


def _runtime_raw_event_key(raw_event: Any) -> str:
    if isinstance(raw_event, (dict, list)):
        return json.dumps(
            raw_event,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return str(raw_event)


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
    method_lower = method.lower()

    if method in {"message.completed", "message.updated"}:
        role = _extract_message_role(params)
        if role != "user":
            assistant_message_text = _extract_message_text(params)
    elif method in {"prompt/message", "turn/message"}:
        assistant_message_text = _extract_message_text(params)
    elif method in {"prompt/completed", "turn/completed"}:
        assistant_message_text = _extract_message_text(params)
        if _status_indicates_successful_completion(
            params.get("status") or params.get("turn"),
            assume_true_when_missing=True,
        ):
            terminal_signal = RuntimeThreadTerminalSignal(
                source=method,
                status="ok",
                timestamp=timestamp,
            )
    elif method in {"prompt/cancelled", "turn/cancelled"}:
        terminal_signal = RuntimeThreadTerminalSignal(
            source=method,
            status="interrupted",
            timestamp=timestamp,
        )
    elif method in {"prompt/failed", "turn/failed", "turn/error", "error"}:
        failure_message = _extract_error_message(params)
        terminal_signal = RuntimeThreadTerminalSignal(
            source=method,
            status="error",
            timestamp=timestamp,
        )
    elif method == "session.idle":
        assistant_message_text = _extract_message_text(params)
        terminal_signal = RuntimeThreadTerminalSignal(
            source=method,
            status="ok",
            timestamp=timestamp,
        )
    elif method in {"session.status", "session/status"}:
        assistant_message_text = _extract_message_text(params)
        if _session_status_type(raw_event) == "idle":
            terminal_signal = RuntimeThreadTerminalSignal(
                source=method,
                status="ok",
                timestamp=timestamp,
            )
    elif method == "item/completed":
        item = params.get("item")
        if (
            isinstance(item, dict)
            and str(item.get("type") or "").strip() == "agentMessage"
            and str(item.get("phase") or "").strip().lower() != "commentary"
        ):
            assistant_message_text = _extract_agent_message_text(item)

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
            update_kind = str(
                update.get("sessionUpdate") or update.get("session_update") or ""
            ).strip()
            if update_kind == "agent_message_chunk":
                assistant_stream_text = _extract_output_delta(update)

    return _RawEventInspection(
        assistant_message_text=assistant_message_text,
        assistant_stream_text=assistant_stream_text,
        failure_message=failure_message,
        terminal_signal=terminal_signal,
    )


def _session_status_type(raw_event: dict[str, Any]) -> str:
    params = raw_event.get("params")
    if not isinstance(params, dict):
        message = raw_event.get("message")
        if isinstance(message, dict):
            params = message.get("params")
    if not isinstance(params, dict):
        return ""
    status = params.get("status")
    if isinstance(status, dict):
        for key in ("type", "status", "state"):
            value = str(status.get(key) or "").strip().lower()
            if value:
                return value
    properties = params.get("properties")
    if isinstance(properties, dict):
        nested_status = properties.get("status")
        if isinstance(nested_status, dict):
            for key in ("type", "status", "state"):
                value = str(nested_status.get(key) or "").strip().lower()
                if value:
                    return value
    return str(params.get("status") or "").strip().lower()


def _status_indicates_successful_completion(
    status: Any, *, assume_true_when_missing: bool
) -> bool:
    normalized = _extract_status_value(status)
    if not isinstance(normalized, str):
        return assume_true_when_missing
    return normalized.lower() in _SUCCESSFUL_COMPLETION_STATUSES


def _extract_status_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("type", "status", "state"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


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


def _extract_message_text(params: dict[str, Any]) -> Optional[str]:
    for key in (
        "text",
        "content",
        "message",
        "final_message",
        "finalOutput",
        "final_output",
    ):
        value = params.get(key)
        text = _string_from_value(value)
        if text:
            return text
    output = params.get("output")
    if isinstance(output, dict):
        text = _string_from_value(output.get("text") or output.get("content"))
        if text:
            return text
    item = params.get("item")
    if isinstance(item, dict):
        text = _extract_agent_message_text(item)
        if text:
            return text
    return None


def _extract_agent_message_text(item: dict[str, Any]) -> Optional[str]:
    for key in ("text", "message"):
        text = _string_from_value(item.get(key))
        if text:
            return text
    content = item.get("content")
    if isinstance(content, list):
        parts = [_string_from_value(part) for part in content]
        joined = "".join(part for part in parts if part)
        return joined or None
    return _string_from_value(content)


def _extract_output_delta(params: dict[str, Any]) -> Optional[str]:
    for key in ("delta", "text", "content"):
        text = _string_from_value(params.get(key))
        if text:
            return text
    output = params.get("output")
    if isinstance(output, dict):
        for key in ("delta", "text", "content"):
            text = _string_from_value(output.get(key))
            if text:
                return text
    return None


def _extract_error_message(params: dict[str, Any]) -> str:
    for key in ("message", "error", "reason"):
        value = params.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("error")
        text = _string_from_value(value)
        if text:
            return text
    return "Turn error"


def _string_from_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in ("text", "message", "content", "value"):
            nested_text = _string_from_value(value.get(key))
            if nested_text:
                return nested_text
        return None
    if isinstance(value, list):
        parts = [_string_from_value(item) for item in value]
        joined = "".join(part for part in parts if part)
        return joined or None
    return None


__all__ = [
    "RuntimeThreadCompletionSource",
    "RuntimeThreadOutcome",
    "RuntimeThreadOutcomeStatus",
    "RuntimeThreadTerminalSignal",
    "RuntimeTurnTerminalStateMachine",
]
