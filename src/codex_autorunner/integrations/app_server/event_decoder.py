from __future__ import annotations

from typing import Any, Optional

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

    if not isinstance(method, str):
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

    if method == "turn/error":
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
        turn_id=params.get("turnId"),
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
        turn_id=params.get("turnId"),
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
        turn_id=params.get("turnId"),
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
        turn_id=params.get("turnId"),
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
        turn_id=params.get("turnId"),
    )


def _decode_turn_completed(
    method: str, params: dict[str, Any]
) -> Optional[TurnCompletedNotification]:
    return TurnCompletedNotification(
        method=method,
        turn_id=params.get("turnId"),
        result=params.get("result"),
    )


def _decode_error(method: str, params: dict[str, Any]) -> ErrorNotification:
    return ErrorNotification(
        method=method,
        message=params.get("message", "Unknown error"),
        code=params.get("code"),
        turn_id=params.get("turnId"),
    )


def _decode_approval_request(method: str, params: dict[str, Any]) -> ApprovalRequest:
    approval_type = method.replace("item/", "").replace("/requestApproval", "")
    return ApprovalRequest(
        method=method,
        approval_type=approval_type,
        item_id=params.get("itemId"),
        turn_id=params.get("turnId"),
        context=params,
    )
