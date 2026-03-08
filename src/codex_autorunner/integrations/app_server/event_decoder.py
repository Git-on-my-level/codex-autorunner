from __future__ import annotations

from typing import Any, Optional

from .ids import extract_thread_id_for_turn, extract_turn_id
from .protocol_types import (
    ApprovalRequest,
    ErrorNotification,
    ItemCompletedNotification,
    NotificationResult,
    OutputDeltaNotification,
    ReasoningSummaryDeltaNotification,
    TokenUsageNotification,
    ToolCallNotification,
    TurnCompletedNotification,
)

APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}


def decode_notification(message: dict[str, Any]) -> NotificationResult:
    """Decode raw JSON-RPC notification dict into typed protocol object."""
    method = message.get("method", "")
    params = message.get("params", {}) or {}

    if not isinstance(method, str) or not isinstance(params, dict):
        return None

    if method in APPROVAL_METHODS:
        return _decode_approval_request(method, params)

    if method == "item/reasoning/summaryTextDelta":
        return _decode_reasoning_summary_delta(method, params)

    if method == "item/agentMessage/delta":
        return _decode_output_delta(method, params)

    if method == "turn/streamDelta" or "outputdelta" in method.lower():
        return _decode_output_delta(method, params)

    if method == "item/toolCall/start":
        return _decode_tool_call(method, params)

    if method == "item/toolCall/end":
        return None

    if method == "item/completed":
        return _decode_item_completed(method, params)

    if method in {"turn/tokenUsage", "turn/usage", "thread/tokenUsage/updated"}:
        return _decode_token_usage(method, params)

    if method == "turn/completed":
        return _decode_turn_completed(method, params)

    if method in {"turn/error", "error"}:
        return _decode_error(method, params)

    return None


def _decode_reasoning_summary_delta(
    method: str, params: dict[str, Any]
) -> Optional[ReasoningSummaryDeltaNotification]:
    delta = params.get("delta")
    if not isinstance(delta, str):
        return None
    return ReasoningSummaryDeltaNotification(
        method=method,
        delta=delta,
        item_id=params.get("itemId"),
        turn_id=_extract_turn_id(params),
        thread_id=_extract_thread_id(params),
    )


def _decode_output_delta(
    method: str, params: dict[str, Any]
) -> Optional[OutputDeltaNotification]:
    content = _extract_output_delta_content(params)
    if not content:
        return None
    return OutputDeltaNotification(
        method=method,
        content=content,
        item_id=params.get("itemId"),
        turn_id=_extract_turn_id(params),
        thread_id=_extract_thread_id(params),
    )


def _extract_output_delta_content(params: dict[str, Any]) -> Optional[str]:
    content = params.get("content")
    if isinstance(content, str):
        return content
    delta = params.get("delta")
    if isinstance(delta, str):
        return delta
    message = params.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
    return None


def _decode_tool_call(
    method: str, params: dict[str, Any]
) -> Optional[ToolCallNotification]:
    tool_name, tool_input = _normalize_tool_name(params)
    if not tool_name:
        return None
    return ToolCallNotification(
        method=method,
        tool_name=tool_name,
        tool_input=tool_input,
        item_id=params.get("itemId"),
        turn_id=_extract_turn_id(params),
        thread_id=_extract_thread_id(params),
    )


def _normalize_tool_name(params: dict[str, Any]) -> tuple[Optional[str], Any]:
    item = params.get("item")
    if isinstance(item, dict):
        tool_call = item.get("toolCall") or item.get("tool_call")
        if isinstance(tool_call, dict):
            name = tool_call.get("name")
            if isinstance(name, str):
                input_val = tool_call.get("input")
                return name, input_val
    tool_name = params.get("toolName") or params.get("tool_name")
    if isinstance(tool_name, str):
        return tool_name, params.get("toolInput") or params.get("input")
    return None, None


def _decode_item_completed(
    method: str, params: dict[str, Any]
) -> Optional[ItemCompletedNotification]:
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    return ItemCompletedNotification(
        method=method,
        item=item,
        item_id=params.get("itemId"),
        turn_id=_extract_turn_id(params) or _extract_turn_id(item),
        thread_id=_extract_thread_id(params),
    )


def _decode_token_usage(
    method: str, params: dict[str, Any]
) -> Optional[TokenUsageNotification]:
    usage = params.get("usage") or params.get("tokenUsage")
    if not isinstance(usage, dict):
        return None
    return TokenUsageNotification(
        method=method,
        usage=usage,
        turn_id=_extract_turn_id(params),
        thread_id=_extract_thread_id(params),
    )


def _decode_turn_completed(
    method: str, params: dict[str, Any]
) -> Optional[TurnCompletedNotification]:
    status = params.get("status")
    if status is None and isinstance(params.get("turn"), dict):
        turn_status = params["turn"].get("status")
        if isinstance(turn_status, dict):
            status = turn_status.get("type") or turn_status.get("status")
        elif isinstance(turn_status, str):
            status = turn_status
    return TurnCompletedNotification(
        method=method,
        turn_id=_extract_turn_id(params),
        result=params.get("result"),
        status=status if isinstance(status, str) else None,
        thread_id=_extract_thread_id(params),
    )


def _decode_error(method: str, params: dict[str, Any]) -> ErrorNotification:
    error_payload = params.get("error")
    code = params.get("code")
    if code is None and isinstance(error_payload, dict):
        code = error_payload.get("code")
    return ErrorNotification(
        method=method,
        message=_extract_error_message(params),
        code=code if isinstance(code, int) else None,
        turn_id=_extract_turn_id(params),
        thread_id=_extract_thread_id(params),
        will_retry=(
            params.get("willRetry")
            if isinstance(params.get("willRetry"), bool)
            else None
        ),
    )


def _decode_approval_request(method: str, params: dict[str, Any]) -> ApprovalRequest:
    approval_type = method.replace("item/", "").replace("/requestApproval", "")
    return ApprovalRequest(
        method=method,
        approval_type=approval_type,
        item_id=params.get("itemId"),
        turn_id=_extract_turn_id(params),
        thread_id=_extract_thread_id(params),
        context=params,
    )


def _extract_turn_id(params: dict[str, Any]) -> Optional[str]:
    return extract_turn_id(params) or extract_turn_id(params.get("turn"))


def _extract_thread_id(params: dict[str, Any]) -> Optional[str]:
    return extract_thread_id_for_turn(params)


def _extract_error_message(params: dict[str, Any]) -> str:
    error_payload = params.get("error")
    if isinstance(error_payload, dict):
        for key in ("message", "detail", "error"):
            value = error_payload.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("message", "detail", "reason"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    return "Unknown error"
