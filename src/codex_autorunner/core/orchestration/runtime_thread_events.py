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
)
from ..sse import SSEEvent, parse_sse_lines
from ..time_utils import now_iso
from .runtime_threads import RuntimeThreadOutcome

_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}


def _merge_assistant_stream(current: str, incoming: str) -> str:
    if not incoming:
        return current
    if not current:
        return incoming
    if incoming == current:
        return current
    if len(incoming) > len(current) and incoming.startswith(current):
        return incoming
    max_overlap = min(len(current), max(len(incoming) - 1, 0))
    for overlap in range(max_overlap, 0, -1):
        if current[-overlap:] == incoming[:overlap]:
            return f"{current}{incoming[overlap:]}"
    return f"{current}{incoming}"


@dataclass
class RuntimeThreadRunEventState:
    reasoning_buffers: dict[str, str] = field(default_factory=dict)
    assistant_stream_text: str = ""
    assistant_message_text: str = ""
    token_usage: Optional[dict[str, Any]] = None
    last_error_message: Optional[str] = None
    completed_seen: bool = False
    message_roles: dict[str, str] = field(default_factory=dict)
    pending_stream_by_message: dict[str, str] = field(default_factory=dict)
    pending_stream_no_id: str = ""
    message_roles_seen: bool = False
    opencode_tool_status: dict[str, str] = field(default_factory=dict)
    opencode_patch_hashes: set[str] = field(default_factory=set)

    def note_stream_text(self, text: str) -> None:
        if isinstance(text, str) and text:
            self.assistant_stream_text = _merge_assistant_stream(
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

    def note_message_role(
        self,
        message_id: Optional[str],
        role: Optional[str],
    ) -> list[RunEvent]:
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
                    timestamp=now_iso(),
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
                    timestamp=now_iso(),
                    content=pending_no_id,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                )
            )
        return events

    def note_message_part_text(
        self,
        message_id: Optional[str],
        text: str,
    ) -> list[RunEvent]:
        if not isinstance(text, str) or not text:
            return []
        if message_id is None:
            if not self.message_roles_seen:
                self.note_stream_text(text)
                return [
                    OutputDelta(
                        timestamp=now_iso(),
                        content=text,
                        delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                    )
                ]
            self.pending_stream_no_id = _merge_assistant_stream(
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
                    timestamp=now_iso(),
                    content=text,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                )
            ]
        self.pending_stream_by_message[message_id] = _merge_assistant_stream(
            self.pending_stream_by_message.get(message_id, ""),
            text,
        )
        return []


async def normalize_runtime_thread_raw_event(
    raw_event: str,
    state: RuntimeThreadRunEventState,
) -> list[RunEvent]:
    events: list[RunEvent] = []
    async for sse_event in _parse_runtime_thread_sse(raw_event):
        events.extend(_normalize_sse_event(sse_event, state))
    return events


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
    """Prefer a streamed completion over a later transport error."""

    if outcome.status != "error" or not state.completed_seen:
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
) -> list[RunEvent]:
    payload = _load_json_object(sse_event.data)
    if sse_event.event in {"app-server", "event"}:
        message = payload.get("message")
        if isinstance(message, dict):
            return _normalize_message_event(
                str(message.get("method") or ""),
                _coerce_dict(message.get("params")),
                state,
            )
    return _normalize_message_event(
        sse_event.event,
        payload,
        state,
    )


def _normalize_message_event(
    method: str,
    params: dict[str, Any],
    state: RuntimeThreadRunEventState,
) -> list[RunEvent]:
    method_lower = method.lower()
    if not method:
        return []

    if method == "item/reasoning/summaryTextDelta":
        delta = params.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        key = _reasoning_buffer_key(params)
        if key:
            delta = f"{state.reasoning_buffers.get(key, '')}{delta}"
            state.reasoning_buffers[key] = delta
        return [RunNotice(timestamp=now_iso(), kind="thinking", message=delta)]

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
            content = _extract_agent_message_text(item)
            if not content:
                return []
            state.note_message_text(content)
            return [
                OutputDelta(
                    timestamp=now_iso(),
                    content=content,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
                )
            ]
        tool_name, tool_input = _normalize_tool_name(params, item=item)
        if tool_name:
            return [
                ToolCall(
                    timestamp=now_iso(),
                    tool_name=tool_name,
                    tool_input=tool_input,
                )
            ]
        return []

    if method == "item/agentMessage/delta":
        return _assistant_stream_events(params, state)

    if method == "message.part.updated":
        return _normalize_message_part_updated(params, state)

    if method in _APPROVAL_METHODS:
        request_id = _request_id_for_event(method, params)
        summary = _approval_summary(method, params)
        return [
            ApprovalRequested(
                timestamp=now_iso(),
                request_id=request_id,
                description=summary,
                context=dict(params),
            )
        ]

    if method == "item/toolCall/start":
        tool_name, tool_input = _normalize_tool_name(params)
        return [
            ToolCall(
                timestamp=now_iso(),
                tool_name=tool_name or "toolCall",
                tool_input=tool_input,
            )
        ]

    if method == "item/toolCall/end":
        return []

    if method == "usage":
        usage = _extract_usage(params)
        if usage is None:
            return []
        state.token_usage = dict(usage)
        return [TokenUsage(timestamp=now_iso(), usage=dict(usage))]

    if method == "permission":
        request_id = _request_id_for_event(method, params)
        description = str(
            params.get("reason") or params.get("message") or "Approval requested"
        ).strip()
        return [
            ApprovalRequested(
                timestamp=now_iso(),
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
                timestamp=now_iso(),
                request_id=request_id,
                description=question or "Question pending",
                context=dict(params),
            )
        ]

    if method in {"message.updated", "message.completed"}:
        role_events = state.note_message_role(
            _extract_message_id(params),
            _extract_message_role(params),
        )
        content = _extract_message_text(params)
        if not content:
            return role_events
        if _extract_message_role(params) == "user":
            return role_events
        state.note_message_text(content)
        return role_events + [
            OutputDelta(
                timestamp=now_iso(),
                content=content,
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
            )
        ]

    if method == "message.delta":
        return _assistant_stream_events(params, state)

    if method == "turn/streamDelta" or "outputdelta" in method_lower:
        return _output_delta_events(method, params, state)

    if method in {
        "turn/tokenUsage",
        "turn/usage",
        "thread/tokenUsage/updated",
    }:
        usage = _extract_usage(params)
        if usage is None:
            return []
        state.token_usage = dict(usage)
        return [TokenUsage(timestamp=now_iso(), usage=dict(usage))]

    if method == "turn/error":
        error_message = params.get("message")
        if not isinstance(error_message, str) or not error_message.strip():
            error_message = "Turn error"
        state.last_error_message = str(error_message)
        return [Failed(timestamp=now_iso(), error_message=str(error_message))]

    if method == "error":
        error = _coerce_dict(params.get("error"))
        error_message = error.get("message") or params.get("message")
        if not isinstance(error_message, str) or not error_message.strip():
            error_message = "Turn error"
        state.last_error_message = str(error_message)
        return [Failed(timestamp=now_iso(), error_message=str(error_message))]

    if method in {"turn/completed", "session.idle"}:
        state.completed_seen = True
        return []

    if method == "session.status":
        status = _coerce_dict(params.get("status"))
        status_type = str(status.get("type") or status.get("status") or "").strip()
        if status_type.lower() == "idle":
            state.completed_seen = True
            return []
        return []

    return []


def _assistant_stream_events(
    params: dict[str, Any],
    state: RuntimeThreadRunEventState,
) -> list[RunEvent]:
    content = _extract_output_delta(params)
    if not content:
        return []
    state.note_stream_text(content)
    return [
        OutputDelta(
            timestamp=now_iso(),
            content=content,
            delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
        )
    ]


def _normalize_message_part_updated(
    params: dict[str, Any],
    state: RuntimeThreadRunEventState,
) -> list[RunEvent]:
    part = _extract_message_part(params)
    if not part:
        content = _extract_output_delta(params)
        if not content:
            return []
        return state.note_message_part_text(_extract_part_message_id(params), content)

    if bool(part.get("ignored")):
        return []

    part_type = str(part.get("type") or "").strip().lower()
    if part_type in {"", "text"}:
        content = _extract_output_delta(params)
        if not content:
            return []
        return state.note_message_part_text(_extract_part_message_id(params), content)

    if part_type == "reasoning":
        content = _extract_opencode_reasoning_text(params, part, state)
        if not content:
            return []
        return [RunNotice(timestamp=now_iso(), kind="thinking", message=content)]

    if part_type == "tool":
        return _normalize_opencode_tool_part(part, state)

    if part_type == "patch":
        return _normalize_opencode_patch_part(part, state)

    if part_type == "usage":
        usage = _extract_opencode_usage_part(part)
        if usage is None:
            return []
        state.token_usage = dict(usage)
        return [TokenUsage(timestamp=now_iso(), usage=dict(usage))]

    return []


def _output_delta_events(
    method: str,
    params: dict[str, Any],
    state: RuntimeThreadRunEventState,
) -> list[RunEvent]:
    content = _extract_output_delta(params)
    if not content:
        return []
    delta_type = _output_delta_type_for_method(method)
    if delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM:
        state.note_stream_text(content)
    return [
        OutputDelta(
            timestamp=now_iso(),
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
    return value if isinstance(value, dict) else {}


def _extract_message_part(params: dict[str, Any]) -> dict[str, Any]:
    properties = _coerce_dict(params.get("properties"))
    part = properties.get("part")
    if isinstance(part, dict):
        return part
    part = params.get("part")
    if isinstance(part, dict):
        return part
    return {}


def _extract_output_delta(params: dict[str, Any]) -> str:
    for key in ("content", "delta", "text", "output"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested_text = value.get("text")
            if isinstance(nested_text, str) and nested_text:
                return nested_text
    properties = _coerce_dict(params.get("properties"))
    delta = _coerce_dict(properties.get("delta"))
    delta_text = delta.get("text")
    if isinstance(delta_text, str) and delta_text:
        return delta_text
    part = _coerce_dict(properties.get("part"))
    if part.get("type") == "text":
        part_text = part.get("text")
        if isinstance(part_text, str) and part_text:
            return part_text
    return ""


def _extract_output_delta_only(params: dict[str, Any]) -> str:
    for key in ("content", "delta", "text", "output"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested_text = value.get("text")
            if isinstance(nested_text, str) and nested_text:
                return nested_text
    properties = _coerce_dict(params.get("properties"))
    delta = _coerce_dict(properties.get("delta"))
    delta_text = delta.get("text")
    if isinstance(delta_text, str) and delta_text:
        return delta_text
    return ""


def _extract_opencode_reasoning_text(
    params: dict[str, Any],
    part: dict[str, Any],
    state: RuntimeThreadRunEventState,
) -> str:
    key = None
    for candidate in ("id", "partId"):
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
) -> list[RunEvent]:
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
                    timestamp=now_iso(),
                    tool_name=tool_name.strip(),
                    tool_input=input_payload,
                )
            )

    if status_text == "completed" and last_status != status_text:
        exit_code = state_payload.get("exitCode")
        if exit_code is not None:
            events.append(
                OutputDelta(
                    timestamp=now_iso(),
                    content=f"exit {exit_code}",
                    delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
                )
            )
    elif status_text in {"error", "failed"} and last_status != status_text:
        error = state_payload.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("error")
        if isinstance(error, str) and error.strip():
            events.append(
                OutputDelta(
                    timestamp=now_iso(),
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
            timestamp=now_iso(),
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
    for key in ("text", "message", "content"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _extract_message_info(params: dict[str, Any]) -> dict[str, Any]:
    info = params.get("info")
    if isinstance(info, dict):
        return info
    properties = _coerce_dict(params.get("properties"))
    nested = properties.get("info")
    return nested if isinstance(nested, dict) else {}


def _extract_message_id(params: dict[str, Any]) -> Optional[str]:
    info = _extract_message_info(params)
    for key in ("id", "messageID", "messageId", "message_id"):
        value = info.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("messageID", "messageId", "message_id"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_message_role(params: dict[str, Any]) -> Optional[str]:
    info = _extract_message_info(params)
    role = info.get("role")
    if isinstance(role, str) and role:
        return role
    role = params.get("role")
    if isinstance(role, str) and role:
        return role
    return None


def _extract_part_message_id(params: dict[str, Any]) -> Optional[str]:
    properties = _coerce_dict(params.get("properties"))
    part = _coerce_dict(properties.get("part"))
    for key in ("messageID", "messageId", "message_id"):
        value = part.get(key)
        if isinstance(value, str) and value:
            return value
    return None


__all__ = [
    "RuntimeThreadRunEventState",
    "normalize_runtime_thread_raw_event",
    "recover_post_completion_outcome",
    "terminal_run_event_from_outcome",
    "_extract_output_delta",
    "_output_delta_type_for_method",
    "_normalize_tool_name",
    "_extract_agent_message_text",
    "_extract_usage",
    "_coerce_dict",
    "_normalize_message_event",
]
