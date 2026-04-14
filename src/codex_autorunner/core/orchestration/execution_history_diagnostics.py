from __future__ import annotations

import dataclasses
import logging
import time
from pathlib import Path
from typing import Any, Optional

from ..text_utils import _json_dumps
from ..time_utils import now_iso
from .cold_trace_store import ColdTraceStore
from .execution_history import timeline_hot_family_for_event_type
from .execution_history_maintenance import (
    ExecutionHistoryMaintenancePolicy,
)
from .sqlite import open_orchestration_sqlite

logger = logging.getLogger("codex_autorunner.execution_history_diagnostics")

_TIMELINE_EVENT_FAMILY = "turn.timeline"


def _collect_timeline_family_counts(conn: Any) -> dict[str, int]:
    family_counts: dict[str, int] = {}
    rows = conn.execute(
        """
        SELECT event_type, COUNT(*) AS cnt
          FROM orch_event_projections
         INDEXED BY idx_orch_event_projections_family_type_execution
         WHERE event_family = ?
           AND execution_id IS NOT NULL
         GROUP BY event_type
        """,
        (_TIMELINE_EVENT_FAMILY,),
    ).fetchall()
    for row in rows:
        family = timeline_hot_family_for_event_type(row["event_type"])
        if family:
            family_counts[family] = family_counts.get(family, 0) + int(row["cnt"] or 0)
    return dict(sorted(family_counts.items()))


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryThresholds:
    oversized_execution_hot_rows: int = 128
    oversized_execution_total_events: int = 500
    cold_trace_bytes_warning: int = 50 * 1024 * 1024
    cold_trace_bytes_error: int = 200 * 1024 * 1024
    startup_recovery_duration_warning_seconds: float = 5.0
    startup_recovery_duration_error_seconds: float = 30.0
    completion_gap_attempts_warning: int = 3
    completion_gap_attempts_error: int = 10
    notice_amplification_warning: int = 50
    notice_amplification_error: int = 200
    top_n_heavy_executions: int = 10
    hot_row_count_warning: int = 5000
    hot_row_count_error: int = 20000

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryMetrics:
    total_executions: int
    terminal_executions: int
    timeline_rows: int
    checkpoints: int
    finalized_manifests: int
    archived_manifests: int
    total_trace_bytes: int
    trace_file_count: int
    hot_row_count_by_family: dict[str, int]
    cold_trace_bytes_by_execution: dict[str, int]
    event_count_by_execution: dict[str, int]
    oversized_execution_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryTopN:
    top_heavy_executions: tuple[dict[str, Any], ...]
    top_event_families: tuple[dict[str, Any], ...]
    top_cold_trace_by_bytes: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryThresholdBreach:
    level: str
    metric: str
    value: Any
    threshold: Any
    message: str
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryDiagnosticReport:
    metrics: ExecutionHistoryMetrics
    top_n: ExecutionHistoryTopN
    threshold_breaches: tuple[ExecutionHistoryThresholdBreach, ...]
    startup_recovery_duration_seconds: Optional[float]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class CompletionGapDetection:
    execution_id: str
    attempt_count: int
    first_attempt_at: Optional[str]
    last_attempt_at: Optional[str]
    breach_level: str
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def collect_execution_history_metrics(
    hub_root: Path,
    *,
    policy: Optional[ExecutionHistoryMaintenancePolicy] = None,
) -> ExecutionHistoryMetrics:
    resolved_policy = policy or ExecutionHistoryMaintenancePolicy()
    store = ColdTraceStore(hub_root)
    manifests = list(store.iter_manifests())
    checkpoints = list(store.iter_checkpoints())

    manifest_by_execution: dict[str, int] = {}
    for manifest in manifests:
        eid = manifest.execution_id
        if eid:
            manifest_by_execution[eid] = manifest_by_execution.get(eid, 0) + 1

    cold_bytes_by_execution: dict[str, int] = {}
    for manifest in manifests:
        eid = manifest.execution_id
        if eid:
            cold_bytes_by_execution[eid] = cold_bytes_by_execution.get(eid, 0) + int(
                manifest.byte_count or 0
            )

    finalized_manifests = sum(1 for m in manifests if m.status == "finalized")
    archived_manifests = sum(1 for m in manifests if m.status == "archived")
    total_trace_bytes = sum(int(m.byte_count or 0) for m in manifests)

    event_count_by_execution: dict[str, int] = {}
    oversized_ids: list[str] = []

    with open_orchestration_sqlite(hub_root) as conn:
        execution_rows = conn.execute(
            "SELECT execution_id, status FROM orch_thread_executions ORDER BY created_at ASC"
        ).fetchall()
        hot_rows_by_family = _collect_timeline_family_counts(conn)

        for row in conn.execute(
            """
            SELECT execution_id, COUNT(*) AS cnt
              FROM orch_event_projections
             INDEXED BY idx_orch_event_projections_family_execution_order
             WHERE event_family = ?
               AND execution_id IS NOT NULL
             GROUP BY execution_id
            """,
            (_TIMELINE_EVENT_FAMILY,),
        ).fetchall():
            eid = str(row["execution_id"] or "").strip()
            if eid:
                cnt = int(row["cnt"] or 0)
                event_count_by_execution[eid] = cnt
                if cnt > resolved_policy.max_hot_rows_per_completed_execution:
                    oversized_ids.append(eid)

    return ExecutionHistoryMetrics(
        total_executions=len(execution_rows),
        terminal_executions=sum(
            1
            for r in execution_rows
            if str(r["status"] or "").strip().lower()
            in {
                "completed",
                "failed",
                "cancelled",
                "canceled",
                "interrupted",
                "aborted",
                "stopped",
                "ok",
                "error",
            }
        ),
        timeline_rows=sum(hot_rows_by_family.values()),
        checkpoints=len(checkpoints),
        finalized_manifests=finalized_manifests,
        archived_manifests=archived_manifests,
        total_trace_bytes=total_trace_bytes,
        trace_file_count=sum(1 for m in manifests if m.status in ("finalized", "open")),
        hot_row_count_by_family=hot_rows_by_family,
        cold_trace_bytes_by_execution=dict(sorted(cold_bytes_by_execution.items())),
        event_count_by_execution=dict(sorted(event_count_by_execution.items())),
        oversized_execution_ids=tuple(sorted(oversized_ids)),
    )


def collect_top_n_heavy_executions(
    hub_root: Path,
    *,
    top_n: int = 10,
) -> ExecutionHistoryTopN:
    store = ColdTraceStore(hub_root)
    manifests = list(store.iter_manifests())

    with open_orchestration_sqlite(hub_root) as conn:
        execution_hot_rows = {
            str(row["execution_id"]): int(row["cnt"] or 0)
            for row in conn.execute(
                """
                SELECT execution_id, COUNT(*) AS cnt
                  FROM orch_event_projections
                 WHERE event_family = ?
                   AND execution_id IS NOT NULL
                 GROUP BY execution_id
                """,
                (_TIMELINE_EVENT_FAMILY,),
            ).fetchall()
            if row["execution_id"] is not None
        }

        family_counts = _collect_timeline_family_counts(conn)

    cold_bytes_by_execution: dict[str, int] = {}
    for m in manifests:
        if m.execution_id:
            cold_bytes_by_execution[m.execution_id] = cold_bytes_by_execution.get(
                m.execution_id, 0
            ) + int(m.byte_count or 0)

    hot_sorted = sorted(execution_hot_rows.items(), key=lambda x: x[1], reverse=True)[
        :top_n
    ]
    top_heavy = tuple({"execution_id": eid, "hot_rows": cnt} for eid, cnt in hot_sorted)

    family_sorted = sorted(family_counts.items(), key=lambda x: x[1], reverse=True)[
        :top_n
    ]
    top_families = tuple(
        {"event_family": fam, "hot_rows": cnt} for fam, cnt in family_sorted
    )

    cold_sorted = sorted(
        cold_bytes_by_execution.items(), key=lambda x: x[1], reverse=True
    )[:top_n]
    top_cold = tuple(
        {"execution_id": eid, "cold_trace_bytes": cnt} for eid, cnt in cold_sorted
    )

    return ExecutionHistoryTopN(
        top_heavy_executions=top_heavy,
        top_event_families=top_families,
        top_cold_trace_by_bytes=top_cold,
    )


def check_thresholds(
    metrics: ExecutionHistoryMetrics,
    *,
    thresholds: Optional[ExecutionHistoryThresholds] = None,
) -> tuple[ExecutionHistoryThresholdBreach, ...]:
    t = thresholds or ExecutionHistoryThresholds()
    breaches: list[ExecutionHistoryThresholdBreach] = []

    if metrics.total_trace_bytes >= t.cold_trace_bytes_error:
        breaches.append(
            ExecutionHistoryThresholdBreach(
                level="error",
                metric="total_trace_bytes",
                value=metrics.total_trace_bytes,
                threshold=t.cold_trace_bytes_error,
                message=(
                    f"Cold trace storage ({metrics.total_trace_bytes} bytes) "
                    f"exceeds error threshold ({t.cold_trace_bytes_error} bytes)"
                ),
                context={"finalized_manifests": metrics.finalized_manifests},
            )
        )
    elif metrics.total_trace_bytes >= t.cold_trace_bytes_warning:
        breaches.append(
            ExecutionHistoryThresholdBreach(
                level="warning",
                metric="total_trace_bytes",
                value=metrics.total_trace_bytes,
                threshold=t.cold_trace_bytes_warning,
                message=(
                    f"Cold trace storage ({metrics.total_trace_bytes} bytes) "
                    f"exceeds warning threshold ({t.cold_trace_bytes_warning} bytes)"
                ),
                context={"finalized_manifests": metrics.finalized_manifests},
            )
        )

    if metrics.timeline_rows >= t.hot_row_count_error:
        breaches.append(
            ExecutionHistoryThresholdBreach(
                level="error",
                metric="hot_timeline_rows",
                value=metrics.timeline_rows,
                threshold=t.hot_row_count_error,
                message=(
                    f"Hot projection rows ({metrics.timeline_rows}) "
                    f"exceeds error threshold ({t.hot_row_count_error})"
                ),
                context={"hot_row_count_by_family": metrics.hot_row_count_by_family},
            )
        )
    elif metrics.timeline_rows >= t.hot_row_count_warning:
        breaches.append(
            ExecutionHistoryThresholdBreach(
                level="warning",
                metric="hot_timeline_rows",
                value=metrics.timeline_rows,
                threshold=t.hot_row_count_warning,
                message=(
                    f"Hot projection rows ({metrics.timeline_rows}) "
                    f"exceeds warning threshold ({t.hot_row_count_warning})"
                ),
                context={"hot_row_count_by_family": metrics.hot_row_count_by_family},
            )
        )

    for eid in metrics.oversized_execution_ids:
        hot_rows = metrics.event_count_by_execution.get(eid, 0)
        if hot_rows >= t.oversized_execution_hot_rows:
            breaches.append(
                ExecutionHistoryThresholdBreach(
                    level="warning",
                    metric="oversized_execution",
                    value=hot_rows,
                    threshold=t.oversized_execution_hot_rows,
                    message=(
                        f"Execution {eid} has {hot_rows} hot rows "
                        f"(threshold: {t.oversized_execution_hot_rows})"
                    ),
                    context={"execution_id": eid},
                )
            )

    run_notice_count = int(metrics.hot_row_count_by_family.get("run_notice", 0))
    terminal_baseline = max(int(metrics.terminal_executions or 0), 1)
    average_run_notice_rows = run_notice_count / terminal_baseline
    for family, count in metrics.hot_row_count_by_family.items():
        if (
            family == "run_notice"
            and average_run_notice_rows >= t.notice_amplification_error
        ):
            breaches.append(
                ExecutionHistoryThresholdBreach(
                    level="error",
                    metric="notice_amplification",
                    value=round(average_run_notice_rows, 3),
                    threshold=t.notice_amplification_error,
                    message=(
                        "Average run_notice hot rows per terminal execution "
                        f"({average_run_notice_rows:.2f}) exceeds error threshold "
                        f"({t.notice_amplification_error}); possible notice amplification"
                    ),
                    context={
                        "event_family": family,
                        "run_notice_hot_rows": count,
                        "terminal_executions": metrics.terminal_executions,
                    },
                )
            )
        elif (
            family == "run_notice"
            and average_run_notice_rows >= t.notice_amplification_warning
        ):
            breaches.append(
                ExecutionHistoryThresholdBreach(
                    level="warning",
                    metric="notice_amplification",
                    value=round(average_run_notice_rows, 3),
                    threshold=t.notice_amplification_warning,
                    message=(
                        "Average run_notice hot rows per terminal execution "
                        f"({average_run_notice_rows:.2f}) exceeds warning threshold "
                        f"({t.notice_amplification_warning}); possible notice amplification"
                    ),
                    context={
                        "event_family": family,
                        "run_notice_hot_rows": count,
                        "terminal_executions": metrics.terminal_executions,
                    },
                )
            )

    return tuple(breaches)


def detect_completion_gap_repeated_attempts(
    hub_root: Path,
    *,
    thresholds: Optional[ExecutionHistoryThresholds] = None,
) -> tuple[CompletionGapDetection, ...]:
    t = thresholds or ExecutionHistoryThresholds()
    detections: list[CompletionGapDetection] = []

    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            """
            SELECT execution_id,
                   COUNT(*) AS cnt,
                   MIN(timestamp) AS first_at,
                   MAX(timestamp) AS last_at
              FROM orch_event_projections
             INDEXED BY idx_orch_event_projections_family_type_execution
            WHERE event_family = ?
              AND event_type = 'run_notice'
              AND execution_id IS NOT NULL
              AND (
                    payload_json LIKE '%"kind":"completion_gap"%'
                 OR payload_json LIKE '%"kind": "completion_gap"%'
              )
             GROUP BY execution_id
             HAVING cnt >= ?
            """,
            (_TIMELINE_EVENT_FAMILY, t.completion_gap_attempts_warning),
        ).fetchall()

        for row in rows:
            eid = str(row["execution_id"] or "").strip()
            if not eid:
                continue
            cnt = int(row["cnt"] or 0)
            level = "warning"
            if cnt >= t.completion_gap_attempts_error:
                level = "error"
            detections.append(
                CompletionGapDetection(
                    execution_id=eid,
                    attempt_count=cnt,
                    first_attempt_at=str(row["first_at"] or "").strip() or None,
                    last_attempt_at=str(row["last_at"] or "").strip() or None,
                    breach_level=level,
                    context={"attempts": cnt},
                )
            )

    return tuple(detections)


def run_execution_history_diagnostics(
    hub_root: Path,
    *,
    thresholds: Optional[ExecutionHistoryThresholds] = None,
    policy: Optional[ExecutionHistoryMaintenancePolicy] = None,
    measure_startup_recovery: bool = False,
) -> ExecutionHistoryDiagnosticReport:
    t = thresholds or ExecutionHistoryThresholds()
    start = time.monotonic()

    metrics = collect_execution_history_metrics(hub_root, policy=policy)
    top_n = collect_top_n_heavy_executions(hub_root, top_n=t.top_n_heavy_executions)
    breaches = check_thresholds(metrics, thresholds=t)
    gap_detections = detect_completion_gap_repeated_attempts(hub_root, thresholds=t)

    gap_breaches = [
        ExecutionHistoryThresholdBreach(
            level=gap.breach_level,
            metric="completion_gap_attempts",
            value=gap.attempt_count,
            threshold=(
                t.completion_gap_attempts_error
                if gap.breach_level == "error"
                else t.completion_gap_attempts_warning
            ),
            message=(
                f"Execution {gap.execution_id} has {gap.attempt_count} "
                f"completion-gap recovery attempts"
            ),
            context=gap.context,
        )
        for gap in gap_detections
    ]

    elapsed = time.monotonic() - start
    startup_duration: Optional[float] = None
    if measure_startup_recovery:
        startup_duration = elapsed

    if elapsed >= t.startup_recovery_duration_warning_seconds:
        level = (
            "error"
            if elapsed >= t.startup_recovery_duration_error_seconds
            else "warning"
        )
        gap_breaches.append(
            ExecutionHistoryThresholdBreach(
                level=level,
                metric="diagnostic_scan_duration",
                value=round(elapsed, 3),
                threshold=t.startup_recovery_duration_warning_seconds,
                message=(
                    f"Diagnostic scan took {elapsed:.2f}s; startup recovery may be slow"
                ),
                context={"total_executions": metrics.total_executions},
            )
        )

    all_breaches = tuple(breaches) + tuple(gap_breaches)

    _emit_diagnostic_log(metrics, all_breaches, gap_detections, elapsed)

    return ExecutionHistoryDiagnosticReport(
        metrics=metrics,
        top_n=top_n,
        threshold_breaches=all_breaches,
        startup_recovery_duration_seconds=startup_duration,
        generated_at=now_iso(),
    )


def _emit_diagnostic_log(
    metrics: ExecutionHistoryMetrics,
    breaches: tuple[ExecutionHistoryThresholdBreach, ...],
    gap_detections: tuple[CompletionGapDetection, ...],
    elapsed: float,
) -> None:
    error_breaches = [b for b in breaches if b.level == "error"]
    warning_breaches = [b for b in breaches if b.level == "warning"]

    payload = {
        "event": "execution_history_diagnostics",
        "total_executions": metrics.total_executions,
        "terminal_executions": metrics.terminal_executions,
        "timeline_rows": metrics.timeline_rows,
        "checkpoints": metrics.checkpoints,
        "total_trace_bytes": metrics.total_trace_bytes,
        "oversized_executions": len(metrics.oversized_execution_ids),
        "error_breaches": len(error_breaches),
        "warning_breaches": len(warning_breaches),
        "completion_gap_detections": len(gap_detections),
        "scan_duration_seconds": round(elapsed, 3),
    }

    if error_breaches:
        logger.error(_json_dumps(payload))
    elif warning_breaches:
        logger.warning(_json_dumps(payload))
    else:
        logger.info(_json_dumps(payload))


def log_spill_to_cold(
    *,
    execution_id: str,
    event_family: str,
    has_cold_trace: bool,
    hot_rows_so_far: int,
    hot_limit: int,
) -> None:
    logger.info(
        _json_dumps(
            {
                "event": "hot_projection_spill_to_cold",
                "execution_id": execution_id,
                "event_family": event_family,
                "has_cold_trace": has_cold_trace,
                "hot_rows_so_far": hot_rows_so_far,
                "hot_limit": hot_limit,
                "warning": not has_cold_trace,
            }
        )
    )


def log_dedupe(
    *,
    execution_id: str,
    event_family: str,
    dedupe_reason: str,
    deduped_count: int,
) -> None:
    logger.debug(
        _json_dumps(
            {
                "event": "hot_projection_dedupe",
                "execution_id": execution_id,
                "event_family": event_family,
                "dedupe_reason": dedupe_reason,
                "deduped_count": deduped_count,
            }
        )
    )


def log_truncation(
    *,
    execution_id: str,
    event_family: str,
    original_chars: int,
    truncated_chars: int,
    contract: str,
) -> None:
    logger.debug(
        _json_dumps(
            {
                "event": "hot_projection_truncation",
                "execution_id": execution_id,
                "event_family": event_family,
                "original_chars": original_chars,
                "truncated_chars": truncated_chars,
                "contract": contract,
            }
        )
    )


def log_compaction(
    *,
    execution_id: str,
    rows_before: int,
    rows_after: int,
    rows_deleted: int,
    cold_trace_preserved: bool,
    dry_run: bool | None = None,
) -> None:
    payload = {
        "event": "execution_history_compaction",
        "execution_id": execution_id,
        "rows_before": rows_before,
        "rows_after": rows_after,
        "rows_deleted": rows_deleted,
        "cold_trace_preserved": cold_trace_preserved,
    }
    if dry_run is not None:
        payload["dry_run"] = dry_run
    logger.info(_json_dumps(payload))


def log_retention_prune(
    *,
    pruned_execution_ids: int,
    pruned_trace_ids: int,
    hot_rows_deleted: int,
    bytes_reclaimed: int,
    dry_run: bool | None = None,
) -> None:
    payload = {
        "event": "execution_history_retention_prune",
        "pruned_executions": pruned_execution_ids,
        "pruned_traces": pruned_trace_ids,
        "hot_rows_deleted": hot_rows_deleted,
        "bytes_reclaimed": bytes_reclaimed,
    }
    if dry_run is not None:
        payload["dry_run"] = dry_run
    logger.info(_json_dumps(payload))


def log_vacuum(
    *,
    database_path: str,
    size_before: int,
    size_after: int,
    reclaimed_bytes: int,
) -> None:
    logger.info(
        _json_dumps(
            {
                "event": "execution_history_vacuum",
                "database_path": database_path,
                "size_before": size_before,
                "size_after": size_after,
                "reclaimed_bytes": reclaimed_bytes,
            }
        )
    )


def log_quarantine(
    *,
    execution_id: str,
    reason: str,
    context: dict[str, Any],
) -> None:
    logger.warning(
        _json_dumps(
            {
                "event": "execution_history_quarantine",
                "execution_id": execution_id,
                "reason": reason,
                "context": context,
            }
        )
    )


def log_startup_recovery(
    *,
    duration_seconds: float,
    executions_recovered: int,
    checkpoints_loaded: int,
) -> None:
    level = "warning" if duration_seconds > 5.0 else "info"
    payload = _json_dumps(
        {
            "event": "execution_history_startup_recovery",
            "duration_seconds": round(duration_seconds, 3),
            "executions_recovered": executions_recovered,
            "checkpoints_loaded": checkpoints_loaded,
        }
    )
    if level == "warning":
        logger.warning(payload)
    else:
        logger.info(payload)


__all__ = [
    "CompletionGapDetection",
    "ExecutionHistoryDiagnosticReport",
    "ExecutionHistoryMetrics",
    "ExecutionHistoryThresholdBreach",
    "ExecutionHistoryThresholds",
    "ExecutionHistoryTopN",
    "check_thresholds",
    "collect_execution_history_metrics",
    "collect_top_n_heavy_executions",
    "detect_completion_gap_repeated_attempts",
    "log_compaction",
    "log_dedupe",
    "log_quarantine",
    "log_retention_prune",
    "log_spill_to_cold",
    "log_startup_recovery",
    "log_truncation",
    "log_vacuum",
    "run_execution_history_diagnostics",
]
