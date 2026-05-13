from __future__ import annotations

from datetime import timezone
from pathlib import Path
from typing import Any, Optional

from .models import FlowRunRecord, FlowRunStatus, parse_flow_timestamp

DEFAULT_STALE_ALIVE_THRESHOLD_SECONDS = 30 * 60
STALE_ALIVE_REASON = "semantic_progress_stale_without_active_tool"
STALE_ALIVE_MESSAGE = "worker alive but no active tool and semantic progress is stale"


def latest_semantic_progress_at(
    record: FlowRunRecord,
    *,
    last_event_at: Optional[str],
) -> Optional[str]:
    parsed = [
        parse_flow_timestamp(candidate)
        for candidate in (
            last_event_at,
            record.started_at,
            record.created_at,
            record.finished_at,
        )
        if isinstance(candidate, str) and candidate.strip()
    ]
    normalized = [dt.astimezone(timezone.utc) for dt in parsed if dt is not None]
    if not normalized:
        return None
    return max(normalized).isoformat()


def annotate_stale_alive_health(
    repo_root: Path,
    record: FlowRunRecord,
    health: Any,
    *,
    last_event_at: Optional[str],
    last_semantic_progress_at: Optional[str] = None,
    threshold_seconds: int,
    now: str,
) -> Any:
    if (
        record.flow_type != "ticket_flow"
        or record.status != FlowRunStatus.RUNNING
        or getattr(health, "status", None) != "alive"
        or getattr(health, "active_tool", None) is not None
    ):
        return health
    last_progress_at = last_semantic_progress_at or latest_semantic_progress_at(
        record, last_event_at=last_event_at
    )
    last_progress_dt = parse_flow_timestamp(last_progress_at)
    now_dt = parse_flow_timestamp(now)
    if last_progress_dt is None or now_dt is None:
        return health
    age_seconds = int(max(0.0, (now_dt - last_progress_dt).total_seconds()))
    if age_seconds <= threshold_seconds:
        return health

    health.status = "stale_alive"
    health.message = STALE_ALIVE_MESSAGE
    health.last_semantic_progress_at = last_progress_at
    health.last_tool_activity_at = None
    health.current_phase = record.current_step
    health.stale_reason = STALE_ALIVE_REASON
    health.stale_threshold_seconds = threshold_seconds
    health.semantic_stale_age_seconds = age_seconds
    return health


def stale_alive_recovery_payload(health: Any) -> dict[str, Any]:
    return {
        "reason": getattr(health, "stale_reason", None),
        "last_semantic_progress_at": getattr(health, "last_semantic_progress_at", None),
        "last_tool_activity_at": getattr(health, "last_tool_activity_at", None),
        "current_phase": getattr(health, "current_phase", None),
        "stale_threshold_seconds": getattr(health, "stale_threshold_seconds", None),
        "semantic_stale_age_seconds": getattr(
            health, "semantic_stale_age_seconds", None
        ),
        "worker_pid": getattr(health, "pid", None),
    }
