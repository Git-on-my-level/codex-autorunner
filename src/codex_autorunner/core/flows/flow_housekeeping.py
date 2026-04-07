"""Reusable housekeeping service for flow telemetry export, pruning, and compaction.

Both the manual CLI (`car flow housekeep`) and future automatic hooks
(TICKET-005) call into this module so retention logic stays in one place.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..config import FlowRetentionConfig, parse_flow_retention_config
from .models import FlowEventType, FlowRunRecord, parse_flow_timestamp
from .store import FlowStore
from .telemetry_export import export_all_runs, plan_export

_logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7


def _retention_cutoff(retention_config: FlowRetentionConfig) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=retention_config.retention_days)


@dataclasses.dataclass
class RunHousekeepStats:
    run_id: str
    run_status: str
    flow_type: str
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    is_active: bool = False
    is_terminal: bool = False
    is_expired: bool = False
    events_total: int = 0
    telemetry_total: int = 0
    wire_events: int = 0
    estimated_export_bytes: int = 0


@dataclasses.dataclass
class HousekeepStats:
    db_path: str
    db_size_bytes: int = 0
    runs_total: int = 0
    runs_active: int = 0
    runs_terminal: int = 0
    runs_expired: int = 0
    events_total: int = 0
    telemetry_total: int = 0
    wire_events_total: int = 0
    estimated_export_bytes: int = 0
    run_details: List[RunHousekeepStats] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class HousekeepPlan:
    stats: HousekeepStats
    runs_to_process: List[RunHousekeepStats] = dataclasses.field(default_factory=list)
    runs_skipped_active: int = 0
    runs_skipped_not_expired: int = 0
    events_to_export: int = 0
    events_to_prune: int = 0
    estimated_export_bytes: int = 0


@dataclasses.dataclass
class HousekeepResult:
    plan: HousekeepPlan
    runs_processed: int = 0
    events_exported: int = 0
    events_pruned: int = 0
    exported_bytes: int = 0
    archive_files: List[str] = dataclasses.field(default_factory=list)
    errors: List[str] = dataclasses.field(default_factory=list)
    vacuum_performed: bool = False
    db_size_before_bytes: int = 0
    db_size_after_bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runs_processed": self.runs_processed,
            "events_exported": self.events_exported,
            "events_pruned": self.events_pruned,
            "exported_bytes": self.exported_bytes,
            "archive_files": list(self.archive_files),
            "errors": list(self.errors),
            "vacuum_performed": self.vacuum_performed,
            "db_size_before_bytes": self.db_size_before_bytes,
            "db_size_after_bytes": self.db_size_after_bytes,
            "run_details": [
                {
                    "run_id": r.run_id,
                    "run_status": r.run_status,
                    "events_total": r.events_total,
                    "telemetry_total": r.telemetry_total,
                    "wire_events": r.wire_events,
                }
                for r in self.plan.runs_to_process
            ],
        }


def _db_size(db_path: Path) -> int:
    try:
        return db_path.stat().st_size
    except OSError:
        return 0


def _count_events_for_run(store: FlowStore, run_id: str) -> tuple[int, int, int]:
    conn = store._get_conn()
    ev_row = conn.execute(
        "SELECT COUNT(*) as cnt FROM flow_events WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    events_total = ev_row["cnt"] if ev_row else 0

    tel_row = conn.execute(
        "SELECT COUNT(*) as cnt FROM flow_telemetry WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    telemetry_total = tel_row["cnt"] if tel_row else 0

    wire_row = conn.execute(
        """
        SELECT COUNT(*) as cnt FROM flow_events
        WHERE run_id = ? AND event_type IN (?, ?)
        """,
        (
            run_id,
            FlowEventType.APP_SERVER_EVENT.value,
            FlowEventType.AGENT_STREAM_DELTA.value,
        ),
    ).fetchone()
    wire_in_events = wire_row["cnt"] if wire_row else 0

    wire_tel_row = conn.execute(
        "SELECT COUNT(*) as cnt FROM flow_telemetry WHERE run_id = ? AND event_type = ?",
        (run_id, FlowEventType.APP_SERVER_EVENT.value),
    ).fetchone()
    wire_in_telemetry = wire_tel_row["cnt"] if wire_tel_row else 0

    return events_total, telemetry_total, wire_in_events + wire_in_telemetry


def _is_run_expired(
    record: FlowRunRecord,
    cutoff: datetime,
) -> bool:
    if not record.finished_at:
        return False
    finished_dt = parse_flow_timestamp(record.finished_at)
    if finished_dt is None:
        return False
    return finished_dt < cutoff


def gather_stats(
    store: FlowStore,
    db_path: Path,
    retention_config: FlowRetentionConfig,
) -> HousekeepStats:
    stats = HousekeepStats(
        db_path=str(db_path),
        db_size_bytes=_db_size(db_path),
    )
    records = store.list_flow_runs()
    stats.runs_total = len(records)

    for record in records:
        is_active = record.status.is_active() or record.status.is_paused()
        is_terminal = record.status.is_terminal()
        is_expired = (
            _is_run_expired(record, _retention_cutoff(retention_config))
            if is_terminal
            else False
        )

        events_total, telemetry_total, wire_events = _count_events_for_run(
            store, record.id
        )

        run_stats = RunHousekeepStats(
            run_id=record.id,
            run_status=record.status.value,
            flow_type=record.flow_type,
            created_at=record.created_at,
            finished_at=record.finished_at,
            is_active=is_active,
            is_terminal=is_terminal,
            is_expired=is_expired,
            events_total=events_total,
            telemetry_total=telemetry_total,
            wire_events=wire_events,
        )
        stats.run_details.append(run_stats)

        if is_active:
            stats.runs_active += 1
        if is_terminal:
            stats.runs_terminal += 1
        if is_expired:
            stats.runs_expired += 1
        stats.events_total += events_total
        stats.telemetry_total += telemetry_total
        stats.wire_events_total += wire_events

    return stats


def build_plan(
    store: FlowStore,
    db_path: Path,
    retention_config: FlowRetentionConfig,
    *,
    run_ids: Optional[Sequence[str]] = None,
) -> HousekeepPlan:
    stats = gather_stats(store, db_path, retention_config)
    plan = HousekeepPlan(stats=stats)

    for run_stat in stats.run_details:
        if run_ids and run_stat.run_id not in run_ids:
            continue
        if run_stat.is_active:
            plan.runs_skipped_active += 1
            continue
        if not run_stat.is_terminal:
            plan.runs_skipped_active += 1
            continue
        if not run_stat.is_expired:
            plan.runs_skipped_not_expired += 1
            continue
        plan.runs_to_process.append(run_stat)

    export_plan = plan_export(store)
    plan.events_to_export = export_plan.events_to_export
    plan.events_to_prune = export_plan.events_to_prune
    plan.estimated_export_bytes = export_plan.estimated_archive_bytes

    return plan


def execute_housekeep(
    repo_root: Path,
    store: FlowStore,
    db_path: Path,
    retention_config: FlowRetentionConfig,
    *,
    run_ids: Optional[Sequence[str]] = None,
    vacuum: bool = False,
    dry_run: bool = False,
) -> HousekeepResult:
    plan = build_plan(store, db_path, retention_config, run_ids=run_ids)

    result = HousekeepResult(
        plan=plan,
        db_size_before_bytes=_db_size(db_path),
    )

    if dry_run:
        result.db_size_after_bytes = result.db_size_before_bytes
        return result

    target_run_ids = [r.run_id for r in plan.runs_to_process]

    if not target_run_ids:
        _logger.info("No expired terminal runs to housekeep")
    else:
        export_result = export_all_runs(
            repo_root,
            store,
            dry_run=False,
            run_ids=target_run_ids,
        )
        result.runs_processed = sum(1 for r in export_result.records if not r.skipped)
        result.events_exported = export_result.total_exported_events
        result.events_pruned = export_result.total_pruned_events
        result.exported_bytes = export_result.total_exported_bytes
        result.archive_files = list(export_result.archive_files)
        result.errors = list(export_result.errors)

    if vacuum:
        _logger.info("Running VACUUM on %s", db_path)
        store.close()
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
        result.vacuum_performed = True

    result.db_size_after_bytes = _db_size(db_path)
    return result


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "FlowRetentionConfig",
    "HousekeepPlan",
    "HousekeepResult",
    "HousekeepStats",
    "RunHousekeepStats",
    "build_plan",
    "execute_housekeep",
    "gather_stats",
    "parse_flow_retention_config",
]
