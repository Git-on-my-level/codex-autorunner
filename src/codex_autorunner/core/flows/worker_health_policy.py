from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from .models import parse_flow_timestamp


class WorkerHealthAction(str, Enum):
    NONE = "none"
    STATUS_ONLY = "status_only"
    GRACEFUL_RESTART = "graceful_restart"
    FORCE_RESTART = "force_restart"
    PAUSE = "pause"
    ROTATE_REQUESTED = "rotate_requested"


WorkerProcessStatus = Literal[
    "absent", "alive", "dead", "invalid", "mismatch", "stale_alive"
]
AppServerStatus = Literal["unknown", "connected", "recoverable", "stalled_timeout"]


@dataclass(frozen=True)
class WorkerHealthSnapshot:
    process_status: WorkerProcessStatus
    worker_age_seconds: Optional[float] = None
    last_activity_at: Optional[str] = None
    last_semantic_progress_at: Optional[str] = None
    current_ticket: Optional[str] = None
    current_turn: Optional[str] = None
    app_server_status: AppServerStatus = "unknown"
    idle_duration_seconds: Optional[float] = None


@dataclass(frozen=True)
class WorkerHealthDecision:
    action: WorkerHealthAction
    reason: str
    exit_kind: Optional[str] = None
    worker_health_severity: Literal["healthy", "warning", "critical"] = "healthy"

    @property
    def force_restart(self) -> bool:
        return self.action == WorkerHealthAction.FORCE_RESTART


def build_worker_health_snapshot(
    *,
    process_status: WorkerProcessStatus,
    worker_age_seconds: Optional[float] = None,
    last_activity_at: Optional[str] = None,
    last_semantic_progress_at: Optional[str] = None,
    current_ticket: Optional[str] = None,
    current_turn: Optional[str] = None,
    app_server_status: AppServerStatus = "unknown",
    now: Optional[datetime] = None,
) -> WorkerHealthSnapshot:
    return WorkerHealthSnapshot(
        process_status=process_status,
        worker_age_seconds=worker_age_seconds,
        last_activity_at=last_activity_at,
        last_semantic_progress_at=last_semantic_progress_at,
        current_ticket=current_ticket,
        current_turn=current_turn,
        app_server_status=app_server_status,
        idle_duration_seconds=_idle_duration_seconds(last_activity_at, now=now),
    )


def decide_worker_health(
    snapshot: WorkerHealthSnapshot,
    *,
    max_wall_seconds: Optional[float],
    idle_stale_seconds: Optional[float],
) -> WorkerHealthDecision:
    if snapshot.process_status == "absent":
        return WorkerHealthDecision(
            WorkerHealthAction.FORCE_RESTART,
            "missing_worker",
            exit_kind="missing_worker",
            worker_health_severity="critical",
        )
    if snapshot.process_status in {"dead", "invalid", "mismatch"}:
        return WorkerHealthDecision(
            WorkerHealthAction.FORCE_RESTART,
            snapshot.process_status,
            exit_kind="missing_worker",
            worker_health_severity="critical",
        )
    if snapshot.app_server_status == "stalled_timeout":
        return WorkerHealthDecision(
            WorkerHealthAction.FORCE_RESTART,
            "app_server_stalled",
            exit_kind="app_server_stalled",
            worker_health_severity="critical",
        )
    if (
        idle_stale_seconds is not None
        and snapshot.idle_duration_seconds is not None
        and snapshot.idle_duration_seconds >= idle_stale_seconds
    ):
        return WorkerHealthDecision(
            WorkerHealthAction.FORCE_RESTART,
            "idle_stale",
            exit_kind="idle_stale",
            worker_health_severity="critical",
        )
    if (
        max_wall_seconds is not None
        and snapshot.worker_age_seconds is not None
        and snapshot.worker_age_seconds >= max_wall_seconds
    ):
        return WorkerHealthDecision(
            WorkerHealthAction.ROTATE_REQUESTED,
            "rotation_requested",
            exit_kind="rotation_requested",
            worker_health_severity="warning",
        )
    return WorkerHealthDecision(WorkerHealthAction.NONE, "healthy")


def _idle_duration_seconds(
    last_activity_at: Optional[str], *, now: Optional[datetime]
) -> Optional[float]:
    activity_dt = parse_flow_timestamp(last_activity_at)
    if activity_dt is None:
        return None
    basis = now or datetime.now(timezone.utc)
    if basis.tzinfo is None:
        basis = basis.replace(tzinfo=timezone.utc)
    return max(0.0, (basis.astimezone(timezone.utc) - activity_dt).total_seconds())


__all__ = [
    "AppServerStatus",
    "WorkerHealthAction",
    "WorkerHealthDecision",
    "WorkerHealthSnapshot",
    "build_worker_health_snapshot",
    "decide_worker_health",
]
