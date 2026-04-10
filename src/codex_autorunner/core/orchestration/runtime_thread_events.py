from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

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
    TokenUsage,
    ToolCall,
    ToolResult,
)
from ..sse import SSEEvent, parse_sse_lines
from ..time_utils import now_iso
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
from .runtime_threads import RuntimeThreadOutcome
from .stream_text_merge import merge_assistant_stream_text

_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}


def _runtime_raw_event_key(raw_event: Any) -> str:
    if isinstance(raw_event, (dict, list)):
        return json.dumps(
            raw_event,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return str(raw_event)


def merge_runtime_thread_raw_events(
    streamed_raw_events: list[Any] | tuple[Any, ...],
    result_raw_events: list[Any] | tuple[Any, ...],
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
        )
    method = payload.get("method")
    params = payload.get("params")
    if isinstance(method, str) and isinstance(params, dict):
        return normalize_runtime_thread_message(
            method,
            params,
            state,
            timestamp=timestamp,
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
    """Prefer a streamed completion over a later transport error or interrupt."""

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


def _extract_status_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("type", "status", "state"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def _status_indicates_successful_completion(
    status: Any, *, assume_true_when_missing: bool
) -> bool:
    normalized = _extract_status_value(status)
    if not isinstance(normalized, str):
        return assume_true_when_missing
    return normalized.lower() in {
        "completed",
        "complete",
        "done",
        "success",
        "succeeded",
        "idle",
    }


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
            )
    return normalize_runtime_thread_message(
        sse_event.event,
        payload,
        state,
        timestamp=timestamp,
    )


def normalize_runtime_thread_message(
    method: str,
    params: dict[str, Any],
    state: RuntimeThreadRunEventState,
    *,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    event_timestamp = timestamp or now_iso()
    method_lower = method.lower()
    if not method:
        return []
    state.note_runtime_progress(method, timestamp=event_timestamp)

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
        progress_message = _extract_acp_progress_message(params)
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
        update_kind = _extract_session_update_kind(update)
        if update_kind == "agent_message_chunk":
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
        content = _extract_acp_final_message(params)
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

    if method in {"permission/requested", "session/request_permission"}:
        request_id = _request_id_for_event(method, params)
        description = str(
            params.get("description") or params.get("message") or "Approval requested"
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
        error_message = str(
            params.get("error") or params.get("message") or "Turn error"
        ).strip()
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
        content = _extract_acp_final_message(params)
        if content:
            state.note_message_text(content)
            events.append(
                OutputDelta(
                    timestamp=event_timestamp,
                    content=content,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
                )
            )
        if _status_indicates_successful_completion(
            params.get("status"),
            assume_true_when_missing=method.endswith("completed"),
        ):
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
                return []
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
        if _status_indicates_successful_completion(
            params.get("status") or params.get("turn"),
            assume_true_when_missing=True,
        ):
            state.completed_seen = True
        return []

    if method == "session.idle":
        state.completed_seen = True
        return []

    if method == "session.status":
        status = _coerce_dict(params.get("status"))
        if not status:
            properties = _coerce_dict(params.get("properties"))
            status = _coerce_dict(properties.get("status")) if properties else {}
        if _status_indicates_successful_completion(
            status, assume_true_when_missing=False
        ):
            state.completed_seen = True
            return []
        status_type = ""
        if isinstance(status, dict):
            status_type = (
                str(status.get("type") or status.get("status") or "").strip().lower()
            )
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
        return []
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
    return _coerce_dict(params.get("update"))


def _extract_session_update_kind(update: dict[str, Any]) -> str:
    for key in ("sessionUpdate", "session_update"):
        value = update.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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
    content = update.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, dict):
        for key in ("text", "message", "status"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value
        progress_message = _extract_acp_progress_message(content)
        if progress_message:
            return progress_message
        output_delta = _extract_output_delta(content)
        if output_delta:
            return output_delta
    if isinstance(content, list):
        text_parts: list[str] = []
        for entry in content:
            if isinstance(entry, str) and entry:
                text_parts.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            if isinstance(entry_type, str) and entry_type not in (
                "text",
                "output_text",
                "message",
            ):
                continue
            entry_text = ""
            message_text = entry.get("message")
            if isinstance(message_text, str) and message_text:
                entry_text = message_text
            if not entry_text:
                entry_text = _extract_output_delta(entry)
            if not entry_text:
                entry_text = _extract_acp_progress_message(entry)
            if entry_text:
                text_parts.append(entry_text)
        if text_parts:
            return "".join(text_parts)
    return _extract_acp_progress_message(update)


def _extract_acp_progress_message(params: dict[str, Any]) -> str:
    for key in ("message", "status"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_acp_final_message(params: dict[str, Any]) -> str:
    for key in ("finalOutput", "final_output", "message"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


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


def _output_delta_type_for_method(method: str) -> str:
    normalized = method.strip().lower()
    if normalized in {
        "item/commandexecution/outputdelta",
        "item/filechange/outputdelta",
    }:
        return RUN_EVENT_DELTA_TYPE_LOG_LINE
    return RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM


def _normalize_opencode_tool_part(
    part: dict[str, Any],
    state: RuntimeThreadRunEventState,
    *,
    timestamp: Optional[str] = None,
) -> list[RunEvent]:
    event_timestamp = timestamp or now_iso()
    tool_name = part.get("tool") or part.get("name") or ""
    if not isinstance(tool_name, str) or not tool_name.strip():
        return []

    tool_id = part.get("callID") or part.get("id") or tool_name
    tool_id_text = str(tool_id).strip() if tool_id is not None else tool_name.strip()
    state_payload = _coerce_dict(part.get("state"))
    status = state_payload.get("status")
    status_text = str(status).strip().lower() if isinstance(status, str) else ""
    last_status = state.opencode_tool_status.get(tool_id_text)

    input_payload: dict[str, Any] = {}
    for key in ("input", "command", "cmd", "script"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            input_payload[key] = value.strip()
            break
    if not input_payload:
        args = part.get("args") or part.get("arguments") or part.get("params")
        if isinstance(args, dict):
            for key in ("command", "cmd", "script", "input"):
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    input_payload[key] = value.strip()
                    break
        elif isinstance(args, str) and args.strip():
            input_payload["input"] = args.strip()

    events: list[RunEvent] = []
    if last_status is None or status_text in {"running", "pending"}:
        if last_status != status_text:
            events.append(
                ToolCall(
                    timestamp=event_timestamp,
                    tool_name=tool_name.strip(),
                    tool_input=input_payload,
                )
            )

    if status_text == "completed" and last_status != status_text:
        events.append(
            ToolResult(
                timestamp=event_timestamp,
                tool_name=tool_name.strip(),
                status=status_text,
                result=dict(state_payload),
                error=None,
            )
        )
        exit_code = state_payload.get("exitCode")
        if exit_code is not None:
            events.append(
                OutputDelta(
                    timestamp=event_timestamp,
                    content=f"exit {exit_code}",
                    delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
                )
            )
    elif status_text in {"error", "failed"} and last_status != status_text:
        events.append(
            ToolResult(
                timestamp=event_timestamp,
                tool_name=tool_name.strip(),
                status=status_text,
                result=dict(state_payload),
                error=state_payload.get("error"),
            )
        )
        error = state_payload.get("error")
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

    if status_text:
        state.opencode_tool_status[tool_id_text] = status_text
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
    usage: dict[str, Any] = {}

    total_tokens = part.get("totalTokens")
    if isinstance(total_tokens, int):
        usage["totalTokens"] = total_tokens
    input_tokens = part.get("inputTokens")
    if isinstance(input_tokens, int):
        usage["inputTokens"] = input_tokens
    cached_tokens = part.get("cachedInputTokens")
    if isinstance(cached_tokens, int):
        usage["cachedInputTokens"] = cached_tokens
    output_tokens = part.get("outputTokens")
    if isinstance(output_tokens, int):
        usage["outputTokens"] = output_tokens
    reasoning_tokens = part.get("reasoningTokens")
    if isinstance(reasoning_tokens, int):
        usage["reasoningTokens"] = reasoning_tokens
    context_window = part.get("modelContextWindow")
    if isinstance(context_window, int):
        usage["modelContextWindow"] = context_window

    if usage:
        return usage
    return None


def _normalize_tool_name(
    params: dict[str, Any],
    *,
    item: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, Any]]:
    item_dict = item if isinstance(item, dict) else _coerce_dict(params.get("item"))
    item_type = item_dict.get("type")

    if item_type == "commandExecution":
        command = item_dict.get("command")
        if not command:
            command = params.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command).strip()
        if isinstance(command, str) and command:
            return command, {"command": command}
        return "commandExecution", {}

    if item_type == "fileChange":
        files = item_dict.get("files")
        if isinstance(files, list):
            paths = [str(entry) for entry in files if isinstance(entry, str)]
            if paths:
                return "fileChange", {"files": paths}
        return "fileChange", {}

    if item_type == "tool":
        name = item_dict.get("name") or item_dict.get("tool") or item_dict.get("id")
        if isinstance(name, str) and name:
            return name, {}
        return "tool", {}

    tool_call = _coerce_dict(item_dict.get("toolCall") or item_dict.get("tool_call"))
    name = tool_call.get("name") or params.get("toolName") or params.get("tool_name")
    if isinstance(name, str) and name:
        input_payload = tool_call.get("input")
        if isinstance(input_payload, dict):
            return name, input_payload
        input_payload = params.get("toolInput") or params.get("input")
        if isinstance(input_payload, dict):
            return name, input_payload
        return name, {}
    return "", {}


def _reasoning_buffer_key(
    params: dict[str, Any],
    *,
    item: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    for key in ("itemId", "item_id", "turnId", "turn_id"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    if isinstance(item, dict):
        for key in ("id", "itemId", "turnId", "turn_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _extract_agent_message_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text
    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for entry in content:
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            if entry_type not in (None, "output_text", "text", "message"):
                continue
            candidate = entry.get("text")
            if isinstance(candidate, str) and candidate.strip():
                parts.append(candidate)
        if parts:
            return "".join(parts)
    return ""


def _is_commentary_agent_message(item: dict[str, Any]) -> bool:
    return str(item.get("phase") or "").strip().lower() == "commentary"


def _extract_usage(params: dict[str, Any]) -> Optional[dict[str, Any]]:
    usage = params.get("usage") or params.get("tokenUsage")
    if isinstance(usage, dict):
        return usage
    return None


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
    "_extract_output_delta",
    "_output_delta_type_for_method",
    "_normalize_tool_name",
    "_extract_agent_message_text",
    "_extract_usage",
    "_coerce_dict",
]
