from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .text_utils import _iso_now, _normalize_text

PMA_AUTOMATION_STORE_FILENAME = "automation_store.json"
PMA_AUTOMATION_VERSION = 1
DEFAULT_PMA_LANE_ID = "pma:default"
TIMER_TYPE_ONE_SHOT = "one_shot"
TIMER_TYPE_WATCHDOG = "watchdog"
DEFAULT_WATCHDOG_IDLE_SECONDS = 300


def _normalize_lane_id(value: Any) -> str:
    return _normalize_text(value) or DEFAULT_PMA_LANE_ID


def _normalize_state(value: Any, *, fallback: Optional[str] = None) -> Optional[str]:
    text = _normalize_text(value)
    if text is None:
        return fallback
    return text.lower()


def _normalize_timer_type(value: Any) -> str:
    text = _normalize_state(value, fallback=TIMER_TYPE_ONE_SHOT)
    if text == TIMER_TYPE_WATCHDOG:
        return TIMER_TYPE_WATCHDOG
    return TIMER_TYPE_ONE_SHOT


def _normalize_non_negative_int(
    value: Any, *, fallback: Optional[int] = None
) -> Optional[int]:
    if value is None:
        return fallback
    try:
        parsed = int(value)
    except (ValueError, TypeError):
        return fallback
    if parsed < 0:
        return fallback
    return parsed


def _normalize_positive_int(
    value: Any, *, fallback: Optional[int] = None
) -> Optional[int]:
    parsed = _normalize_non_negative_int(value, fallback=fallback)
    if parsed is None:
        return None
    if parsed <= 0:
        return fallback
    return parsed


def _normalize_bool(value: Any, *, fallback: Optional[bool] = None) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return fallback


def _normalize_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _normalize_text(item)
        if text is None:
            continue
        norm = text.lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _parse_iso(value: Any) -> Optional[datetime]:
    text = _normalize_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_due_timestamp(
    value: Any, *, field_name: str = "due_at"
) -> Optional[str]:
    text = _normalize_text(value)
    if text is None:
        return None
    parsed = _parse_iso(text)
    if parsed is None:
        raise ValueError(f"{field_name} must be a valid ISO-8601 timestamp")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_after_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def default_pma_automation_state() -> dict[str, Any]:
    return {
        "version": PMA_AUTOMATION_VERSION,
        "updated_at": _iso_now(),
        "subscriptions": [],
        "timers": [],
        "wakeups": [],
    }


__all__ = [
    "DEFAULT_PMA_LANE_ID",
    "DEFAULT_WATCHDOG_IDLE_SECONDS",
    "PMA_AUTOMATION_STORE_FILENAME",
    "PMA_AUTOMATION_VERSION",
    "TIMER_TYPE_ONE_SHOT",
    "TIMER_TYPE_WATCHDOG",
    "_iso_after_seconds",
    "_iso_now",
    "_normalize_bool",
    "_normalize_due_timestamp",
    "_normalize_lane_id",
    "_normalize_non_negative_int",
    "_normalize_positive_int",
    "_normalize_state",
    "_normalize_text",
    "_normalize_text_list",
    "_normalize_timer_type",
    "_parse_iso",
    "default_pma_automation_state",
]
