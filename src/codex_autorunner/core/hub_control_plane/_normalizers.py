from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..orchestration.models import (
    BusyThreadPolicy,
    MessageRequestKind,
)


def normalize_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def normalize_required_text(value: Any, *, field_name: str) -> str:
    normalized = normalize_optional_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def normalize_optional_identifier(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        normalized = str(value).strip()
        return normalized or None
    return None


def normalize_required_identifier(value: Any, *, field_name: str) -> str:
    normalized = normalize_optional_identifier(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def copy_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return dict(value)


def normalize_string_set(value: Iterable[Any] | None) -> tuple[str, ...]:
    if value is None or isinstance(value, (str, bytes)):
        return ()
    normalized = {
        item.strip() for item in value if isinstance(item, str) and item.strip()
    }
    return tuple(sorted(normalized))


def coerce_int(value: Any, *, field_name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if normalized < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return normalized


def normalize_message_request_kind(value: Any) -> MessageRequestKind:
    normalized = normalize_optional_text(value)
    if normalized == "review":
        return "review"
    return "message"


def normalize_busy_thread_policy(value: Any) -> BusyThreadPolicy:
    normalized = normalize_optional_text(value)
    if normalized == "interrupt":
        return "interrupt"
    if normalized == "queue":
        return "queue"
    return "reject"


def normalize_run_event_payloads(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    events: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            events.append(dict(item))
    return tuple(events)


def normalize_bool(value: Any, *, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return fallback


__all__ = [
    "coerce_int",
    "copy_mapping",
    "normalize_bool",
    "normalize_busy_thread_policy",
    "normalize_message_request_kind",
    "normalize_optional_identifier",
    "normalize_optional_text",
    "normalize_required_identifier",
    "normalize_required_text",
    "normalize_run_event_payloads",
    "normalize_string_set",
]
