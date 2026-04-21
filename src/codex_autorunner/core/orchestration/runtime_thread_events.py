from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..acp_lifecycle import analyze_acp_lifecycle_message
from ..acp_lifecycle import (
    extract_error_message as _shared_acp_error_message,
)
from ..acp_lifecycle import (
    extract_message_text as _shared_acp_message_text,
)
from ..acp_lifecycle import (
    extract_output_delta as _shared_acp_output_delta,
)
from ..acp_lifecycle import (
    extract_progress_message as _shared_acp_progress_message,
)
from ..acp_lifecycle import (
    extract_session_update as _shared_acp_session_update,
)
from ..acp_lifecycle import (
    extract_usage as _shared_extract_usage,
)
from ..ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunEvent,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from ..sse import SSEEvent, parse_sse_lines
from ..time_utils import now_iso
from .codex_item_normalizers import (
    extract_agent_message_text as _extract_agent_message_text,
)
from .codex_item_normalizers import (
    is_commentary_agent_message as _is_commentary_agent_message,
)
from .codex_item_normalizers import (
    merge_runtime_raw_events,
)
from .codex_item_normalizers import (
    normalize_tool_name as _normalize_tool_name,
)
from .codex_item_normalizers import (
    output_delta_type_for_method as _output_delta_type_for_method,
)
from .codex_item_normalizers import (
    reasoning_buffer_key as _reasoning_buffer_key,
)
from .opencode_event_fields import (
    coerce_dict as _event_coerce_dict,
)
from .opencode_event_fields import (
    extract_message_id as _event_extract_message_id,
)
from .opencode_event_fields import (
    extract_message_part as _event_extract_message_part,
)
from .opencode_event_fields import (
    extract_message_role as _event_extract_message_role,
)
from .opencode_event_fields import (
    extract_output_delta as _event_extract_output_delta,
)
from .opencode_event_fields import (
    extract_part_id as _event_extract_part_id,
)
from .opencode_event_fields import (
    extract_part_message_id as _event_extract_part_message_id,
)
from .opencode_event_fields import (
    extract_part_type as _event_extract_part_type,
)
from .runtime_payload_shapes import (
    OpenCodeToolPartShape,
    TokenUsageShape,
)
from .runtime_threads import RUNTIME_THREAD_TIMEOUT_ERROR, RuntimeThreadOutcome
from .stream_text_merge import merge_assistant_stream_text

_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}

DIRECT_RUN_EVENT_TYPES = (
    OutputDelta,
    ToolCall,
    ApprovalRequested,
    RunNotice,
    TokenUsage,
    Completed,
    Failed,
    Started,
)


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
            event_state.note_stream_text(str(run_event.content or ""))
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


@dataclass
class RuntimeThreadRunEventState:
    reasoning_buffers: dict[str, str] = field(default_factory=dict)
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

    def note_stream_text(self, text: str) -> None:
        if isinstance(text, str) and text:
            self.assistant_stream_text = merge_assistant_stream_text(
                self.assistant_stream_text,
                text,
            )

    def note_message_text(self, text: str) -> None:
        if isinstance(text, str) and text.strip():
            self.assistant_message_text = text

    def best_assistant_text(self) -> str:
        if self.assistant_message_text.strip():
            return self.assistant_message_text
        return self.assistant_stream_text

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
                )
            )
        return events

    def note_message_part_text(
        self,
        message_id: Optional[str],
        text: str,
        *,
        timestamp: Optional[str] = None,
    ) -> list[RunEvent]:
        event_timestamp = timestamp or now_iso()
        if not isinstance(text, str) or not text:
            return []
        if message_id is None:
            if not self.message_roles_seen:
                self.note_stream_text(text)
                return [
                    OutputDelta(
                        timestamp=event_timestamp,
                        content=text,
                        delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                    )
                ]
            self.pending_stream_no_id = merge_assistant_stream_text(
                self.pending_stream_no_id,
                text,
            )
            return []
        role = self.message_roles.get(message_id)
        if role == "user":
            return []
        if role == "assistant":
            self.note_stream_text(text)
            return [
                OutputDelta(
                    timestamp=event_timestamp,
                    content=text,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                )
            ]
        self.pending_stream_by_message[message_id] = merge_assistant_stream_text(
            self.pending_stream_by_message.get(message_id, ""),
            text,
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
    return []


def terminal_run_event_from_outcome(
    outcome: RuntimeThreadOutcome,
    state: RuntimeThreadRunEventState,
) -> Completed | Failed:
    if outcome.status == "ok":
        return Completed(
            timestamp=now_iso(),
            final_message=outcome.assistant_text or state.best_assistant_text(),
        )
    return Failed(
        timestamp=now_iso(),
        error_message=_public_terminal_error_message(outcome),
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
    assistant_text = outcome.assistant_text or state.best_assistant_text()
    if not isinstance(assistant_text, str) or not assistant_text.strip():
        return outcome
    return RuntimeThreadOutcome(
        status="ok",
        assistant_text=assistant_text,
        error=None,
        backend_thread_id=outcome.backend_thread_id,
        backend_turn_id=outcome.backend_turn_id,
    )


def _public_terminal_error_message(outcome: RuntimeThreadOutcome) -> str:
    detail = str(outcome.error or "").strip()
    if detail in {"Runtime thread timed out", "Runtime thread interrupted"}:
        return detail
    return "Runtime thread failed"


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
    if sse_event.event in {"app-server", "event", "zeroclaw"}:
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


def normalize_runtime_thread_message(
    method: str,
    params: dict[str, Any],
    state: RuntimeThreadRunEventState,
    *,
    raw_message: Optional[dict[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    event_timestamp = timestamp or now_iso()
    method_lower = method.lower()
    if not method:
        return []
    state.note_runtime_progress(method, timestamp=event_timestamp)
    acp_lifecycle = analyze_acp_lifecycle_message(
        raw_message or {"method": method, "params": params}
    )

    if method == "item/reasoning/summaryTextDelta":
        delta = params.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        key = _reasoning_buffer_key(params)
        if key:
            delta = f"{state.reasoning_buffers.get(key, '')}{delta}"
            state.reasoning_buffers[key] = delta
        return [RunNotice(timestamp=event_timestamp, kind="thinking", message=delta)]

    if method in {"prompt/output", "prompt/delta", "prompt/progress", "turn/progress"}:
        output_events = _assistant_stream_events(
            params,
            state,
            timestamp=event_timestamp,
        )
        if output_events:
            return output_events
        usage = _extract_usage(params)
        if usage is not None:
            state.token_usage = dict(usage)
            return [TokenUsage(timestamp=event_timestamp, usage=dict(usage))]
        progress_message = acp_lifecycle.progress_message
        if progress_message:
            return [
                RunNotice(
                    timestamp=event_timestamp,
                    kind="progress",
                    message=progress_message,
                )
            ]
        return []

    if method == "session/update":
        update = _extract_session_update(params)
        update_kind = acp_lifecycle.session_update_kind or ""
        if update_kind == "agent_message_chunk":
            if acp_lifecycle.message_phase == "commentary":
                commentary_text = acp_lifecycle.output_delta or _extract_output_delta(
                    _extract_session_update_message_params(update)
                )
                if not commentary_text:
                    return []
                return [
                    RunNotice(
                        timestamp=event_timestamp,
                        kind="commentary",
                        message=commentary_text,
                        data={"already_streamed": acp_lifecycle.already_streamed},
                    )
                ]
            return _assistant_stream_events(
                _extract_session_update_message_params(update),
                state,
                timestamp=event_timestamp,
            )
        if update_kind == "agent_thought_chunk":
            progress_message = _extract_session_update_text(update)
            if not progress_message:
                return []
            return [
                RunNotice(
                    timestamp=event_timestamp,
                    kind="progress",
                    message=progress_message,
                )
            ]
        if update_kind == "usage_update":
            session_usage = _extract_usage(update)
            if session_usage is None:
                return []
            state.token_usage = dict(session_usage)
            return [
                TokenUsage(
                    timestamp=event_timestamp,
                    usage=dict(session_usage),
                )
            ]
        return []

    if method in {"prompt/message", "turn/message"}:
        content = acp_lifecycle.assistant_text
        if not content:
            return []
        if acp_lifecycle.message_phase == "commentary":
            return [
                RunNotice(
                    timestamp=event_timestamp,
                    kind="commentary",
                    message=content,
                    data={"already_streamed": acp_lifecycle.already_streamed},
                )
            ]
        state.note_message_text(content)
        return [
            OutputDelta(
                timestamp=event_timestamp,
                content=content,
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
            )
        ]

    if method in {"permission/requested", "session/request_permission"}:
        request_id = acp_lifecycle.permission_request_id or _request_id_for_event(
            method, params
        )
        description = str(
            acp_lifecycle.permission_description or "Approval requested"
        ).strip()
        context = _coerce_dict(params.get("context")) or dict(params)
        return [
            ApprovalRequested(
                timestamp=event_timestamp,
                request_id=request_id,
                description=description or "Approval requested",
                context=context,
            )
        ]

    if method == "permission/decision":
        decision = str(params.get("decision") or "cancel").strip().lower()
        description = str(
            params.get("description")
            or params.get("reason")
            or "Approval decision recorded"
        ).strip()
        label = {
            "accept": "Approval accepted",
            "decline": "Approval declined",
            "cancel": "Approval cancelled",
        }.get(decision, "Approval updated")
        message = label if not description else f"{label}: {description}"
        return [
            RunNotice(
                timestamp=event_timestamp,
                kind="approval",
                message=message,
                data=dict(params),
            )
        ]

    if method == "token/usage":
        usage = _extract_usage(params)
        if usage is None:
            return []
        state.token_usage = dict(usage)
        return [TokenUsage(timestamp=event_timestamp, usage=dict(usage))]

    if method in {"prompt/failed", "turn/failed"}:
        error_message = _shared_acp_error_message(params)
        state.last_error_message = error_message or "Turn error"
        return [
            Failed(
                timestamp=event_timestamp,
                error_message=state.last_error_message,
            )
        ]

    if method in {
        "prompt/completed",
        "turn/completed",
        "prompt/cancelled",
        "turn/cancelled",
    }:
        events: list[RunEvent] = []
        content = acp_lifecycle.assistant_text
        if content:
            state.note_message_text(content)
            events.append(
                OutputDelta(
                    timestamp=event_timestamp,
                    content=content,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
                )
            )
        if acp_lifecycle.runtime_terminal_status == "ok":
            state.completed_seen = True
        return events

    if method == "item/completed":
        item = params.get("item")
        if not isinstance(item, dict):
            return []
        item_type = str(item.get("type") or "").strip()
        if item_type == "reasoning":
            key = _reasoning_buffer_key(params, item=item)
            if key:
                state.reasoning_buffers.pop(key, None)
            return []
        if item_type == "agentMessage":
            if _is_commentary_agent_message(item):
                content = _extract_agent_message_text(item)
                if not content:
                    return []
                return [
                    RunNotice(
                        timestamp=event_timestamp,
                        kind="commentary",
                        message=content,
                        data={"already_streamed": _extract_already_streamed_flag(item)},
                    )
                ]
            content = _extract_agent_message_text(item)
            if not content:
                return []
            state.note_message_text(content)
            return [
                OutputDelta(
                    timestamp=event_timestamp,
                    content=content,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
                )
            ]
        tool_name, tool_input = _normalize_tool_name(params, item=item)
        if tool_name:
            item_type_lower = str(item_type or "").strip().lower()
            if item_type_lower in {"commandexecution", "filechange", "tool"}:
                exit_code = item.get("exitCode")
                failed = isinstance(exit_code, int) and exit_code != 0
                return [
                    ToolResult(
                        timestamp=event_timestamp,
                        tool_name=tool_name,
                        status="error" if failed else "completed",
                        result=item.get("result"),
                        error=(
                            str(exit_code) if failed and exit_code is not None else None
                        ),
                    )
                ]
            return [
                ToolCall(
                    timestamp=event_timestamp,
                    tool_name=tool_name,
                    tool_input=tool_input,
                )
            ]
        return []

    if method == "item/agentMessage/delta":
        return _assistant_stream_events(params, state, timestamp=event_timestamp)

    if method in {"message.part.updated", "message.part.delta"}:
        return _normalize_message_part_updated(
            params,
            state,
            timestamp=event_timestamp,
        )

    if method in _APPROVAL_METHODS:
        request_id = _request_id_for_event(method, params)
        summary = _approval_summary(method, params)
        return [
            ApprovalRequested(
                timestamp=event_timestamp,
                request_id=request_id,
                description=summary,
                context=dict(params),
            )
        ]

    if method == "item/toolCall/start":
        tool_name, tool_input = _normalize_tool_name(params)
        return [
            ToolCall(
                timestamp=event_timestamp,
                tool_name=tool_name or "toolCall",
                tool_input=tool_input,
            )
        ]

    if method == "item/toolCall/end":
        tool_name, _tool_input = _normalize_tool_name(params)
        result = params.get("result")
        error = params.get("error")
        return [
            ToolResult(
                timestamp=event_timestamp,
                tool_name=tool_name or str(params.get("name") or "toolCall"),
                status="error" if error else "completed",
                result=result,
                error=error,
            )
        ]

    if method == "usage":
        usage = _extract_usage(params)
        if usage is None:
            return []
        state.token_usage = dict(usage)
        return [TokenUsage(timestamp=event_timestamp, usage=dict(usage))]

    if method == "permission":
        request_id = _request_id_for_event(method, params)
        description = str(
            params.get("reason") or params.get("message") or "Approval requested"
        ).strip()
        return [
            ApprovalRequested(
                timestamp=event_timestamp,
                request_id=request_id,
                description=description or "Approval requested",
                context=dict(params),
            )
        ]

    if method == "question":
        request_id = _request_id_for_event(method, params)
        question = str(params.get("question") or "").strip()
        return [
            ApprovalRequested(
                timestamp=event_timestamp,
                request_id=request_id,
                description=question or "Question pending",
                context=dict(params),
            )
        ]

    if method in {"message.updated", "message.completed"}:
        role_events = state.note_message_role(
            _extract_message_id(params),
            _extract_message_role(params),
            timestamp=event_timestamp,
        )
        if _extract_message_phase(params) == "commentary":
            content = _extract_message_text(params)
            if not content:
                return role_events
            return role_events + [
                RunNotice(
                    timestamp=event_timestamp,
                    kind="commentary",
                    message=content,
                    data={"already_streamed": _extract_already_streamed_flag(params)},
                )
            ]
        content = _extract_message_text(params)
        if not content:
            return role_events
        if _extract_message_role(params) == "user":
            return role_events
        state.note_message_text(content)
        return role_events + [
            OutputDelta(
                timestamp=event_timestamp,
                content=content,
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
            )
        ]

    if method == "message.delta":
        part_type = _event_extract_part_type(
            params, part_types=state.opencode_part_types
        )
        if part_type and part_type != "text":
            return []
        return _assistant_stream_events(params, state, timestamp=event_timestamp)

    if method == "turn/streamDelta" or "outputdelta" in method_lower:
        return _output_delta_events(
            method,
            params,
            state,
            timestamp=event_timestamp,
        )

    if method in {
        "turn/tokenUsage",
        "turn/usage",
        "thread/tokenUsage/updated",
    }:
        usage = _extract_usage(params)
        if usage is None:
            return []
        state.token_usage = dict(usage)
        return [TokenUsage(timestamp=event_timestamp, usage=dict(usage))]

    if method == "turn/error":
        turn_error_message: Any = params.get("message")
        if not isinstance(turn_error_message, str) or not turn_error_message.strip():
            turn_error_message = "Turn error"
        state.last_error_message = str(turn_error_message)
        return [
            Failed(timestamp=event_timestamp, error_message=str(turn_error_message))
        ]

    if method == "error":
        error = _coerce_dict(params.get("error"))
        generic_error_message: Any = error.get("message") or params.get("message")
        if (
            not isinstance(generic_error_message, str)
            or not generic_error_message.strip()
        ):
            generic_error_message = "Turn error"
        state.last_error_message = str(generic_error_message)
        return [
            Failed(timestamp=event_timestamp, error_message=str(generic_error_message))
        ]

    if method == "turn/completed":
        if acp_lifecycle.runtime_terminal_status == "ok":
            state.completed_seen = True
        return []

    if method == "session.idle":
        if acp_lifecycle.runtime_terminal_status == "ok":
            state.completed_seen = True
        return []

    if method in {"session.status", "session/status"}:
        if acp_lifecycle.runtime_terminal_status == "ok":
            state.completed_seen = True
            return []
        status_type = acp_lifecycle.session_status or ""
        if status_type and status_type != "idle":
            return [
                RunNotice(
                    timestamp=event_timestamp,
                    kind="progress",
                    message=f"agent {status_type}",
                )
            ]
        return []

    return []


def _assistant_stream_events(
    params: dict[str, Any],
    state: RuntimeThreadRunEventState,
    *,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    phase = str(params.get("phase") or "").strip().lower()
    if phase == "commentary":
        content = _extract_output_delta(params)
        if not content:
            return []
        return [
            RunNotice(
                timestamp=timestamp or now_iso(),
                kind="commentary",
                message=content,
                data={"already_streamed": _extract_already_streamed_flag(params)},
            )
        ]
    content = _extract_output_delta(params)
    if not content:
        return []
    state.note_stream_text(content)
    return [
        OutputDelta(
            timestamp=timestamp or now_iso(),
            content=content,
            delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
        )
    ]


def _normalize_message_part_updated(
    params: dict[str, Any],
    state: RuntimeThreadRunEventState,
    *,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    part = _extract_message_part(params)
    part_id = _extract_part_id(params, part=part)
    part_type = str(part.get("type") or "").strip().lower()
    if part_id and part_type:
        state.opencode_part_types[part_id] = part_type
    elif part_id and not part_type:
        part_type = state.opencode_part_types.get(part_id, "")
    part_for_processing = dict(part) if part else {}
    if part_id and "id" not in part_for_processing:
        part_for_processing["id"] = part_id
    if part_type and "type" not in part_for_processing:
        part_for_processing["type"] = part_type
    if not part and part_type in {"", "text"}:
        content = _extract_output_delta(params)
        if not content:
            return []
        return state.note_message_part_text(
            _extract_part_message_id(params),
            content,
            timestamp=timestamp,
        )

    if part and bool(part.get("ignored")):
        return []

    if part_type in {"", "text"}:
        content = _extract_output_delta(params)
        if not content:
            return []
        return state.note_message_part_text(
            _extract_part_message_id(params),
            content,
            timestamp=timestamp,
        )

    if part_type == "reasoning":
        content = _extract_opencode_reasoning_text(params, part_for_processing, state)
        if not content:
            return []
        return [
            RunNotice(
                timestamp=timestamp or now_iso(),
                kind="thinking",
                message=content,
            )
        ]

    if part_type == "tool":
        if not part_for_processing:
            return []
        return _normalize_opencode_tool_part(
            part_for_processing, state, timestamp=timestamp
        )

    if part_type == "patch":
        if not part_for_processing:
            return []
        return _normalize_opencode_patch_part(
            part_for_processing, state, timestamp=timestamp
        )

    if part_type == "usage":
        if not part_for_processing:
            return []
        usage = _extract_opencode_usage_part(part_for_processing)
        if usage is None:
            return []
        state.token_usage = dict(usage)
        return [TokenUsage(timestamp=timestamp or now_iso(), usage=dict(usage))]

    return []


def _output_delta_events(
    method: str,
    params: dict[str, Any],
    state: RuntimeThreadRunEventState,
    *,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    content = _extract_output_delta(params)
    if not content:
        return []
    delta_type = _output_delta_type_for_method(method)
    if delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM:
        state.note_stream_text(content)
    return [
        OutputDelta(
            timestamp=timestamp or now_iso(),
            content=content,
            delta_type=delta_type,
        )
    ]


def _load_json_object(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return _coerce_dict(loaded)


def _coerce_dict(value: Any) -> dict[str, Any]:
    return _event_coerce_dict(value)


def _extract_message_part(params: dict[str, Any]) -> dict[str, Any]:
    return _event_extract_message_part(params)


def _extract_output_delta(params: dict[str, Any]) -> str:
    return _event_extract_output_delta(params, include_part_text=True)


def _extract_session_update(params: dict[str, Any]) -> dict[str, Any]:
    return _shared_acp_session_update(params)


def _extract_session_update_message_params(update: dict[str, Any]) -> dict[str, Any]:
    content = update.get("content")
    if isinstance(content, dict):
        params = dict(content)
        message = update.get("message")
        if isinstance(message, str) and message.strip() and "message" not in params:
            params["message"] = message
        return params
    return {
        "content": content,
        "message": update.get("message"),
    }


def _extract_session_update_text(update: dict[str, Any]) -> str:
    return _extract_acp_progress_message(update) or _shared_acp_output_delta(update)


def _extract_acp_progress_message(params: dict[str, Any]) -> str:
    return _shared_acp_progress_message(params)


def _extract_output_delta_only(params: dict[str, Any]) -> str:
    return _event_extract_output_delta(params, include_part_text=False)


def _extract_opencode_reasoning_text(
    params: dict[str, Any],
    part: dict[str, Any],
    state: RuntimeThreadRunEventState,
) -> str:
    key = None
    for candidate in ("id", "partID", "partId", "part_id"):
        value = part.get(candidate)
        if isinstance(value, str) and value:
            key = value
            break

    full_text = part.get("text")
    if isinstance(full_text, str) and full_text:
        if key:
            state.reasoning_buffers[key] = full_text
        return full_text

    delta_text = _extract_output_delta_only(params)
    if not delta_text:
        return ""
    if key:
        combined = f"{state.reasoning_buffers.get(key, '')}{delta_text}"
        state.reasoning_buffers[key] = combined
        return combined
    return delta_text


def _normalize_opencode_tool_part(
    part: dict[str, Any],
    state: RuntimeThreadRunEventState,
    *,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    shape = OpenCodeToolPartShape.from_raw_part(part)
    if shape is None:
        return []

    event_timestamp = timestamp or now_iso()
    last_status = state.opencode_tool_status.get(shape.tool_id)

    events: list[RunEvent] = []
    if last_status is None or shape.status in {"running", "pending"}:
        if last_status != shape.status:
            events.append(
                ToolCall(
                    timestamp=event_timestamp,
                    tool_name=shape.tool_name,
                    tool_input=shape.input_payload,
                )
            )

    if shape.status == "completed" and last_status != shape.status:
        events.append(
            ToolResult(
                timestamp=event_timestamp,
                tool_name=shape.tool_name,
                status=shape.status,
                result=dict(shape.state_payload),
                error=None,
            )
        )
        exit_code = shape.state_payload.get("exitCode")
        if exit_code is not None:
            events.append(
                OutputDelta(
                    timestamp=event_timestamp,
                    content=f"exit {exit_code}",
                    delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
                )
            )
    elif shape.status in {"error", "failed"} and last_status != shape.status:
        events.append(
            ToolResult(
                timestamp=event_timestamp,
                tool_name=shape.tool_name,
                status=shape.status,
                result=dict(shape.state_payload),
                error=shape.error,
            )
        )
        error = shape.error
        if isinstance(error, dict):
            error = error.get("message") or error.get("error")
        if isinstance(error, str) and error.strip():
            events.append(
                OutputDelta(
                    timestamp=event_timestamp,
                    content=f"error: {error.strip()}",
                    delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
                )
            )

    if shape.status:
        state.opencode_tool_status[shape.tool_id] = shape.status
    return events


def _normalize_opencode_patch_part(
    part: dict[str, Any],
    state: RuntimeThreadRunEventState,
    *,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    patch_hash = part.get("hash")
    if isinstance(patch_hash, str) and patch_hash:
        if patch_hash in state.opencode_patch_hashes:
            return []
        state.opencode_patch_hashes.add(patch_hash)

    lines: list[str] = []
    files = part.get("files")
    if isinstance(files, list) and files:
        lines.append("file update")
        for entry in files:
            if isinstance(entry, dict):
                path = entry.get("path") or entry.get("file")
                action = entry.get("status") or "M"
                if isinstance(path, str) and path:
                    lines.append(f"{action} {path}")
            elif isinstance(entry, str) and entry:
                lines.append(f"M {entry}")
    elif isinstance(files, str) and files:
        lines.extend(["file update", f"M {files}"])

    return [
        OutputDelta(
            timestamp=timestamp or now_iso(),
            content=line,
            delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
        )
        for line in lines
    ]


def _extract_opencode_usage_part(part: dict[str, Any]) -> Optional[dict[str, Any]]:
    shape = TokenUsageShape.from_raw(part)
    if shape.is_empty():
        return None
    return shape.to_dict()


def _extract_message_phase(params: dict[str, Any]) -> Optional[str]:
    phase = params.get("phase")
    if isinstance(phase, str) and phase.strip():
        return phase.strip().lower()
    info = _extract_message_info(params)
    nested_phase = info.get("phase")
    if isinstance(nested_phase, str) and nested_phase.strip():
        return nested_phase.strip().lower()
    return None


def _extract_already_streamed_flag(payload: dict[str, Any]) -> bool:
    for key in ("already_streamed", "alreadyStreamed"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    info = _extract_message_info(payload)
    for key in ("already_streamed", "alreadyStreamed"):
        value = info.get(key)
        if isinstance(value, bool):
            return value
    return False


def _extract_usage(params: dict[str, Any]) -> Optional[dict[str, Any]]:
    result = _shared_extract_usage(params)
    return result if result else None


def _request_id_for_event(method: str, params: dict[str, Any]) -> str:
    for key in ("id", "requestId", "request_id", "itemId", "item_id"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    turn_id = params.get("turnId") or params.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        return turn_id
    return method


def _approval_summary(method: str, params: dict[str, Any]) -> str:
    if method == "item/commandExecution/requestApproval":
        command = params.get("command")
        if not command:
            item = _coerce_dict(params.get("item"))
            command = item.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command).strip()
        if isinstance(command, str) and command.strip():
            return command
        return "Command approval requested"
    if method == "item/fileChange/requestApproval":
        files = params.get("files")
        if isinstance(files, list):
            paths = [str(entry) for entry in files if isinstance(entry, str)]
            if paths:
                return ", ".join(paths)
        return "File approval requested"
    return "Approval requested"


def _extract_message_text(params: dict[str, Any]) -> str:
    shared_text = _shared_acp_message_text(params)
    if shared_text:
        return shared_text
    properties = _coerce_dict(params.get("properties"))
    shared_properties_text = _shared_acp_message_text(properties)
    if shared_properties_text:
        return shared_properties_text
    for key in ("text", "message"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    content = params.get("content")
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if isinstance(part_type, str) and part_type != "text":
                continue
            part_text = part.get("text")
            if isinstance(part_text, str) and part_text:
                text_parts.append(part_text)
        if text_parts:
            return "".join(text_parts)
    elif isinstance(content, str) and content.strip():
        return content
    parts = params.get("parts")
    if isinstance(parts, list):
        text_parts_from_parts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                text_parts_from_parts.append(text)
        if text_parts_from_parts:
            return "".join(text_parts_from_parts)
    return ""


def _extract_message_info(params: dict[str, Any]) -> dict[str, Any]:
    info = params.get("info")
    if isinstance(info, dict):
        return info
    properties = _coerce_dict(params.get("properties"))
    nested = properties.get("info")
    return nested if isinstance(nested, dict) else {}


def _extract_message_id(params: dict[str, Any]) -> Optional[str]:
    return _event_extract_message_id(params)


def _extract_message_role(params: dict[str, Any]) -> Optional[str]:
    return _event_extract_message_role(params)


def _extract_part_message_id(params: dict[str, Any]) -> Optional[str]:
    return _event_extract_part_message_id(params)


def _extract_part_id(
    params: dict[str, Any], *, part: Optional[dict[str, Any]] = None
) -> Optional[str]:
    return _event_extract_part_id(params, part=part)


__all__ = [
    "RuntimeThreadRunEventState",
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
    "completion_source_from_outcome",
    "_extract_output_delta",
    "_output_delta_type_for_method",
    "_normalize_tool_name",
    "_extract_agent_message_text",
    "_extract_usage",
    "_coerce_dict",
    "_reasoning_buffer_key",
    "_is_commentary_agent_message",
]
