from __future__ import annotations

from typing import Any, Optional

_MESSAGE_ID_KEYS = ("messageID", "messageId", "message_id")
_PART_ID_KEYS = ("id", "partID", "partId", "part_id")


def coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _extract_text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text:
            return text
        message = value.get("message")
        if isinstance(message, str) and message:
            return message
        return _extract_text_content(value.get("content"))
    if isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            if isinstance(entry, str) and entry:
                parts.append(entry)
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
            text = _extract_text_content(entry)
            if text:
                parts.append(text)
        return "".join(parts)
    return ""


def extract_message_properties(params: dict[str, Any]) -> dict[str, Any]:
    return coerce_dict(params.get("properties"))


def extract_message_part(params: dict[str, Any]) -> dict[str, Any]:
    properties = extract_message_properties(params)
    part = properties.get("part")
    if isinstance(part, dict):
        return part
    part = params.get("part")
    if isinstance(part, dict):
        return part
    return {}


def extract_message_info(params: dict[str, Any]) -> dict[str, Any]:
    properties = extract_message_properties(params)
    info = properties.get("info")
    if isinstance(info, dict):
        return info
    info = params.get("info")
    if isinstance(info, dict):
        return info
    return {}


def extract_message_id(params: dict[str, Any]) -> Optional[str]:
    properties = extract_message_properties(params)
    info = extract_message_info(params)
    part = extract_message_part(params)
    for source, keys in (
        (info, ("id", *_MESSAGE_ID_KEYS)),
        (part, _MESSAGE_ID_KEYS),
        (properties, _MESSAGE_ID_KEYS),
        (params, _MESSAGE_ID_KEYS),
    ):
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_message_role(params: dict[str, Any]) -> Optional[str]:
    info = extract_message_info(params)
    role = info.get("role")
    if isinstance(role, str) and role:
        return role
    role = params.get("role")
    if isinstance(role, str) and role:
        return role
    return None


def extract_part_message_id(params: dict[str, Any]) -> Optional[str]:
    properties = extract_message_properties(params)
    part = extract_message_part(params)
    for source in (part, properties, params):
        for key in _MESSAGE_ID_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_part_id(
    params: dict[str, Any], *, part: Optional[dict[str, Any]] = None
) -> Optional[str]:
    properties = extract_message_properties(params)
    if part is None:
        part = extract_message_part(params)
    for source in (part, properties, params):
        for key in _PART_ID_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_part_type(
    params: dict[str, Any], *, part_types: Optional[dict[str, str]] = None
) -> str:
    part = extract_message_part(params)
    part_type = str(part.get("type") or "").strip().lower()
    part_id = extract_part_id(params, part=part)
    if part_types is not None and part_id:
        if part_type:
            part_types[part_id] = part_type
        else:
            part_type = part_types.get(part_id, "")
    return part_type


def extract_output_delta(
    params: dict[str, Any], *, include_part_text: bool = True
) -> str:
    for key in ("content", "delta", "text", "output"):
        value = params.get(key)
        extracted = _extract_text_content(value)
        if extracted:
            return extracted
    properties = extract_message_properties(params)
    delta_raw = properties.get("delta")
    if isinstance(delta_raw, str) and delta_raw:
        return delta_raw
    delta = coerce_dict(delta_raw)
    delta_text = delta.get("text")
    if isinstance(delta_text, str) and delta_text:
        return delta_text
    if include_part_text:
        part = coerce_dict(properties.get("part"))
        if part.get("type") == "text":
            part_text = part.get("text")
            if isinstance(part_text, str) and part_text:
                return part_text
    return ""
