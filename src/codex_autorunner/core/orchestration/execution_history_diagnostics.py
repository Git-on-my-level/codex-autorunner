from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..freshness import parse_iso_datetime
from ..text_utils import _json_dumps, _json_loads_object
from ..time_utils import now_iso
from .cold_trace_store import ColdTraceStore
from .execution_history import timeline_hot_family_for_event_type
from .execution_history_maintenance import (
    ExecutionHistoryMaintenancePolicy,
)
from .runtime_chain_diagnostics import (
    RuntimeChainReport,
    collect_runtime_chain_invariant_diagnostics,
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
class CanonicalTurnStateDiagnostic:
    request_id: str
    execution_id: str
    surface_origin: Optional[str]
    target: dict[str, Any]
    lifecycle_phase: str
    terminal_status: Optional[str]
    runtime_options: dict[str, Any]
    recovery_action: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryDiagnosticReport:
    metrics: ExecutionHistoryMetrics
    top_n: ExecutionHistoryTopN
    threshold_breaches: tuple[ExecutionHistoryThresholdBreach, ...]
    canonical_turns: tuple[CanonicalTurnStateDiagnostic, ...]
    runtime_chains: tuple[RuntimeChainReport, ...]
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


def _surface_origin(request: dict[str, Any]) -> Optional[str]:
    origin = request.get("origin")
    if not isinstance(origin, dict):
        return None
    surface_kind = str(origin.get("surface_kind") or "").strip()
    surface_key = str(origin.get("surface_key") or "").strip()
    if surface_kind and surface_key:
        return f"{surface_kind}:{surface_key}"
    source_id = str(origin.get("source_id") or "").strip()
    return source_id or None


def _terminal_status_from_record(status: str) -> Optional[str]:
    normalized = str(status or "").strip().lower()
    if normalized in {"ok", "completed", "complete", "success", "succeeded"}:
        return "completed"
    if normalized in {"error", "failed"}:
        return "failed"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized in {"interrupted", "lost"}:
        return normalized
    return None


def _terminal_write_evidence(conn: Any, execution_id: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT event_id, event_type, timestamp, status, payload_json
          FROM orch_event_projections
         WHERE event_family = ?
           AND execution_id = ?
           AND event_type IN ('turn_completed', 'turn_failed', 'turn_interrupted')
         ORDER BY timestamp ASC, event_id ASC
        """,
        (_TIMELINE_EVENT_FAMILY, execution_id),
    ).fetchall()
    writes: list[dict[str, Any]] = []
    signatures: set[tuple[str, str, str]] = set()
    for row in rows:
        payload = _json_loads_object(row["payload_json"])
        event_obj = payload.get("event")
        event: dict[str, Any] = event_obj if isinstance(event_obj, dict) else {}
        signature = (
            str(row["event_type"] or ""),
            str(row["status"] or ""),
            str(event.get("final_message") or event.get("error_message") or ""),
        )
        signatures.add(signature)
        writes.append(
            {
                "event_id": str(row["event_id"] or ""),
                "event_type": str(row["event_type"] or ""),
                "status": str(row["status"] or ""),
                "timestamp": str(row["timestamp"] or ""),
                "message": signature[2] or None,
            }
        )
    evidence: dict[str, Any] = {"terminal_write_count": len(writes)}
    if writes:
        evidence["terminal_writes"] = writes
    if len(writes) > 1:
        evidence["duplicate_terminal_writes"] = True
        evidence["conflicting_terminal_writes"] = len(signatures) > 1
    return evidence


def _recovery_action_for_record(
    *,
    status: str,
    started_at: Optional[str],
    created_at: Optional[str],
    terminal_status: Optional[str],
    stale_after_seconds: float,
    now: datetime,
) -> tuple[str, str]:
    normalized = str(status or "").strip().lower()
    if terminal_status is not None:
        return "none", "terminal"
    if normalized == "queued":
        return "replay_queued", "queued"
    if normalized == "running":
        basis = parse_iso_datetime(started_at) or parse_iso_datetime(created_at)
        if basis is None:
            return "record_conflict", "running_unknown_age"
        age_seconds = max(0.0, (now - basis).total_seconds())
        if age_seconds >= stale_after_seconds:
            return "classify_stale_running", "stale_running"
        return "wait", "running"
    if normalized in {"claiming"}:
        return "replay_queued", normalized
    return "record_conflict", normalized or "unknown"


def collect_canonical_turn_state_diagnostics(
    hub_root: Path,
    *,
    stale_after_seconds: float = 30 * 60,
    now: Optional[datetime] = None,
) -> tuple[CanonicalTurnStateDiagnostic, ...]:
    resolved_now = now or datetime.now(timezone.utc)
    diagnostics: list[CanonicalTurnStateDiagnostic] = []
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute("""
            SELECT execution_id, thread_target_id, status, started_at, finished_at,
                   created_at, turn_request_json, turn_record_json, error_text
              FROM orch_thread_executions
             ORDER BY created_at ASC, execution_id ASC
            """).fetchall()
        for row in rows:
            execution_id = str(row["execution_id"] or "").strip()
            request = _json_loads_object(row["turn_request_json"])
            record = _json_loads_object(row["turn_record_json"])
            if not execution_id or not request or not record:
                continue
            record_status = str(record.get("status") or row["status"] or "").strip()
            terminal_status = _terminal_status_from_record(record_status)
            recovery_action, lifecycle_phase = _recovery_action_for_record(
                status=record_status,
                started_at=record.get("started_at") or row["started_at"],
                created_at=row["created_at"],
                terminal_status=terminal_status,
                stale_after_seconds=stale_after_seconds,
                now=resolved_now,
            )
            evidence = _terminal_write_evidence(conn, execution_id)
            error_text = (
                str(record.get("error_text") or row["error_text"] or "").strip() or None
            )
            if error_text is not None:
                evidence["error_text"] = error_text
                if "opencode_first_event_timeout" in error_text:
                    evidence["runtime_error_code"] = "opencode_first_event_timeout"
            conflict_evidence = record.get("conflict_evidence")
            if isinstance(conflict_evidence, dict) and conflict_evidence:
                evidence["record_conflict_evidence"] = conflict_evidence
            diagnostics.append(
                CanonicalTurnStateDiagnostic(
                    request_id=str(
                        record.get("request_id")
                        or request.get("request_id")
                        or execution_id
                    ),
                    execution_id=execution_id,
                    surface_origin=_surface_origin(request),
                    target={
                        "target_id": request.get("target_id")
                        or row["thread_target_id"],
                        "target_kind": request.get("target_kind") or "thread",
                        "workspace_root": request.get("workspace_root"),
                    },
                    lifecycle_phase=lifecycle_phase,
                    terminal_status=terminal_status,
                    runtime_options={
                        "agent": request.get("agent"),
                        "profile": request.get("profile"),
                        "model": request.get("model"),
                        "model_payload": request.get("model_payload") or {},
                        "reasoning": request.get("reasoning"),
                        "approval_policy": request.get("approval_policy"),
                        "approval_mode": request.get("approval_mode"),
                        "sandbox_policy": request.get("sandbox_policy"),
                    },
                    recovery_action=recovery_action,
                    evidence=evidence,
                )
            )
    return tuple(diagnostics)


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
    canonical_turns = collect_canonical_turn_state_diagnostics(hub_root)
    runtime_chains = collect_runtime_chain_invariant_diagnostics(hub_root)

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
    runtime_chain_breaches = [
        ExecutionHistoryThresholdBreach(
            level=(
                "error"
                if any(finding.severity == "error" for finding in chain.findings)
                else "warning"
            ),
            metric="runtime_chain_invariant",
            value=len(chain.findings),
            threshold=0,
            message=(
                "Runtime-chain invariant findings for "
                f"{chain.row_identity.get('execution_id') or chain.row_identity.get('chat_id')}"
            ),
            context={
                "lookup": chain.lookup,
                "row_identity": chain.row_identity,
                "codes": [finding.code for finding in chain.findings],
            },
        )
        for chain in runtime_chains
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

    all_breaches = tuple(breaches) + tuple(gap_breaches) + tuple(runtime_chain_breaches)

    _emit_diagnostic_log(metrics, all_breaches, gap_detections, elapsed)

    return ExecutionHistoryDiagnosticReport(
        metrics=metrics,
        top_n=top_n,
        threshold_breaches=all_breaches,
        canonical_turns=canonical_turns,
        runtime_chains=runtime_chains,
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
    "CanonicalTurnStateDiagnostic",
    "CompletionGapDetection",
    "ExecutionHistoryDiagnosticReport",
    "ExecutionHistoryMetrics",
    "ExecutionHistoryThresholdBreach",
    "ExecutionHistoryThresholds",
    "ExecutionHistoryTopN",
    "check_thresholds",
    "collect_canonical_turn_state_diagnostics",
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
