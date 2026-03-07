from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Request


def coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_tail_duration_seconds(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    raw = value.strip().lower()
    if not raw:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="since must not be empty")
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total_seconds = 0
    idx = 0
    size = len(raw)
    while idx < size:
        start = idx
        while idx < size and raw[idx].isdigit():
            idx += 1
        if start == idx or idx >= size:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid since duration. Use forms like 30s, 5m, 2h, 1d, "
                    "or combined 1h30m."
                ),
            )
        amount_text = raw[start:idx]
        if len(amount_text) > 9:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=400, detail="since duration component is too large"
            )
        unit = raw[idx]
        multiplier = multipliers.get(unit)
        if multiplier is None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid since duration. Use forms like 30s, 5m, 2h, 1d, "
                    "or combined 1h30m."
                ),
            )
        idx += 1
        total_seconds += int(amount_text) * multiplier
    if total_seconds <= 0:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="since must be > 0")
    return total_seconds


def since_ms_from_duration(value: Optional[str]) -> Optional[int]:
    seconds = parse_tail_duration_seconds(value)
    if seconds is None:
        return None
    return int((datetime.now(timezone.utc).timestamp() - seconds) * 1000)


def normalize_tail_level(level: Optional[str]) -> str:
    normalized = (level or "info").strip().lower() or "info"
    if normalized not in {"info", "debug"}:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="level must be info or debug")
    return normalized


def resolve_resume_after(
    request: Request, since_event_id: Optional[int]
) -> Optional[int]:
    if since_event_id is not None:
        if since_event_id < 0:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail="since_event_id must be >= 0")
        return since_event_id
    last_event_id = request.headers.get("Last-Event-ID")
    if not last_event_id:
        return None
    try:
        parsed = int(last_event_id)
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400, detail="Invalid Last-Event-ID header"
        ) from exc
    if parsed < 0:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="Last-Event-ID must be >= 0")
    return parsed


def iso_from_event_ms(value: Any) -> Optional[str]:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()


def truncate_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."
