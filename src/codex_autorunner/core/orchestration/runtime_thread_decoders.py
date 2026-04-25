from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .runtime_thread_events import RuntimeThreadRunEventState

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
    Failed,
    OutputDelta,
    RunEvent,
    RunNotice,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from ..time_utils import now_iso
from .codex_item_normalizers import (
    extract_agent_message_text as _extract_agent_message_text,
)
from .codex_item_normalizers import (
    is_commentary_agent_message as _is_commentary_agent_message,
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


@dataclass
class DecoderContext:
    timestamp: str
    raw_message: dict[str, Any]
    acp_lifecycle: Any


class MessageDecoder(ABC):
    @abstractmethod
    def methods(self) -> frozenset[str]: ...

    def can_decode(self, method: str) -> bool:
        return method in self.methods()

    @abstractmethod
    def decode(
        self,
        method: str,
        params: dict[str, Any],
        state: RuntimeThreadRunEventState,
        ctx: DecoderContext,
    ) -> list[RunEvent]: ...


class DecoderRegistry:
    def __init__(self) -> None:
        self._exact: dict[str, MessageDecoder] = {}
        self._decoders: list[MessageDecoder] = []

    def register(self, decoder: MessageDecoder) -> None:
        for method in decoder.methods():
            self._exact[method] = decoder
        self._decoders.append(decoder)

    def has_decoder(self, method: str) -> bool:
        if method in self._exact:
            return True
        return any(d.can_decode(method) for d in self._decoders)

    def decode(
        self,
        method: str,
        params: dict[str, Any],
        state: RuntimeThreadRunEventState,
        ctx: DecoderContext,
    ) -> list[RunEvent]:
        decoder = self._exact.get(method)
        if decoder is not None:
            return decoder.decode(method, params, state, ctx)
        for decoder in self._decoders:
            if decoder.can_decode(method):
                return decoder.decode(method, params, state, ctx)
        return []


def _coerce_dict(value: Any) -> dict[str, Any]:
    return _event_coerce_dict(value)


def _extract_usage(params: dict[str, Any]) -> Optional[dict[str, Any]]:
    result = _shared_extract_usage(params)
    return result if result else None


def _extract_output_delta(params: dict[str, Any]) -> str:
    return _event_extract_output_delta(params, include_part_text=True)


def _extract_output_delta_only(params: dict[str, Any]) -> str:
    return _event_extract_output_delta(params, include_part_text=False)


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


def _extract_message_phase(params: dict[str, Any]) -> Optional[str]:
    phase = params.get("phase")
    if isinstance(phase, str) and phase.strip():
        return phase.strip().lower()
    info = _extract_message_info(params)
    nested_phase = info.get("phase")
    if isinstance(nested_phase, str) and nested_phase.strip():
        return nested_phase.strip().lower()
    return None


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


def _extract_message_part(params: dict[str, Any]) -> dict[str, Any]:
    return _event_extract_message_part(params)


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
    delta_type = str(params.get("deltaType") or params.get("delta_type") or "").strip()
    if not delta_type:
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


_APPROVAL_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }
)


class CodexItemDecoder(MessageDecoder):

    def methods(self) -> frozenset[str]:
        return (
            frozenset(
                {
                    "item/reasoning/summaryTextDelta",
                    "item/completed",
                    "item/agentMessage/delta",
                    "item/toolCall/start",
                    "item/toolCall/end",
                }
            )
            | _APPROVAL_METHODS
        )

    def decode(self, method, params, state, ctx):
        ts = ctx.timestamp

        if method == "item/reasoning/summaryTextDelta":
            delta = params.get("delta")
            if not isinstance(delta, str) or not delta:
                return []
            key = _reasoning_buffer_key(params)
            if key:
                delta = f"{state.reasoning_buffers.get(key, '')}{delta}"
                state.reasoning_buffers[key] = delta
            return [RunNotice(timestamp=ts, kind="thinking", message=delta)]

        if method == "item/completed":
            return self._decode_item_completed(params, state, ts)

        if method == "item/agentMessage/delta":
            return _assistant_stream_events(params, state, timestamp=ts)

        if method == "item/toolCall/start":
            tool_name, tool_input = _normalize_tool_name(params)
            return [
                ToolCall(
                    timestamp=ts,
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
                    timestamp=ts,
                    tool_name=tool_name or str(params.get("name") or "toolCall"),
                    status="error" if error else "completed",
                    result=result,
                    error=error,
                )
            ]

        request_id = _request_id_for_event(method, params)
        summary = _approval_summary(method, params)
        return [
            ApprovalRequested(
                timestamp=ts,
                request_id=request_id,
                description=summary,
                context=dict(params),
            )
        ]

    def _decode_item_completed(self, params, state, ts):
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
                        timestamp=ts,
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
                    timestamp=ts,
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
                        timestamp=ts,
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
                    timestamp=ts,
                    tool_name=tool_name,
                    tool_input=tool_input,
                )
            ]
        return []


class ACPPromptTurnDecoder(MessageDecoder):
    _PROMPT_OUTPUT_METHODS = frozenset(
        {
            "prompt/output",
            "prompt/delta",
            "prompt/progress",
            "turn/progress",
        }
    )
    _MESSAGE_METHODS = frozenset({"prompt/message", "turn/message"})
    _FAILED_METHODS = frozenset({"prompt/failed", "turn/failed"})
    _COMPLETION_METHODS = frozenset(
        {
            "prompt/completed",
            "turn/completed",
            "prompt/cancelled",
            "turn/cancelled",
        }
    )

    def methods(self) -> frozenset[str]:
        return (
            self._PROMPT_OUTPUT_METHODS
            | self._MESSAGE_METHODS
            | self._FAILED_METHODS
            | self._COMPLETION_METHODS
        )

    def decode(self, method, params, state, ctx):
        ts = ctx.timestamp
        acp = ctx.acp_lifecycle

        if method in self._PROMPT_OUTPUT_METHODS:
            output_events = _assistant_stream_events(
                params,
                state,
                timestamp=ts,
            )
            if output_events:
                return output_events
            usage = _extract_usage(params)
            if usage is not None:
                state.token_usage = dict(usage)
                return [TokenUsage(timestamp=ts, usage=dict(usage))]
            progress_message = acp.progress_message
            if progress_message:
                return [
                    RunNotice(
                        timestamp=ts,
                        kind="progress",
                        message=progress_message,
                    )
                ]
            return []

        if method in self._MESSAGE_METHODS:
            content = acp.assistant_text
            if not content:
                return []
            if acp.message_phase == "commentary":
                return [
                    RunNotice(
                        timestamp=ts,
                        kind="commentary",
                        message=content,
                        data={"already_streamed": acp.already_streamed},
                    )
                ]
            state.note_message_text(content)
            return [
                OutputDelta(
                    timestamp=ts,
                    content=content,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
                )
            ]

        if method in self._FAILED_METHODS:
            error_message = _shared_acp_error_message(params)
            state.last_error_message = error_message or "Turn error"
            return [
                Failed(
                    timestamp=ts,
                    error_message=state.last_error_message,
                )
            ]

        events: list[RunEvent] = []
        content = acp.assistant_text
        if content:
            state.note_message_text(content)
            events.append(
                OutputDelta(
                    timestamp=ts,
                    content=content,
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
                )
            )
        if acp.runtime_terminal_status == "ok":
            state.completed_seen = True
        return events


class PermissionDecoder(MessageDecoder):
    _REQUEST_METHODS = frozenset(
        {
            "permission/requested",
            "session/request_permission",
        }
    )

    def methods(self) -> frozenset[str]:
        return frozenset(
            {
                "permission/requested",
                "session/request_permission",
                "permission/decision",
                "permission",
                "question",
            }
        )

    def decode(self, method, params, state, ctx):
        ts = ctx.timestamp
        acp = ctx.acp_lifecycle

        if method in self._REQUEST_METHODS:
            request_id = acp.permission_request_id or _request_id_for_event(
                method, params
            )
            description = str(
                acp.permission_description or "Approval requested"
            ).strip()
            context = _coerce_dict(params.get("context")) or dict(params)
            return [
                ApprovalRequested(
                    timestamp=ts,
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
                    timestamp=ts,
                    kind="approval",
                    message=message,
                    data=dict(params),
                )
            ]

        if method == "permission":
            request_id = _request_id_for_event(method, params)
            description = str(
                params.get("reason") or params.get("message") or "Approval requested"
            ).strip()
            return [
                ApprovalRequested(
                    timestamp=ts,
                    request_id=request_id,
                    description=description or "Approval requested",
                    context=dict(params),
                )
            ]

        request_id = _request_id_for_event(method, params)
        question_text = str(params.get("question") or "").strip()
        return [
            ApprovalRequested(
                timestamp=ts,
                request_id=request_id,
                description=question_text or "Question pending",
                context=dict(params),
            )
        ]


class UsageDecoder(MessageDecoder):

    def methods(self) -> frozenset[str]:
        return frozenset(
            {
                "token/usage",
                "usage",
                "turn/tokenUsage",
                "turn/usage",
                "thread/tokenUsage/updated",
            }
        )

    def decode(self, method, params, state, ctx):
        ts = ctx.timestamp
        usage = _extract_usage(params)
        if usage is None:
            return []
        state.token_usage = dict(usage)
        return [TokenUsage(timestamp=ts, usage=dict(usage))]


class SessionUpdateDecoder(MessageDecoder):

    def methods(self) -> frozenset[str]:
        return frozenset(
            {
                "session/update",
                "session.idle",
                "session.status",
                "session/status",
            }
        )

    def decode(self, method, params, state, ctx):
        ts = ctx.timestamp
        acp = ctx.acp_lifecycle

        if method == "session/update":
            return self._decode_session_update(params, state, ts, acp)

        if method == "session.idle":
            if acp.runtime_terminal_status == "ok":
                state.completed_seen = True
            return []

        if acp.runtime_terminal_status == "ok":
            state.completed_seen = True
            return []
        status_type = acp.session_status or ""
        if status_type and status_type != "idle":
            return [
                RunNotice(
                    timestamp=ts,
                    kind="progress",
                    message=f"agent {status_type}",
                )
            ]
        return []

    def _decode_session_update(self, params, state, ts, acp):
        update = _extract_session_update(params)
        update_kind = acp.session_update_kind or ""
        if update_kind == "agent_message_chunk":
            if acp.message_phase == "commentary":
                commentary_text = acp.output_delta or _extract_output_delta(
                    _extract_session_update_message_params(update)
                )
                if not commentary_text:
                    return []
                return [
                    RunNotice(
                        timestamp=ts,
                        kind="commentary",
                        message=commentary_text,
                        data={"already_streamed": acp.already_streamed},
                    )
                ]
            return _assistant_stream_events(
                _extract_session_update_message_params(update),
                state,
                timestamp=ts,
            )
        if update_kind == "agent_thought_chunk":
            progress_message = _extract_session_update_text(update)
            if not progress_message:
                return []
            return [
                RunNotice(
                    timestamp=ts,
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
                    timestamp=ts,
                    usage=dict(session_usage),
                )
            ]
        return []


class OpenCodeMessageDecoder(MessageDecoder):

    def methods(self) -> frozenset[str]:
        return frozenset(
            {
                "message.part.updated",
                "message.part.delta",
                "message.updated",
                "message.completed",
                "message.delta",
            }
        )

    def decode(self, method, params, state, ctx):
        ts = ctx.timestamp

        if method in {"message.part.updated", "message.part.delta"}:
            return _normalize_message_part_updated(params, state, timestamp=ts)

        if method in {"message.updated", "message.completed"}:
            return self._decode_message_lifecycle(params, state, ts)

        return self._decode_message_delta(params, state, ts)

    def _decode_message_lifecycle(self, params, state, ts):
        role_events = state.note_message_role(
            _extract_message_id(params),
            _extract_message_role(params),
            timestamp=ts,
        )
        if _extract_message_phase(params) == "commentary":
            content = _extract_message_text(params)
            if not content:
                return role_events
            return role_events + [
                RunNotice(
                    timestamp=ts,
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
                timestamp=ts,
                content=content,
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
            )
        ]

    def _decode_message_delta(self, params, state, ts):
        part_type = _event_extract_part_type(
            params, part_types=state.opencode_part_types
        )
        if part_type and part_type != "text":
            return []
        return _assistant_stream_events(params, state, timestamp=ts)


class StreamDeltaDecoder(MessageDecoder):

    def methods(self) -> frozenset[str]:
        return frozenset({"turn/streamDelta"})

    def can_decode(self, method: str) -> bool:
        return method == "turn/streamDelta" or "outputdelta" in method.lower()

    def decode(self, method, params, state, ctx):
        return _output_delta_events(
            method,
            params,
            state,
            timestamp=ctx.timestamp,
        )


class ErrorDecoder(MessageDecoder):

    def methods(self) -> frozenset[str]:
        return frozenset({"turn/error", "error"})

    def decode(self, method, params, state, ctx):
        ts = ctx.timestamp

        if method == "turn/error":
            turn_error_message: Any = params.get("message")
            if (
                not isinstance(turn_error_message, str)
                or not turn_error_message.strip()
            ):
                turn_error_message = "Turn error"
            state.last_error_message = str(turn_error_message)
            return [Failed(timestamp=ts, error_message=str(turn_error_message))]

        error = _coerce_dict(params.get("error"))
        generic_error_message: Any = error.get("message") or params.get("message")
        if (
            not isinstance(generic_error_message, str)
            or not generic_error_message.strip()
        ):
            generic_error_message = "Turn error"
        state.last_error_message = str(generic_error_message)
        return [Failed(timestamp=ts, error_message=str(generic_error_message))]


def build_default_decoder_registry() -> DecoderRegistry:
    registry = DecoderRegistry()
    registry.register(CodexItemDecoder())
    registry.register(ACPPromptTurnDecoder())
    registry.register(PermissionDecoder())
    registry.register(UsageDecoder())
    registry.register(SessionUpdateDecoder())
    registry.register(OpenCodeMessageDecoder())
    registry.register(StreamDeltaDecoder())
    registry.register(ErrorDecoder())
    return registry


__all__ = [
    "DecoderContext",
    "DecoderRegistry",
    "MessageDecoder",
    "CodexItemDecoder",
    "ACPPromptTurnDecoder",
    "PermissionDecoder",
    "UsageDecoder",
    "SessionUpdateDecoder",
    "OpenCodeMessageDecoder",
    "StreamDeltaDecoder",
    "ErrorDecoder",
    "build_default_decoder_registry",
]
