from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        dt = _parse_iso_timestamp(value)
        if dt is not None:
            return dt
        try:
            return _coerce_datetime(float(value))
        except Exception:
            return None
    return None


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_friendly_time(value: datetime) -> str:
    month = value.strftime("%b")
    day = value.day
    hour = value.strftime("%I").lstrip("0") or "12"
    minute = value.strftime("%M")
    ampm = value.strftime("%p").lower()
    return f"{month} {day}, {hour}:{minute}{ampm}"


def _format_future_time(delay_seconds: float) -> Optional[str]:
    if delay_seconds <= 0:
        return None
    dt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _approval_age_seconds(created_at: Optional[str]) -> Optional[int]:
    dt = _parse_iso_timestamp(created_at)
    if dt is None:
        return None
    return max(int((datetime.now(timezone.utc) - dt).total_seconds()), 0)
