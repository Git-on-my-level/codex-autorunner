"""Delivery recovery cursor planning and backoff state helpers.

Owns the pure logic for deciding whether a stuck interaction delivery is still
in backoff, and for planning the next recovery cursor (snapshot hashing,
exponential backoff scheduling, unchanged-cursor abandonment).  Extracted from
``service.py`` as a stable seam so the main service module can stay focused on
orchestration and transport.

Public API:
    - :func:`plan_delivery_recovery_cursor`
    - :func:`is_recovery_backoff_active`
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_INTERACTION_RECOVERY_INITIAL_BACKOFF_SECONDS = 5.0
_INTERACTION_RECOVERY_MAX_BACKOFF_SECONDS = 300.0
_INTERACTION_RECOVERY_MAX_UNCHANGED_CURSOR_ATTEMPTS = 3
_INTERACTION_RECOVERY_METADATA_KEY = "_recovery"


def _parse_interaction_recovery_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _interaction_recovery_delay_seconds(attempts: int) -> float:
    if attempts <= 0:
        return 0.0
    delay = _INTERACTION_RECOVERY_INITIAL_BACKOFF_SECONDS * (2 ** max(0, attempts - 1))
    return float(min(_INTERACTION_RECOVERY_MAX_BACKOFF_SECONDS, delay))


def _interaction_recovery_snapshot_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _read_interaction_recovery_metadata(cursor: dict[str, Any]) -> dict[str, Any]:
    raw = cursor.get(_INTERACTION_RECOVERY_METADATA_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _write_interaction_recovery_metadata(
    cursor: dict[str, Any],
    *,
    snapshot_hash: str,
    unchanged_attempts: int,
    scheduled_attempt: int,
    scheduled_at: datetime,
) -> dict[str, Any]:
    updated_cursor = dict(cursor)
    delay_seconds = _interaction_recovery_delay_seconds(scheduled_attempt)
    updated_cursor[_INTERACTION_RECOVERY_METADATA_KEY] = {
        "snapshot_hash": snapshot_hash,
        "unchanged_attempts": max(0, int(unchanged_attempts)),
        "scheduled_attempt": max(0, int(scheduled_attempt)),
        "scheduled_at": scheduled_at.astimezone(timezone.utc).isoformat(),
        "next_retry_at": (scheduled_at + timedelta(seconds=delay_seconds))
        .astimezone(timezone.utc)
        .isoformat(),
        "backoff_seconds": delay_seconds,
    }
    return updated_cursor


def is_recovery_backoff_active(
    *,
    updated_at: Optional[str],
    attempt_count: int,
    now: datetime,
) -> bool:
    if attempt_count <= 0:
        return False
    updated_dt = _parse_interaction_recovery_datetime(updated_at)
    if updated_dt is None:
        return False
    retry_at = updated_dt + timedelta(
        seconds=_interaction_recovery_delay_seconds(attempt_count)
    )
    return now < retry_at


def plan_delivery_recovery_cursor(
    *,
    cursor: dict[str, Any],
    attempt_count: int,
    now: datetime,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    metadata = _read_interaction_recovery_metadata(cursor)
    next_retry_at = _parse_interaction_recovery_datetime(metadata.get("next_retry_at"))
    if next_retry_at is not None and now < next_retry_at:
        return None, None
    snapshot_source = {
        key: value
        for key, value in cursor.items()
        if key != _INTERACTION_RECOVERY_METADATA_KEY
    }
    snapshot_hash = _interaction_recovery_snapshot_hash(snapshot_source)
    prior_hash = str(metadata.get("snapshot_hash") or "").strip()
    unchanged_attempts = (
        max(0, int(metadata.get("unchanged_attempts") or 0)) + 1
        if prior_hash == snapshot_hash
        else 1
    )
    if unchanged_attempts > _INTERACTION_RECOVERY_MAX_UNCHANGED_CURSOR_ATTEMPTS:
        return None, "unchanged_delivery_cursor"
    scheduled_attempt = max(1, int(attempt_count) + 1)
    return (
        _write_interaction_recovery_metadata(
            snapshot_source,
            snapshot_hash=snapshot_hash,
            unchanged_attempts=unchanged_attempts,
            scheduled_attempt=scheduled_attempt,
            scheduled_at=now,
        ),
        None,
    )
