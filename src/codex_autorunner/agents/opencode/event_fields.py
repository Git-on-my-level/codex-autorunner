from __future__ import annotations

from typing import Any, Optional

_MESSAGE_ID_KEYS = ("messageID", "messageId", "message_id")
_PART_ID_KEYS = ("id", "partID", "partId", "part_id")


def _coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _extract_properties(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return _coerce_dict(payload.get("properties"))


def _extract_part(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    properties = _extract_properties(payload)
    part = properties.get("part")
    if isinstance(part, dict):
        return part
    part = payload.get("part")
    if isinstance(part, dict):
        return part
    return {}


def _extract_info(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    info = payload.get("info")
    if isinstance(info, dict):
        return info
    properties = _extract_properties(payload)
    info = properties.get("info")
    if isinstance(info, dict):
        return info
    return {}


def extract_message_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    info = _extract_info(payload)
    for key in ("id", *_MESSAGE_ID_KEYS):
        info_id = info.get(key)
        if isinstance(info_id, str) and info_id:
            return info_id
    part = _extract_part(payload)
    properties = _extract_properties(payload)
    for source in (part, properties, payload):
        for key in _MESSAGE_ID_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_message_role(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    info = _extract_info(payload)
    role = info.get("role")
    if isinstance(role, str) and role:
        return role
    role = payload.get("role")
    if isinstance(role, str) and role:
        return role
    return None


def extract_part_message_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    part = _extract_part(payload)
    properties = _extract_properties(payload)
    for source in (part, properties, payload):
        for key in _MESSAGE_ID_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_part_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    part = _extract_part(payload)
    properties = _extract_properties(payload)
    for source in (part, properties, payload):
        for key in _PART_ID_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_part_type(
    payload: Any, *, part_types: Optional[dict[str, str]] = None
) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    part = _extract_part(payload)
    part_type_raw = part.get("type")
    part_type: Optional[str] = None
    if isinstance(part_type_raw, str):
        normalized = part_type_raw.strip().lower()
        part_type = normalized or None
    part_id = extract_part_id(payload)
    if part_types is not None and isinstance(part_id, str) and part_id:
        if isinstance(part_type, str):
            part_types[part_id] = part_type
        else:
            part_type = part_types.get(part_id)
    return part_type
