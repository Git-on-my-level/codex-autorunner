from __future__ import annotations

from typing import Any, Optional

from ...core.coercion import coerce_int


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _format_percent(value: Any) -> Optional[str]:
    number = _coerce_number(value)
    if number is None:
        return None
    if number.is_integer():
        return f"{int(number)}%"
    return f"{number:.1f}%"


def _compute_used_percent(entry: dict[str, Any]) -> Optional[float]:
    remaining = _coerce_number(entry.get("remaining"))
    limit = _coerce_number(entry.get("limit"))
    if remaining is None or limit is None or limit <= 0:
        return None
    used = (limit - remaining) / limit * 100
    return max(min(used, 100.0), 0.0)


def _rate_limit_window_minutes(
    entry: dict[str, Any],
    section: Optional[str] = None,
) -> Optional[int]:
    window_minutes = coerce_int(entry.get("window_minutes", entry.get("windowMinutes")))
    if window_minutes is None:
        for candidate in (
            "window",
            "window_mins",
            "windowMins",
            "period_minutes",
            "periodMinutes",
            "duration_minutes",
            "durationMinutes",
        ):
            window_minutes = coerce_int(entry.get(candidate))
            if window_minutes is not None:
                break
    if window_minutes is None:
        window_seconds = coerce_int(
            entry.get("window_seconds", entry.get("windowSeconds"))
        )
        if window_seconds is not None:
            window_minutes = max(int(round(window_seconds / 60)), 1)
    if window_minutes is None and section in ("primary", "secondary"):
        window_minutes = 300 if section == "primary" else 10080
    return window_minutes


def _format_rate_limit_window(window_minutes: Optional[int]) -> Optional[str]:
    if not isinstance(window_minutes, int) or window_minutes <= 0:
        return None
    if window_minutes == 300:
        return "5h"
    if window_minutes % 1440 == 0:
        return f"{window_minutes // 1440}d"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}h"
    return f"{window_minutes}m"
