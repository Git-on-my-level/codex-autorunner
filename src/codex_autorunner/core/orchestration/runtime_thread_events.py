from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..acp_lifecycle import analyze_acp_lifecycle_message
from ..logging_utils import log_event
from ..ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    RUN_EVENT_STREAM_MODE_SNAPSHOT,
    ApprovalRequested,
    Completed,
    Failed,
    Interrupted,
    OutputDelta,
    ProviderRuntimeReported,
    RunEvent,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
)
from ..sse import SSEEvent, parse_sse_lines
from ..time_utils import now_iso
from .codex_item_normalizers import (
    merge_runtime_raw_events,
)
from .opencode_event_fields import (
    coerce_dict as _coerce_dict,
)
from .runtime_thread_decoders import (
    DecoderContext,
    build_default_decoder_registry,
)
from .runtime_threads import RUNTIME_THREAD_TIMEOUT_ERROR, RuntimeThreadOutcome
from .stream_text_merge import AssistantOutputState, AssistantTextAccumulator

_logger = logging.getLogger(__name__)

DECODE_FAILURE_REASON_MALFORMED_JSON = "malformed_json"
DECODE_FAILURE_REASON_REGISTRY_MISS = "registry_miss"
DECODE_FAILURE_REASON_EMPTY_METHOD = "empty_method"
DECODE_FAILURE_REASON_UNSUPPORTED_SHAPE = "unsupported_shape"
DECODE_FAILURE_REASON_UNSUPPORTED_TYPE = "unsupported_type"
DECODE_FAILURE_REASON_MALFORMED_SSE_JSON = "malformed_sse_json"

DIRECT_RUN_EVENT_TYPES = (
    OutputDelta,
    ToolCall,
    ApprovalRequested,
    RunNotice,
    TokenUsage,
    ProviderRuntimeReported,
    Completed,
    Failed,
    Interrupted,
    Started,
)

RawTimelineEvent = Any
TimelineEvent = RunEvent
RawTimelineHistory = list[RawTimelineEvent]
TimelineHistory = list[TimelineEvent]


@dataclass
class RuntimeEventDriver:
    """Keeps append-only timeline history separate from reduced assistant output."""

    state: RuntimeThreadRunEventState = field(
        default_factory=lambda: RuntimeThreadRunEventState()
    )
    raw_events: RawTimelineHistory = field(default_factory=list)
    run_events: TimelineHistory = field(default_factory=list)
    assistant_output: AssistantOutputState = field(default_factory=AssistantOutputState)
    timeline_assistant_chunks: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    token_usage: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.assistant_output.text and self.state.best_assistant_text():
            self.assistant_output = AssistantOutputState(
                stream_text=self.state.assistant_stream_text,
                final_text=self.state.assistant_message_text,
            )

    async def consume_raw_event(
        self,
        raw_event: Any,
        *,
        timestamp: Optional[str] = None,
        store_raw_event: bool = True,
    ) -> list[RunEvent]:
        if store_raw_event:
            self.raw_events.append(raw_event)
        events = await normalize_runtime_progress_event(
            raw_event,
            self.state,
        )
        self._record_run_events(events)
        return events

    async def consume_raw_events(
        self,
        raw_events: Iterable[Any],
        *,
        timestamp: Optional[str] = None,
        store_raw_event: bool = True,
    ) -> list[RunEvent]:
        normalized: list[RunEvent] = []
        for raw_event in raw_events:
            normalized.extend(
                await self.consume_raw_event(
                    raw_event,
                    timestamp=timestamp,
                    store_raw_event=store_raw_event,
                )
            )
        return normalized

    def append_run_event(self, event: RunEvent) -> None:
        self._record_run_events([event])

    def best_assistant_text(self) -> str:
        return self.assistant_output.text

    @property
    def assistant_parts(self) -> list[str]:
        """Append-only assistant output timeline chunks, not final output state."""

        return self.timeline_assistant_chunks

    def merged_raw_events(self, raw_events: Iterable[Any]) -> list[Any]:
        return merge_runtime_thread_raw_events(
            self.raw_events,
            list(raw_events),
        )

    def _record_run_events(self, events: list[RunEvent]) -> None:
        if not events:
            return
        self.run_events.extend(events)
        for event in events:
            if isinstance(event, OutputDelta):
                if event.delta_type in {
                    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
                }:
                    self.timeline_assistant_chunks.append(event.content)
                    if event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM:
                        if event.stream_mode == RUN_EVENT_STREAM_MODE_SNAPSHOT:
                            self.assistant_output.note_stream_snapshot(event.content)
                        else:
                            self.assistant_output.note_stream_delta(event.content)
                    else:
                        self.assistant_output.note_final_message(event.content)
                    continue
                if event.delta_type == RUN_EVENT_DELTA_TYPE_LOG_LINE:
                    self.log_lines.append(event.content)
                    continue
            if isinstance(event, Completed) and isinstance(event.final_message, str):
                self.assistant_output.note_final_message(event.final_message)
            if isinstance(event, TokenUsage) and isinstance(event.usage, dict):
                self.token_usage = dict(event.usage)


async def decode_runtime_raw_messages(raw_event: Any) -> list[dict[str, Any]]:
    if isinstance(raw_event, dict):
        raw_sse = raw_event.get("raw_event")
        if isinstance(raw_sse, str) and raw_sse.strip():
            return await decode_runtime_raw_messages(raw_sse)
        if isinstance(raw_event.get("message"), dict):
            return [dict(raw_event["message"])]
        if isinstance(raw_event.get("method"), str):
            return [dict(raw_event)]
        _log_decode_failure(
            DECODE_FAILURE_REASON_UNSUPPORTED_SHAPE,
            payload_type="dict",
            payload_keys=tuple(raw_event.keys()),
        )
        return []
    if not isinstance(raw_event, str):
        _log_decode_failure(
            DECODE_FAILURE_REASON_UNSUPPORTED_TYPE,
            payload_type=type(raw_event).__name__,
        )
        return []
    text = raw_event.strip()
    if not text:
        return []
    if not text.startswith("event:") and not text.startswith("data:"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            _log_decode_failure(
                DECODE_FAILURE_REASON_MALFORMED_JSON,
                raw_length=len(text),
            )
            return []
        return await decode_runtime_raw_messages(parsed)

    messages: list[dict[str, Any]] = []
    async for sse_event in _parse_runtime_thread_sse(text):
        payload = _load_json_object(sse_event.data)
        if sse_event.event in {"app-server", "event"}:
            message = payload.get("message")
            if isinstance(message, dict):
                messages.append(dict(message))
                continue
        messages.append(
            {
                "method": sse_event.event,
                "params": payload,
            }
        )
    return messages


def raw_event_message(raw_event: Any) -> dict[str, Any]:
    if not isinstance(raw_event, dict):
        return {}
    message = raw_event.get("message")
    if isinstance(message, dict):
        return message
    return raw_event


def raw_event_method(raw_event: Any) -> str:
    message = raw_event_message(raw_event)
    return str(message.get("method") or "").strip()


def raw_event_session_update(raw_event: Any) -> dict[str, Any]:
    message = raw_event_message(raw_event)
    params = message.get("params")
    if not isinstance(params, dict):
        return {}
    update = params.get("update")
    if isinstance(update, dict):
        return update
    return {}


def raw_event_content_summary(raw_event: Any) -> dict[str, Any]:
    update = raw_event_session_update(raw_event)
    content = update.get("content")
    part_types: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip()
            if item_type:
                part_types.append(item_type)
    return {
        "session_update_kind": str(
            update.get("sessionUpdate") or update.get("session_update") or ""
        ).strip(),
        "content_kind": type(content).__name__ if content is not None else "missing",
        "content_part_count": len(content) if isinstance(content, list) else None,
        "content_part_types": tuple(part_types),
    }


def note_run_event_state(
    event_state: RuntimeThreadRunEventState,
    run_event: Any,
) -> None:
    event_state.note_runtime_progress(
        type(run_event).__name__,
        timestamp=now_iso(),
    )
    if isinstance(run_event, OutputDelta):
        if run_event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM:
            event_state.note_stream_text(
                str(run_event.content or ""),
                merge_snapshot=run_event.stream_mode == RUN_EVENT_STREAM_MODE_SNAPSHOT,
                preserve_word_boundaries=bool(
                    run_event.data.get("preserve_word_boundaries")
                ),
            )
            return
        if run_event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE:
            event_state.note_message_text(str(run_event.content or ""))
            return
        return
    if isinstance(run_event, TokenUsage) and isinstance(run_event.usage, dict):
        event_state.token_usage = dict(run_event.usage)
        return
    if isinstance(run_event, Completed):
        event_state.completed_seen = True
        if isinstance(run_event.final_message, str):
            event_state.note_message_text(run_event.final_message)
        return
    if isinstance(run_event, Failed):
        error_message = str(run_event.error_message or "").strip()
        if error_message:
            event_state.last_error_message = error_message
        return
    if isinstance(run_event, Interrupted):
        reason = str(run_event.reason or "").strip()
        if reason:
            event_state.last_error_message = reason


async def normalize_runtime_progress_event(
    raw_event: Any,
    event_state: RuntimeThreadRunEventState,
) -> list[Any]:
    if isinstance(raw_event, DIRECT_RUN_EVENT_TYPES):
        note_run_event_state(event_state, raw_event)
        return [raw_event]
    return await normalize_runtime_thread_raw_event(raw_event, event_state)


def runtime_trace_fields(
    event_state: RuntimeThreadRunEventState,
) -> dict[str, Any]:
    return {
        "last_runtime_method": event_state.last_runtime_method,
        "last_progress_at": event_state.last_progress_at,
    }


def terminal_evidence_trace_fields(
    outcome: RuntimeThreadOutcome,
) -> dict[str, Any]:
    return dict(outcome.terminal_evidence)


def completion_source_from_outcome(
    outcome: RuntimeThreadOutcome,
    *,
    recovered_after_completion: bool,
) -> str:
    if recovered_after_completion and outcome.completion_source == "prompt_return":
        return "post_completion_recovery"
    if outcome.status == "interrupted":
        return "interrupt"
    if str(outcome.error or "").strip() == RUNTIME_THREAD_TIMEOUT_ERROR:
        return "timeout"
    if outcome.completion_source:
        return outcome.completion_source
    return "prompt_return"


def merge_runtime_thread_raw_events(
    streamed_raw_events: list[Any] | tuple[Any, ...],
    result_raw_events: list[Any] | tuple[Any, ...],
) -> list[Any]:
    return merge_runtime_raw_events(streamed_raw_events, result_raw_events)


def _merge_pending_stream_text(
    current: str, incoming: str, *, preserve_word_boundaries: bool = False
) -> str:
    accumulator = AssistantTextAccumulator(stream_text=current)
    accumulator.merge_snapshot(
        incoming, preserve_word_boundaries=preserve_word_boundaries
    )
    return accumulator.stream_text


@dataclass
class RuntimeThreadRunEventState:
    reasoning_buffers: dict[str, str] = field(default_factory=dict)
    reasoning_last_emitted: dict[str, str] = field(default_factory=dict)
    assistant_stream_text: str = ""
    assistant_message_text: str = ""
    token_usage: Optional[dict[str, Any]] = None
    last_error_message: Optional[str] = None
    last_runtime_method: Optional[str] = None
    last_progress_at: Optional[str] = None
    completed_seen: bool = False
    message_roles: dict[str, str] = field(default_factory=dict)
    pending_stream_by_message: dict[str, str] = field(default_factory=dict)
    pending_stream_no_id: str = ""
    message_roles_seen: bool = False
    opencode_part_types: dict[str, str] = field(default_factory=dict)
    opencode_tool_status: dict[str, str] = field(default_factory=dict)
    opencode_patch_hashes: set[str] = field(default_factory=set)
    _assistant_text: AssistantOutputState = field(
        default_factory=AssistantOutputState,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._assistant_text = AssistantOutputState(
            stream_text=self.assistant_stream_text,
            final_text=self.assistant_message_text,
        )

    def note_stream_text(
        self,
        text: str,
        *,
        merge_snapshot: bool = True,
        preserve_word_boundaries: bool = False,
    ) -> None:
        if merge_snapshot:
            self._assistant_text.note_stream_snapshot(
                text, preserve_word_boundaries=preserve_word_boundaries
            )
        else:
            self._assistant_text.note_stream_delta(
                text, preserve_word_boundaries=preserve_word_boundaries
            )
        self.assistant_stream_text = self._assistant_text.stream_text

    def note_message_text(self, text: str) -> None:
        if isinstance(text, str) and text.strip():
            self._assistant_text.note_final_message(text)
            self.assistant_message_text = self._assistant_text.final_text

    def best_assistant_text(self) -> str:
        return self._assistant_text.text

    def note_runtime_progress(
        self,
        method: Optional[str],
        *,
        timestamp: Optional[str] = None,
    ) -> None:
        normalized_method = str(method or "").strip()
        if normalized_method:
            self.last_runtime_method = normalized_method
        self.last_progress_at = timestamp or now_iso()

    def note_message_role(
        self,
        message_id: Optional[str],
        role: Optional[str],
        *,
        timestamp: Optional[str] = None,
    ) -> list[RunEvent]:
        event_timestamp = timestamp or now_iso()
        if not message_id or not role:
            return []
        self.message_roles[message_id] = role
        self.message_roles_seen = True
        if role == "user":
            self.pending_stream_by_message.pop(message_id, None)
            self.pending_stream_no_id = ""
            return []
        pending = self.pending_stream_by_message.pop(message_id, "")
        events: list[RunEvent] = []
        if pending:
            self.note_stream_text(pending)
            events.append(
                OutputDelta(
                    timestamp=event_timestamp,
                    content=pending,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                    stream_mode=RUN_EVENT_STREAM_MODE_SNAPSHOT,
                )
            )
        if self.pending_stream_no_id:
            pending_no_id = self.pending_stream_no_id
            self.pending_stream_no_id = ""
            self.note_stream_text(pending_no_id)
            events.append(
                OutputDelta(
                    timestamp=event_timestamp,
                    content=pending_no_id,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                    stream_mode=RUN_EVENT_STREAM_MODE_SNAPSHOT,
                )
            )
        return events

    def note_message_part_text(
        self,
        message_id: Optional[str],
        text: str,
        *,
        timestamp: Optional[str] = None,
        preserve_word_boundaries: bool = False,
    ) -> list[RunEvent]:
        event_timestamp = timestamp or now_iso()
        if not isinstance(text, str) or not text:
            return []
        if message_id is None:
            if not self.message_roles_seen:
                self.note_stream_text(
                    text, preserve_word_boundaries=preserve_word_boundaries
                )
                return [
                    OutputDelta(
                        timestamp=event_timestamp,
                        content=text,
                        delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                        stream_mode=RUN_EVENT_STREAM_MODE_SNAPSHOT,
                        data=(
                            {"preserve_word_boundaries": True}
                            if preserve_word_boundaries
                            else {}
                        ),
                    )
                ]
            self.pending_stream_no_id = _merge_pending_stream_text(
                self.pending_stream_no_id,
                text,
                preserve_word_boundaries=preserve_word_boundaries,
            )
            return []
        role = self.message_roles.get(message_id)
        if role == "user":
            return []
        if role == "assistant":
            self.note_stream_text(
                text, preserve_word_boundaries=preserve_word_boundaries
            )
            return [
                OutputDelta(
                    timestamp=event_timestamp,
                    content=text,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                    stream_mode=RUN_EVENT_STREAM_MODE_SNAPSHOT,
                    data=(
                        {"preserve_word_boundaries": True}
                        if preserve_word_boundaries
                        else {}
                    ),
                )
            ]
        self.pending_stream_by_message[message_id] = _merge_pending_stream_text(
            self.pending_stream_by_message.get(message_id, ""),
            text,
            preserve_word_boundaries=preserve_word_boundaries,
        )
        return []


async def normalize_runtime_thread_raw_event(
    raw_event: Any,
    state: RuntimeThreadRunEventState,
    *,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    if isinstance(raw_event, dict):
        raw_sse = raw_event.get("raw_event")
        if isinstance(raw_sse, str) and raw_sse.strip():
            events: list[RunEvent] = []
            async for sse_event in _parse_runtime_thread_sse(raw_sse):
                events.extend(
                    _normalize_sse_event(sse_event, state, timestamp=timestamp)
                )
            return events
        return normalize_runtime_thread_message_payload(
            raw_event,
            state,
            timestamp=timestamp,
        )
    str_raw_events: list[RunEvent] = []
    async for sse_event in _parse_runtime_thread_sse(raw_event):
        str_raw_events.extend(
            _normalize_sse_event(sse_event, state, timestamp=timestamp)
        )
    return str_raw_events


def normalize_runtime_thread_message_payload(
    payload: dict[str, Any],
    state: RuntimeThreadRunEventState,
    *,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    if isinstance(payload.get("message"), dict):
        message = payload["message"]
        return normalize_runtime_thread_message(
            str(message.get("method") or ""),
            _coerce_dict(message.get("params")),
            state,
            timestamp=timestamp,
            raw_message=message,
        )
    method = payload.get("method")
    params = payload.get("params")
    if isinstance(method, str) and isinstance(params, dict):
        return normalize_runtime_thread_message(
            method,
            params,
            state,
            timestamp=timestamp,
            raw_message=payload,
        )
    event_timestamp = timestamp or now_iso()
    _log_decode_failure(
        DECODE_FAILURE_REASON_UNSUPPORTED_SHAPE,
        payload_type="dict",
        payload_keys=tuple(payload.keys()),
        has_message=isinstance(payload.get("message"), dict),
        has_method=isinstance(payload.get("method"), str),
        has_params=isinstance(payload.get("params"), dict),
    )
    return [
        RunNotice(
            timestamp=event_timestamp,
            kind="decode_failure",
            message="Unsupported runtime thread message shape",
            data={
                "reason": DECODE_FAILURE_REASON_UNSUPPORTED_SHAPE,
                "payload_type": "dict",
                "payload_keys": tuple(payload.keys()),
            },
        )
    ]


def terminal_run_event_from_outcome(
    outcome: RuntimeThreadOutcome,
    state: RuntimeThreadRunEventState,
) -> Completed | Failed | Interrupted:
    if outcome.status == "ok":
        if outcome.assistant_output is not None:
            final_message = outcome.assistant_output.text
        else:
            final_message = outcome.assistant_text
        if not final_message and outcome.assistant_output is None:
            final_message = state.best_assistant_text()
        return Completed(
            timestamp=now_iso(),
            final_message=final_message,
        )
    if outcome.status == "interrupted":
        return Interrupted(
            timestamp=now_iso(),
            reason="Runtime thread interrupted",
        )
    _error_detail = str(outcome.error or "").strip()
    return Failed(
        timestamp=now_iso(),
        error_message=(
            _error_detail
            if _error_detail in {"Runtime thread timed out"}
            else "Runtime thread failed"
        ),
    )


def recover_post_completion_outcome(
    outcome: RuntimeThreadOutcome,
    state: RuntimeThreadRunEventState,
) -> RuntimeThreadOutcome:
    """Prefer a streamed completion over a later transport error or interrupt.

    Bounded compatibility contract:
    - This function may upgrade an ``error`` or ``interrupted`` outcome to
      ``ok`` **only** when all three conditions hold:
      1. ``state.completed_seen`` is True (a terminal completion signal was
         observed in the event stream).
      2. The outcome status is ``error`` or ``interrupted`` (not already ``ok``).
      3. Non-empty assistant text exists (either from the outcome or from the
         event state).
    - It must **not** widen into a general fallback matrix.  Missing evidence
      (no completion signal, no assistant text) means the original outcome is
      returned unchanged.
    - An already-ok outcome passes through unchanged.
    """

    if outcome.status not in {"error", "interrupted"} or not state.completed_seen:
        return outcome
    assistant_text = outcome.assistant_text
    if not assistant_text:
        assistant_text = state.best_assistant_text()
    if not isinstance(assistant_text, str) or not assistant_text.strip():
        return outcome
    return RuntimeThreadOutcome(
        status="ok",
        assistant_text=assistant_text,
        error=None,
        backend_thread_id=outcome.backend_thread_id,
        backend_turn_id=outcome.backend_turn_id,
    )


async def _parse_runtime_thread_sse(raw_event: str):
    async def _iter_lines() -> Any:
        for line in str(raw_event).splitlines():
            yield line
        yield ""

    async for sse_event in parse_sse_lines(_iter_lines()):
        yield sse_event


def _normalize_sse_event(
    sse_event: SSEEvent,
    state: RuntimeThreadRunEventState,
    *,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    payload = _load_json_object(sse_event.data)
    if sse_event.event in {"app-server", "event"}:
        message = payload.get("message")
        if isinstance(message, dict):
            return normalize_runtime_thread_message(
                str(message.get("method") or ""),
                _coerce_dict(message.get("params")),
                state,
                timestamp=timestamp,
                raw_message=message,
            )
    return normalize_runtime_thread_message(
        sse_event.event,
        payload,
        state,
        timestamp=timestamp,
        raw_message={"method": sse_event.event, "params": payload},
    )


_DEFAULT_REGISTRY = build_default_decoder_registry()
assert (
    _DEFAULT_REGISTRY is not None
), "Decoder registry must be initialized at module load"


def _log_decode_failure(
    reason: str,
    **fields: Any,
) -> None:
    log_event(
        _logger,
        logging.DEBUG,
        "orchestration.event_decode.failure",
        reason=reason,
        **fields,
    )


def _should_emit_decode_failure_notice() -> bool:
    value = (
        str(os.environ.get("CAR_RUNTIME_DECODE_FAILURE_NOTICES") or "").strip().lower()
    )
    if value in {"1", "true", "yes", "on", "debug"}:
        return True
    return "PYTEST_CURRENT_TEST" in os.environ


def normalize_runtime_thread_message(
    method: str,
    params: dict[str, Any],
    state: RuntimeThreadRunEventState,
    *,
    raw_message: Optional[dict[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    event_timestamp = timestamp or now_iso()
    if not method:
        _log_decode_failure(
            DECODE_FAILURE_REASON_EMPTY_METHOD,
            payload_keys=tuple(params.keys()) if isinstance(params, dict) else None,
        )
        return [
            RunNotice(
                timestamp=event_timestamp,
                kind="decode_failure",
                message="Empty method in runtime thread message",
                data={
                    "reason": DECODE_FAILURE_REASON_EMPTY_METHOD,
                    "payload_keys": (
                        tuple(params.keys()) if isinstance(params, dict) else None
                    ),
                },
            )
        ]
    state.note_runtime_progress(method, timestamp=event_timestamp)
    acp_lifecycle = analyze_acp_lifecycle_message(
        raw_message or {"method": method, "params": params}
    )
    ctx = DecoderContext(
        timestamp=event_timestamp,
        raw_message=raw_message or {"method": method, "params": params},
        acp_lifecycle=acp_lifecycle,
    )
    if not _DEFAULT_REGISTRY.has_decoder(method):
        _log_decode_failure(
            DECODE_FAILURE_REASON_REGISTRY_MISS,
            method=method,
            payload_keys=tuple(params.keys()) if isinstance(params, dict) else None,
        )
        if not _should_emit_decode_failure_notice():
            return []
        return [
            RunNotice(
                timestamp=event_timestamp,
                kind="decode_failure",
                message=f"No decoder for method: {method}",
                data={
                    "reason": DECODE_FAILURE_REASON_REGISTRY_MISS,
                    "method": method,
                },
            )
        ]
    return _DEFAULT_REGISTRY.decode(method, params, state, ctx)


def _load_json_object(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        _log_decode_failure(
            DECODE_FAILURE_REASON_MALFORMED_SSE_JSON,
            raw_length=len(raw),
        )
        return {}
    return _coerce_dict(loaded)


__all__ = [
    "RuntimeEventDriver",
    "RuntimeThreadRunEventState",
    "RawTimelineEvent",
    "RawTimelineHistory",
    "TimelineEvent",
    "TimelineHistory",
    "decode_runtime_raw_messages",
    "merge_runtime_thread_raw_events",
    "normalize_runtime_thread_message",
    "normalize_runtime_thread_message_payload",
    "normalize_runtime_thread_raw_event",
    "recover_post_completion_outcome",
    "terminal_run_event_from_outcome",
    "DIRECT_RUN_EVENT_TYPES",
    "raw_event_message",
    "raw_event_method",
    "raw_event_session_update",
    "raw_event_content_summary",
    "note_run_event_state",
    "normalize_runtime_progress_event",
    "runtime_trace_fields",
    "terminal_evidence_trace_fields",
    "completion_source_from_outcome",
    "DECODE_FAILURE_REASON_MALFORMED_JSON",
    "DECODE_FAILURE_REASON_REGISTRY_MISS",
    "DECODE_FAILURE_REASON_EMPTY_METHOD",
    "DECODE_FAILURE_REASON_UNSUPPORTED_SHAPE",
    "DECODE_FAILURE_REASON_UNSUPPORTED_TYPE",
    "DECODE_FAILURE_REASON_MALFORMED_SSE_JSON",
]
