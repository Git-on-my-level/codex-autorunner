from __future__ import annotations

import json
from typing import Any, Optional

from ...core.sse import SSEEvent
from .event_fields import (
    extract_message_id as extract_event_message_id,
)
from .event_fields import (
    extract_message_role as extract_event_message_role,
)
from .protocol_types import (
    MessageEvent,
    PermissionEvent,
    QuestionEvent,
    UsageEvent,
)


def decode_sse_event(event: SSEEvent) -> Optional[Any]:
    """Decode SSE event into typed protocol object."""
    event_type = event.event

    if event_type in ("message.delta", "message.completed", "message.updated"):
        return _decode_message_event(event)

    if event_type == "usage":
        return _decode_usage_event(event)

    if event_type == "permission":
        return _decode_permission_event(event)

    if event_type == "question":
        return _decode_question_event(event)

    return None


def _decode_message_event(event: SSEEvent) -> Optional[MessageEvent]:
    try:
        payload = json.loads(event.data)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(payload, dict):
        return None

    message_id = _extract_message_id(payload)
    role = _extract_role(payload)

    content = None
    if event.event == "message.delta":
        content = _extract_delta_content(payload)

    return MessageEvent(
        event_type=event.event,
        message_id=message_id,
        role=role,
        content=content,
    )


def _extract_message_id(payload: dict[str, Any]) -> Optional[str]:
    return extract_event_message_id(payload)


def _extract_role(payload: dict[str, Any]) -> Optional[str]:
    return extract_event_message_role(payload)


def _extract_delta_content(payload: dict[str, Any]) -> Optional[str]:
    delta = payload.get("delta")
    if isinstance(delta, str):
        return delta

    delta_dict = payload.get("delta")
    if isinstance(delta_dict, dict):
        text = delta_dict.get("text")
        if isinstance(text, str):
            return text

    return None


def _decode_usage_event(event: SSEEvent) -> Optional[UsageEvent]:
    try:
        payload = json.loads(event.data)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(payload, dict):
        return None

    usage = _extract_usage_from_payload(payload)
    if not usage:
        return None

    provider_id = None
    model_id = None

    info = payload.get("info")
    if isinstance(info, dict):
        provider_id = info.get("providerID") or info.get("provider_id")
        model_id = info.get("modelID") or info.get("model_id")

    return UsageEvent(
        event_type=event.event,
        usage=usage,
        provider_id=provider_id,
        model_id=model_id,
    )


def _extract_usage_from_payload(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    for key in ("usage", "token_usage", "usage_stats", "usageStats"):
        usage = payload.get(key)
        if isinstance(usage, dict):
            return usage

    info = payload.get("info")
    if isinstance(info, dict):
        for key in ("usage", "token_usage", "usage_stats", "usageStats"):
            usage = info.get(key)
            if isinstance(usage, dict):
                return usage

    return None


def _decode_permission_event(event: SSEEvent) -> Optional[PermissionEvent]:
    try:
        payload = json.loads(event.data)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(payload, dict):
        return None

    permission_id = _extract_permission_id(payload)
    if not permission_id:
        return None

    permission = _extract_permission(payload)
    reason = payload.get("reason")

    return PermissionEvent(
        event_type=event.event,
        permission_id=permission_id,
        permission=permission,
        reason=reason if isinstance(reason, str) else None,
    )


def _extract_permission_id(payload: dict[str, Any]) -> Optional[str]:
    for key in ("id", "permissionID", "permissionId", "permission_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_permission(payload: dict[str, Any]) -> str:
    permission = payload.get("permission")
    if isinstance(permission, str):
        return permission
    return "unknown"


def _decode_question_event(event: SSEEvent) -> Optional[QuestionEvent]:
    try:
        payload = json.loads(event.data)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(payload, dict):
        return None

    question_id = _extract_question_id(payload)
    if not question_id:
        return None

    question = payload.get("question")
    context = _extract_question_context(payload)

    return QuestionEvent(
        event_type=event.event,
        question_id=question_id,
        question=question if isinstance(question, str) else "",
        context=context,
    )


def _extract_question_id(payload: dict[str, Any]) -> Optional[str]:
    for key in ("id", "questionID", "questionId", "question_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_question_context(payload: dict[str, Any]) -> Optional[list[list[str]]]:
    context = payload.get("context")
    if isinstance(context, list):
        return context
    return None


def parse_message_response(payload: Any) -> dict[str, Any]:
    """Parse message response payload for text and error."""
    if not isinstance(payload, dict):
        return {"text": None, "error": None}

    text = None
    error = None

    text = payload.get("text")
    if not isinstance(text, str):
        message = payload.get("message")
        if isinstance(message, str):
            text = message
    if not isinstance(text, str):
        content = payload.get("content")
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    part_text = part.get("text")
                    if isinstance(part_text, str):
                        text_parts.append(part_text)
            if text_parts:
                text = "".join(text_parts)
        elif isinstance(content, str):
            text = content

    error = payload.get("error")

    return {"text": text, "error": error}
