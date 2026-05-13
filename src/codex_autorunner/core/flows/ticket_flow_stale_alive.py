from __future__ import annotations

from datetime import timezone
from pathlib import Path
from typing import Any, Optional

from ..config import load_repo_config
from ..config_contract import ConfigError
from .models import FlowRunRecord, FlowRunStatus, parse_flow_timestamp

_DEFAULT_STALE_ALIVE_THRESHOLD_SECONDS = 30 * 60


def ticket_flow_stale_alive_threshold_seconds(repo_root: Path) -> int:
    try:
        ticket_flow = load_repo_config(repo_root).ticket_flow
    except ConfigError:
        return _DEFAULT_STALE_ALIVE_THRESHOLD_SECONDS
    try:
        raw_value: Any = getattr(ticket_flow, "stale_alive_threshold_seconds", None)
        value = int(raw_value)
    except (TypeError, ValueError):
        return _DEFAULT_STALE_ALIVE_THRESHOLD_SECONDS
    return value if value > 0 else _DEFAULT_STALE_ALIVE_THRESHOLD_SECONDS


def ticket_flow_latest_semantic_progress_at(
    record: FlowRunRecord,
    *,
    last_event_at: Optional[str],
) -> Optional[str]:
    candidates: list[str] = []
    for candidate in (
        last_event_at,
        record.started_at,
        record.created_at,
        record.finished_at,
    ):
        if isinstance(candidate, str) and candidate.strip():
            candidates.append(candidate.strip())
    parsed = [parse_flow_timestamp(candidate) for candidate in candidates]
    normalized = [dt.astimezone(timezone.utc) for dt in parsed if dt is not None]
    if not normalized:
        return None
    return max(normalized).isoformat()


def ticket_flow_annotate_stale_alive_health(
    repo_root: Path,
    record: FlowRunRecord,
    health: Any,
    *,
    last_event_at: Optional[str],
    now: str,
) -> Any:
    if (
        record.flow_type != "ticket_flow"
        or record.status != FlowRunStatus.RUNNING
        or getattr(health, "status", None) != "alive"
        or getattr(health, "active_tool", None) is not None
    ):
        return health
    last_progress_at = ticket_flow_latest_semantic_progress_at(
        record,
        last_event_at=last_event_at,
    )
    last_progress_dt = parse_flow_timestamp(last_progress_at)
    now_dt = parse_flow_timestamp(now)
    if last_progress_dt is None or now_dt is None:
        return health
    age_seconds = int(max(0.0, (now_dt - last_progress_dt).total_seconds()))
    threshold_seconds = ticket_flow_stale_alive_threshold_seconds(repo_root)
    if age_seconds <= threshold_seconds:
        return health

    health.status = "stale_alive"
    health.message = "worker alive but no active tool and semantic progress is stale"
    health.last_semantic_progress_at = last_progress_at
    health.last_tool_activity_at = None
    health.current_phase = record.current_step
    health.stale_reason = "semantic_progress_stale_without_active_tool"
    health.stale_threshold_seconds = threshold_seconds
    health.semantic_stale_age_seconds = age_seconds
    return health
