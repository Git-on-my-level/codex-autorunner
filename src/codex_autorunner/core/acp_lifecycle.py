from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional

from .text_utils import _normalize_optional_text

_SESSION_TURN_ID_FALLBACK_METHODS = frozenset(
    {
        "session/update",
        "session/request_permission",
    }
)
_IDLE_TERMINAL_METHODS = frozenset(
    {
        "session.idle",
        "session.status",
        "session/status",
    }
)
_TERMINAL_METHODS = frozenset(
    {
        "prompt/completed",
        "prompt/cancelled",
        "prompt/failed",
        "turn/completed",
        "turn/cancelled",
        "turn/failed",
        "session.idle",
        "session.status",
        "session/status",
    }
)
_SUCCESSFUL_COMPLETION_STATUSES = frozenset(
    {"completed", "complete", "done", "success", "succeeded", "idle"}
)
_INTERRUPTED_COMPLETION_STATUSES = frozenset(
    {"interrupted", "cancelled", "canceled", "aborted"}
)
_TEXT_PART_TYPES = frozenset({"text", "output_text", "message", "agentMessage"})


ACPRuntimeTerminalStatus = Literal["ok", "error", "interrupted"]


def coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def extract_identifier(payload: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        normalized = _normalize_optional_text(value)
        if normalized:
            return normalized
    return None


def extract_text(payload: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def extract_session_update(params: Mapping[str, Any]) -> dict[str, Any]:
    return coerce_mapping(params.get("update"))


def extract_session_update_kind(update: Mapping[str, Any]) -> Optional[str]:
    value = update.get("sessionUpdate")
    if value is None:
        value = update.get("session_update")
    return _normalize_optional_text(value)


def extract_usage(payload: Mapping[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        return dict(usage)
    token_usage = payload.get("tokenUsage")
    if isinstance(token_usage, Mapping):
        return dict(token_usage)
    return {}


def _string_from_value(
    value: Any,
    *,
    keys: tuple[str, ...],
    allowed_part_types: frozenset[str] = _TEXT_PART_TYPES,
) -> Optional[str]:
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, Mapping):
        for key in keys:
            nested = _string_from_value(
                value.get(key),
                keys=keys,
                allowed_part_types=allowed_part_types,
            )
            if nested:
                return nested
        return None
    if isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            if isinstance(entry, Mapping):
                entry_type = _normalize_optional_text(entry.get("type"))
                if entry_type and entry_type not in allowed_part_types:
                    continue
            nested = _string_from_value(
                entry,
                keys=keys,
                allowed_part_types=allowed_part_types,
            )
            if nested:
                parts.append(nested)
        return "".join(parts) or None
    return None


def extract_text_content(value: Any) -> str:
    return (
        _string_from_value(
            value,
            keys=("text", "message", "content", "item"),
        )
        or ""
    )


def extract_output_delta(payload: Mapping[str, Any]) -> str:
    text = _string_from_value(
        payload,
        keys=("delta", "text", "content", "output", "message", "item"),
    )
    if text:
        return text
    output = payload.get("output")
    if isinstance(output, Mapping):
        nested = _string_from_value(
            output,
            keys=("delta", "text", "content", "output", "message"),
        )
        if nested:
            return nested
    return ""


def extract_message_text(payload: Mapping[str, Any]) -> str:
    text = _string_from_value(
        payload,
        keys=(
            "finalOutput",
            "final_output",
            "text",
            "content",
            "message",
            "final_message",
            "output",
            "parts",
            "item",
        ),
    )
    if text:
        return text
    output = payload.get("output")
    if isinstance(output, Mapping):
        nested = _string_from_value(
            output,
            keys=("text", "content", "message"),
        )
        if nested:
            return nested
    return ""


def extract_progress_message(payload: Mapping[str, Any]) -> str:
    direct = extract_text(payload, "message", "status")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    text = extract_text_content(payload.get("content"))
    if text:
        return text
    status_value = _string_from_value(
        payload.get("content"),
        keys=("status", "message", "text"),
    )
    if status_value:
        return status_value
    return ""


def extract_error_message(
    payload: Mapping[str, Any],
    *,
    default: str = "Turn error",
) -> str:
    for key in ("message", "error", "reason"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            value = value.get("message") or value.get("error")
        text = _normalize_optional_text(value)
        if text:
            return text
    return default


def extract_permission_description(payload: Mapping[str, Any]) -> str:
    description = extract_text(payload, "description", "message")
    if description:
        return description
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, Mapping):
        return ""
    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, Mapping):
        command = raw_input.get("command")
        if isinstance(command, list):
            normalized = " ".join(str(part) for part in command).strip()
            if normalized:
                return normalized
        if isinstance(command, str) and command.strip():
            return command
    kind = _normalize_optional_text(tool_call.get("kind"))
    return kind or ""


def session_status_type(payload: Mapping[str, Any]) -> Optional[str]:
    status = payload.get("status")
    if isinstance(status, Mapping):
        for key in ("type", "status", "state"):
            normalized = _normalize_optional_text(status.get(key))
            if normalized:
                return normalized.lower()
        return None
    properties = payload.get("properties")
    if isinstance(properties, Mapping):
        nested_status = properties.get("status")
        if isinstance(nested_status, Mapping):
            for key in ("type", "status", "state"):
                normalized = _normalize_optional_text(nested_status.get(key))
                if normalized:
                    return normalized.lower()
    normalized = _normalize_optional_text(status)
    if normalized:
        return normalized.lower()
    return None


def status_indicates_successful_completion(
    status: Any, *, assume_true_when_missing: bool
) -> bool:
    normalized = _extract_status_value(status)
    if not isinstance(normalized, str):
        return assume_true_when_missing
    return normalized.lower() in _SUCCESSFUL_COMPLETION_STATUSES


def status_indicates_interrupted(status: Any) -> bool:
    normalized = _extract_status_value(status)
    if not isinstance(normalized, str):
        return False
    return normalized.lower() in _INTERRUPTED_COMPLETION_STATUSES


def _extract_status_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("type", "status", "state"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def is_idle_terminal(method: str, params: Mapping[str, Any]) -> bool:
    if method == "session.idle":
        return True
    if method in {"session.status", "session/status"}:
        return session_status_type(params) == "idle"
    return False


def terminal_status_for_method(method: str, params: Mapping[str, Any]) -> Optional[str]:
    if method in {"prompt/failed", "turn/failed"}:
        return _normalize_optional_text(params.get("status")) or "failed"
    if method in {"prompt/cancelled", "turn/cancelled"}:
        return _normalize_optional_text(params.get("status")) or "cancelled"
    if method in {"prompt/completed", "turn/completed"}:
        normalized = _normalize_optional_text(params.get("status"))
        if not normalized:
            for nested_key in ("turn", "prompt"):
                nested = params.get(nested_key)
                if not isinstance(nested, Mapping):
                    continue
                normalized = _extract_status_value(nested.get("status"))
                if normalized:
                    break
        if normalized:
            return normalized
        return "completed"
    if is_idle_terminal(method, params):
        return "completed"
    return None


def runtime_terminal_status_for_lifecycle(
    method: str, params: Mapping[str, Any]
) -> Optional[ACPRuntimeTerminalStatus]:
    terminal_status = terminal_status_for_method(method, params)
    if terminal_status is None:
        return None
    if status_indicates_interrupted(terminal_status):
        return "interrupted"
    if status_indicates_successful_completion(
        terminal_status,
        assume_true_when_missing=method.endswith("completed")
        or is_idle_terminal(method, params),
    ):
        return "ok"
    return "error"


def should_map_missing_turn_id(method: str, params: Mapping[str, Any]) -> bool:
    if method in _SESSION_TURN_ID_FALLBACK_METHODS:
        return True
    if method in _TERMINAL_METHODS:
        return terminal_status_for_method(method, params) is not None
    return False


def should_close_turn_buffer(method: str, params: Mapping[str, Any]) -> bool:
    if runtime_terminal_status_for_lifecycle(method, params) is None:
        return False
    return method not in _IDLE_TERMINAL_METHODS


@dataclass(frozen=True)
class ACPLifecycleSnapshot:
    method: str
    payload: dict[str, Any]
    session_id: Optional[str]
    turn_id: Optional[str]
    normalized_kind: str
    session_update_kind: Optional[str] = None
    session_status: Optional[str] = None
    terminal_status: Optional[str] = None
    runtime_terminal_status: Optional[ACPRuntimeTerminalStatus] = None
    assistant_text: str = ""
    output_delta: str = ""
    progress_message: str = ""
    error_message: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)
    permission_request_id: str = ""
    permission_description: str = ""
    uses_turn_id_fallback: bool = False
    closes_turn_buffer: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.terminal_status is not None


def analyze_acp_lifecycle_message(message: Mapping[str, Any]) -> ACPLifecycleSnapshot:
    payload_source: Mapping[str, Any] = message
    nested_message = message.get("message")
    if isinstance(nested_message, Mapping) and not _normalize_optional_text(
        message.get("method")
    ):
        payload_source = nested_message

    method = _normalize_optional_text(payload_source.get("method")) or ""
    payload = coerce_mapping(payload_source.get("params"))
    session_id = extract_identifier(payload, "sessionId", "session_id", "sessionID")
    turn_id = extract_identifier(
        payload,
        "turnId",
        "turn_id",
        "promptId",
        "prompt_id",
    )
    update = extract_session_update(payload) if method == "session/update" else {}
    update_kind = extract_session_update_kind(update)
    status_type = (
        session_status_type(payload)
        if method in {"session.status", "session/status"}
        else None
    )
    terminal_status = terminal_status_for_method(method, payload)
    runtime_terminal_status = runtime_terminal_status_for_lifecycle(method, payload)
    usage = (
        extract_usage(update)
        if update_kind == "usage_update"
        else extract_usage(payload)
    )
    normalized_kind = "unknown"
    assistant_text = ""
    output_delta = ""
    progress_message = ""
    error_message: Optional[str] = None
    permission_request_id = ""
    permission_description = ""

    if method in {"session/created", "session/loaded"}:
        normalized_kind = "session"
    elif terminal_status is not None:
        normalized_kind = "turn_terminal"
        if runtime_terminal_status != "error":
            assistant_text = extract_message_text(payload)
        if runtime_terminal_status == "error":
            error_message = extract_error_message(payload)
    elif method in {"session.status", "session/status"}:
        normalized_kind = "progress"
        progress_message = status_type or ""
    elif method == "session/update":
        normalized_kind = "unknown"
        if update_kind == "agent_message_chunk":
            normalized_kind = "output_delta"
            output_delta = extract_text_content(update.get("content")) or ""
            if not output_delta:
                output_delta = extract_output_delta(update)
            if not output_delta:
                output_delta = extract_text(update, "message") or ""
        elif update_kind == "agent_thought_chunk":
            normalized_kind = "progress"
            progress_message = extract_text_content(update.get("content")) or ""
            if not progress_message:
                progress_message = extract_progress_message(update)
        elif update_kind == "usage_update":
            normalized_kind = "token_usage"
        elif update_kind == "session_info_update":
            normalized_kind = "session"
    elif method in {"prompt/started", "turn/started"}:
        normalized_kind = "turn_started"
    elif method in {
        "prompt/output",
        "prompt/delta",
        "prompt/progress",
        "turn/progress",
    }:
        output_delta = extract_text(payload, "delta", "textDelta", "text_delta") or ""
        if output_delta:
            normalized_kind = "output_delta"
        elif usage:
            normalized_kind = "token_usage"
        else:
            normalized_kind = "progress"
            progress_message = extract_progress_message(payload)
    elif method in {"prompt/message", "turn/message"}:
        normalized_kind = "message"
        assistant_text = extract_message_text(payload)
    elif method in {"permission/requested", "session/request_permission"}:
        normalized_kind = "permission_requested"
        permission_request_id = (
            extract_identifier(payload, "requestId", "request_id")
            or _normalize_optional_text(payload_source.get("id"))
            or ""
        )
        permission_description = extract_permission_description(payload)
    elif method == "token/usage":
        normalized_kind = "token_usage"

    if method in {"prompt/failed", "turn/failed"}:
        error_message = extract_error_message(payload)

    return ACPLifecycleSnapshot(
        method=method,
        payload=payload,
        session_id=session_id,
        turn_id=turn_id,
        normalized_kind=normalized_kind,
        session_update_kind=update_kind,
        session_status=status_type,
        terminal_status=terminal_status,
        runtime_terminal_status=runtime_terminal_status,
        assistant_text=assistant_text,
        output_delta=output_delta,
        progress_message=progress_message,
        error_message=error_message,
        usage=usage,
        permission_request_id=permission_request_id,
        permission_description=permission_description,
        uses_turn_id_fallback=should_map_missing_turn_id(method, payload),
        closes_turn_buffer=should_close_turn_buffer(method, payload),
    )


def session_update_content_summary(update: Mapping[str, Any]) -> dict[str, Any]:
    content = update.get("content")
    part_types: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, Mapping):
                continue
            item_type = _normalize_optional_text(item.get("type"))
            if item_type:
                part_types.append(item_type)
    extracted_text = extract_text_content(content)
    if not extracted_text:
        fallback_message = update.get("message")
        if isinstance(fallback_message, str):
            extracted_text = fallback_message
    return {
        "content_kind": type(content).__name__ if content is not None else "missing",
        "content_part_types": tuple(part_types),
        "text": extracted_text,
    }


def classify_prompt_response_status(payload: Mapping[str, Any]) -> str:
    result = coerce_mapping(payload)
    raw_reason = result.get("stopReason") or result.get("stop_reason")
    stop_reason = _normalize_optional_text(raw_reason) or ""
    if stop_reason == "cancelled":
        return "cancelled"
    if stop_reason == "refusal":
        return "failed"
    return "completed"


def prompt_terminal_method_for_status(status: str) -> str:
    if status == "cancelled":
        return "prompt/cancelled"
    if status == "failed":
        return "prompt/failed"
    return "prompt/completed"


def extract_prompt_response_error(payload: Mapping[str, Any]) -> Optional[str]:
    if classify_prompt_response_status(payload) != "failed":
        return None
    result = coerce_mapping(payload)
    return _normalize_optional_text(
        result.get("message")
        or result.get("error")
        or result.get("stopReason")
        or result.get("stop_reason")
    )


__all__ = [
    "ACPLifecycleSnapshot",
    "ACPRuntimeTerminalStatus",
    "analyze_acp_lifecycle_message",
    "classify_prompt_response_status",
    "coerce_mapping",
    "extract_error_message",
    "extract_identifier",
    "extract_message_text",
    "extract_output_delta",
    "extract_permission_description",
    "extract_progress_message",
    "extract_prompt_response_error",
    "extract_session_update",
    "extract_session_update_kind",
    "extract_text",
    "extract_text_content",
    "extract_usage",
    "is_idle_terminal",
    "prompt_terminal_method_for_status",
    "runtime_terminal_status_for_lifecycle",
    "session_status_type",
    "session_update_content_summary",
    "should_close_turn_buffer",
    "should_map_missing_turn_id",
    "status_indicates_interrupted",
    "status_indicates_successful_completion",
    "terminal_status_for_method",
]
