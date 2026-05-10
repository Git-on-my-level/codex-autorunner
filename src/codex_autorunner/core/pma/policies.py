from __future__ import annotations

from typing import Any, Literal, cast

from ..text_utils import _normalize_optional_text

BusyPolicy = Literal["queue", "interrupt", "reject"]


def normalize_busy_policy(value: Any, *, default: BusyPolicy = "queue") -> BusyPolicy:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return default
    busy_policy = normalized.lower()
    if busy_policy not in {"queue", "interrupt", "reject"}:
        raise ValueError("busy_policy must be one of: queue, interrupt, reject")
    return cast(BusyPolicy, busy_policy)


def validate_required_message(message: Any) -> str:
    text = str(message or "")
    if not text.strip():
        raise ValueError("message is required")
    return text


def validate_max_text_chars(message: str, max_text_chars: int | None) -> None:
    if max_text_chars is None or max_text_chars <= 0:
        return
    if len(message) > max_text_chars:
        raise ValueError(
            f"message exceeds max_text_chars ({max_text_chars} characters)"
        )


__all__ = [
    "BusyPolicy",
    "normalize_busy_policy",
    "validate_max_text_chars",
    "validate_required_message",
]
