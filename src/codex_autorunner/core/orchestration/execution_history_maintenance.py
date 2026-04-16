from __future__ import annotations

import dataclasses
import json
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, cast

from ..state_roots import resolve_hub_traces_root
from ..text_utils import _json_dumps, _truncate_text
from ..time_utils import now_iso
from .cold_trace_store import ColdTraceStore
from .execution_history import (
    CheckpointSignalStatus,
    ExecutionCheckpoint,
    ExecutionCheckpointSignal,
    ExecutionHistoryEventFamily,
    ExecutionTraceManifest,
    timeline_hot_family_for_event_type,
)
from .sqlite import open_orchestration_sqlite, resolve_orchestration_sqlite_path

_TIMELINE_EVENT_FAMILY = "turn.timeline"
_COMPACTION_SUMMARY_SUFFIX = ":compaction-summary"
_DEFAULT_COMPACTION_MAX_HOT_ROWS = 16
_DEFAULT_HOT_RETENTION_DAYS = 30
_DEFAULT_COLD_TRACE_RETENTION_DAYS = 90
_DEFAULT_DB_SIZE_WARNING_BYTES = 1 * 1024 * 1024 * 1024
_DEFAULT_DB_SIZE_ERROR_BYTES = 3 * 1024 * 1024 * 1024
_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
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
)
_TERMINAL_EVENT_TYPES = frozenset({"turn_completed", "turn_failed"})
_CHECKPOINT_PREVIEW_CHARS = 240
_DEFAULT_AUDIT_LIMIT = 50


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryMaintenancePolicy:
    max_hot_rows_per_completed_execution: int = _DEFAULT_COMPACTION_MAX_HOT_ROWS
    hot_history_retention_days: int = _DEFAULT_HOT_RETENTION_DAYS
    cold_trace_retention_days: int = _DEFAULT_COLD_TRACE_RETENTION_DAYS

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryAuditSummary:
    policy: ExecutionHistoryMaintenancePolicy
    total_executions: int
    terminal_executions: int
    timeline_rows: int
    checkpoints: int
    finalized_manifests: int
    archived_manifests: int
    total_trace_bytes: int
    missing_manifest_execution_ids: tuple[str, ...]
    missing_checkpoint_execution_ids: tuple[str, ...]
    oversized_execution_ids: tuple[str, ...]
    missing_trace_artifact_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryMigrationSummary:
    dry_run: bool
    candidate_executions: int
    manifests_created: int
    checkpoints_created: int
    execution_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryCompactionSummary:
    dry_run: bool
    candidate_executions: int
    compacted_executions: int
    rows_before: int
    rows_after: int
    rows_deleted: int
    summary_rows_written: int
    execution_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryRetentionSummary:
    dry_run: bool
    pruned_execution_ids: tuple[str, ...]
    pruned_trace_ids: tuple[str, ...]
    hot_rows_deleted: int
    checkpoints_deleted: int
    manifests_deleted: int
    trace_files_deleted: int
    bytes_reclaimed: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryExportSummary:
    export_root: str
    execution_count: int
    manifest_count: int
    checkpoint_count: int
    trace_file_count: int
    total_bytes: int
    execution_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryVacuumSummary:
    database_path: str
    size_before: int
    size_after: int
    reclaimed_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryDatabaseHealth:
    database_path: str
    size_bytes: int
    status: str
    warning_threshold_bytes: int
    error_threshold_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExecutionHistoryHousekeepingSummary:
    started_at: str
    finished_at: str
    policy: ExecutionHistoryMaintenancePolicy
    database_size_before_bytes: int
    database_size_after_bytes: int
    compaction: ExecutionHistoryCompactionSummary
    retention: ExecutionHistoryRetentionSummary
    vacuum: Optional[ExecutionHistoryVacuumSummary] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "policy": self.policy.to_dict(),
            "database_size_before_bytes": self.database_size_before_bytes,
            "database_size_after_bytes": self.database_size_after_bytes,
            "compaction": self.compaction.to_dict(),
            "retention": self.retention.to_dict(),
            "vacuum": self.vacuum.to_dict() if self.vacuum is not None else None,
        }


def resolve_execution_history_maintenance_policy(
    config: object,
) -> ExecutionHistoryMaintenancePolicy:
    if config is None:
        return ExecutionHistoryMaintenancePolicy()
    if isinstance(config, ExecutionHistoryMaintenancePolicy):
        return config

    def _mapping_value(key: str, fallback: int) -> int:
        if isinstance(config, Mapping):
            raw = config.get(key, fallback)
        else:
            raw = getattr(config, key, fallback)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = fallback
        return max(0, value)

    max_hot_rows = _mapping_value(
        "orchestration_compaction_max_hot_rows",
        _DEFAULT_COMPACTION_MAX_HOT_ROWS,
    )
    if max_hot_rows <= 0:
        max_hot_rows = _DEFAULT_COMPACTION_MAX_HOT_ROWS
    return ExecutionHistoryMaintenancePolicy(
        max_hot_rows_per_completed_execution=max_hot_rows,
        hot_history_retention_days=_mapping_value(
            "orchestration_hot_history_retention_days",
            _DEFAULT_HOT_RETENTION_DAYS,
        ),
        cold_trace_retention_days=_mapping_value(
            "orchestration_cold_trace_retention_days",
            _DEFAULT_COLD_TRACE_RETENTION_DAYS,
        ),
    )


def audit_execution_history(
    hub_root: Path,
    *,
    policy: ExecutionHistoryMaintenancePolicy | None = None,
    limit: int = _DEFAULT_AUDIT_LIMIT,
) -> ExecutionHistoryAuditSummary:
    resolved_policy = policy or ExecutionHistoryMaintenancePolicy()
    store = ColdTraceStore(hub_root)
    manifests = list(store.iter_manifests())
    checkpoints = list(store.iter_checkpoints())
    manifest_by_execution = {
        manifest.execution_id: manifest
        for manifest in manifests
        if manifest.execution_id
    }
    checkpoint_ids = {
        checkpoint.execution_id
        for checkpoint in checkpoints
        if isinstance(checkpoint.execution_id, str) and checkpoint.execution_id.strip()
    }
    cold_cutoff = _iso_cutoff(resolved_policy.cold_trace_retention_days)

    missing_trace_artifact_ids: list[str] = []
    for manifest in manifests:
        artifact_path = _manifest_artifact_path(hub_root, manifest)
        if not artifact_path.exists():
            missing_trace_artifact_ids.append(manifest.trace_id)

    with open_orchestration_sqlite(hub_root) as conn:
        execution_rows = conn.execute(
            """
            SELECT execution_id, status, finished_at
              FROM orch_thread_executions
             ORDER BY created_at ASC
            """
        ).fetchall()
        timeline_counts = {
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

    terminal_execution_ids: list[str] = []
    missing_manifest_execution_ids: list[str] = []
    missing_checkpoint_execution_ids: list[str] = []
    oversized_execution_ids: list[str] = []
    for row in execution_rows:
        execution_id = str(row["execution_id"] or "").strip()
        if not execution_id:
            continue
        if not _is_terminal_execution_row(row):
            continue
        terminal_execution_ids.append(execution_id)
        hot_rows = timeline_counts.get(execution_id, 0)
        finished_at = _execution_finished_at(row)
        within_cold_retention = (
            cold_cutoff is None
            or not finished_at
            or str(finished_at).strip() >= cold_cutoff
        )
        if within_cold_retention and execution_id not in manifest_by_execution:
            missing_manifest_execution_ids.append(execution_id)
        if execution_id not in checkpoint_ids:
            missing_checkpoint_execution_ids.append(execution_id)
        if hot_rows > resolved_policy.max_hot_rows_per_completed_execution:
            oversized_execution_ids.append(execution_id)

    finalized_manifests = sum(
        1 for manifest in manifests if manifest.status == "finalized"
    )
    archived_manifests = sum(
        1 for manifest in manifests if manifest.status == "archived"
    )
    total_trace_bytes = sum(int(manifest.byte_count or 0) for manifest in manifests)
    return ExecutionHistoryAuditSummary(
        policy=resolved_policy,
        total_executions=len(execution_rows),
        terminal_executions=len(terminal_execution_ids),
        timeline_rows=sum(timeline_counts.values()),
        checkpoints=len(checkpoints),
        finalized_manifests=finalized_manifests,
        archived_manifests=archived_manifests,
        total_trace_bytes=total_trace_bytes,
        missing_manifest_execution_ids=tuple(
            sorted(missing_manifest_execution_ids)[: max(int(limit), 0)]
        ),
        missing_checkpoint_execution_ids=tuple(
            sorted(missing_checkpoint_execution_ids)[: max(int(limit), 0)]
        ),
        oversized_execution_ids=tuple(
            sorted(oversized_execution_ids)[: max(int(limit), 0)]
        ),
        missing_trace_artifact_ids=tuple(
            sorted(missing_trace_artifact_ids)[: max(int(limit), 0)]
        ),
    )


def backfill_legacy_execution_history(
    hub_root: Path,
    *,
    execution_ids: Optional[Sequence[str]] = None,
    dry_run: bool = False,
) -> ExecutionHistoryMigrationSummary:
    store = ColdTraceStore(hub_root)
    target_ids = _normalized_execution_ids(execution_ids)
    manifests_created = 0
    checkpoints_created = 0
    processed_ids: list[str] = []

    with open_orchestration_sqlite(hub_root) as conn:
        execution_rows = _select_execution_rows(conn, execution_ids=target_ids)
    for execution_row in execution_rows:
        execution_id = str(execution_row["execution_id"] or "").strip()
        if not execution_id or not _is_terminal_execution_row(execution_row):
            continue
        with open_orchestration_sqlite(hub_root) as conn:
            timeline_rows = _load_timeline_rows(conn, execution_id)
        if not timeline_rows and store.load_checkpoint(execution_id) is not None:
            continue
        existing_manifest = store.get_manifest(execution_id)
        artifact_missing = (
            existing_manifest is not None
            and not _manifest_artifact_path(hub_root, existing_manifest).exists()
        )
        checkpoint = store.load_checkpoint(execution_id)
        needs_manifest = bool(timeline_rows) and (
            existing_manifest is None or artifact_missing
        )
        needs_checkpoint = checkpoint is None
        if not needs_manifest and not needs_checkpoint:
            continue

        processed_ids.append(execution_id)
        manifest = existing_manifest
        if needs_manifest and not dry_run:
            manifest = _backfill_manifest_from_timeline_rows(
                hub_root,
                execution_row=execution_row,
                timeline_rows=timeline_rows,
            )
            manifests_created += 1
        elif needs_manifest:
            manifests_created += 1

        if needs_checkpoint and not dry_run:
            checkpoint = _build_checkpoint_from_execution_row(
                execution_row,
                timeline_rows,
                trace_manifest_id=(manifest.trace_id if manifest is not None else None),
            )
            store.save_checkpoint(checkpoint)
            checkpoints_created += 1
        elif needs_checkpoint:
            checkpoints_created += 1

    return ExecutionHistoryMigrationSummary(
        dry_run=dry_run,
        candidate_executions=len(processed_ids),
        manifests_created=manifests_created,
        checkpoints_created=checkpoints_created,
        execution_ids=tuple(processed_ids),
    )


def compact_completed_execution_history(
    hub_root: Path,
    *,
    execution_ids: Optional[Sequence[str]] = None,
    policy: ExecutionHistoryMaintenancePolicy | None = None,
    dry_run: bool = False,
) -> ExecutionHistoryCompactionSummary:
    from .execution_history_diagnostics import log_compaction

    resolved_policy = policy or ExecutionHistoryMaintenancePolicy()
    store = ColdTraceStore(hub_root)
    target_ids = _normalized_execution_ids(execution_ids)
    rows_before = 0
    rows_after = 0
    rows_deleted = 0
    summary_rows_written = 0
    compacted_ids: list[str] = []

    with open_orchestration_sqlite(hub_root) as conn:
        execution_rows = _select_execution_rows(conn, execution_ids=target_ids)
    for execution_row in execution_rows:
        execution_id = str(execution_row["execution_id"] or "").strip()
        if not execution_id or not _is_terminal_execution_row(execution_row):
            continue
        with open_orchestration_sqlite(hub_root) as conn:
            timeline_row_count = _count_baseline_timeline_rows(conn, execution_id)
            if (
                timeline_row_count
                <= resolved_policy.max_hot_rows_per_completed_execution
            ):
                continue
            timeline_rows = _load_timeline_rows(conn, execution_id)
        baseline_rows = [
            row for row in timeline_rows if not _is_compaction_summary_row(row)
        ]

        compacted_ids.append(execution_id)
        rows_before += len(baseline_rows)

        manifest = store.get_manifest(execution_id)
        artifact_missing = (
            manifest is not None
            and not _manifest_artifact_path(hub_root, manifest).exists()
        )
        if (manifest is None or artifact_missing) and baseline_rows and not dry_run:
            manifest = _backfill_manifest_from_timeline_rows(
                hub_root,
                execution_row=execution_row,
                timeline_rows=baseline_rows,
            )
        checkpoint = store.load_checkpoint(execution_id)
        if checkpoint is None and not dry_run:
            checkpoint = _build_checkpoint_from_execution_row(
                execution_row,
                baseline_rows,
                trace_manifest_id=(manifest.trace_id if manifest is not None else None),
            )
            store.save_checkpoint(checkpoint)

        keep_rows = _select_compaction_keep_rows(
            baseline_rows,
            max_hot_rows=resolved_policy.max_hot_rows_per_completed_execution,
        )
        rows_after += len(keep_rows) + 1
        rows_deleted += max(len(baseline_rows) - len(keep_rows), 0)
        summary_rows_written += 1

        if dry_run:
            log_compaction(
                execution_id=execution_id,
                rows_before=len(baseline_rows),
                rows_after=len(keep_rows) + 1,
                rows_deleted=max(len(baseline_rows) - len(keep_rows), 0),
                cold_trace_preserved=manifest is not None,
                dry_run=True,
            )
            continue

        summary_row = _build_compaction_summary_row(
            execution_row=execution_row,
            keep_rows=keep_rows,
            original_rows=baseline_rows,
            trace_manifest_id=manifest.trace_id if manifest is not None else None,
        )
        keep_event_ids = {
            str(row["event_id"])
            for row in keep_rows
            if isinstance(row.get("event_id"), str)
        }
        with open_orchestration_sqlite(hub_root) as conn:
            with conn:
                conn.execute(
                    """
                    DELETE FROM orch_event_projections
                     WHERE event_family = ?
                       AND execution_id = ?
                       AND event_id LIKE ?
                    """,
                    (
                        _TIMELINE_EVENT_FAMILY,
                        execution_id,
                        f"%{_COMPACTION_SUMMARY_SUFFIX}",
                    ),
                )
                if keep_event_ids:
                    placeholders = ",".join("?" for _ in keep_event_ids)
                    conn.execute(
                        f"""
                        DELETE FROM orch_event_projections
                         WHERE event_family = ?
                           AND execution_id = ?
                           AND event_id NOT IN ({placeholders})
                        """,
                        (_TIMELINE_EVENT_FAMILY, execution_id, *sorted(keep_event_ids)),
                    )
                else:
                    conn.execute(
                        """
                        DELETE FROM orch_event_projections
                         WHERE event_family = ?
                           AND execution_id = ?
                        """,
                        (_TIMELINE_EVENT_FAMILY, execution_id),
                    )
                conn.execute(
                    """
                    INSERT INTO orch_event_projections (
                        event_id,
                        event_family,
                        event_type,
                        target_kind,
                        target_id,
                        execution_id,
                        repo_id,
                        resource_kind,
                        resource_id,
                        run_id,
                        timestamp,
                        status,
                        payload_json,
                        processed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        event_family = excluded.event_family,
                        event_type = excluded.event_type,
                        target_kind = excluded.target_kind,
                        target_id = excluded.target_id,
                        execution_id = excluded.execution_id,
                        repo_id = excluded.repo_id,
                        resource_kind = excluded.resource_kind,
                        resource_id = excluded.resource_id,
                        run_id = excluded.run_id,
                        timestamp = excluded.timestamp,
                        status = excluded.status,
                        payload_json = excluded.payload_json,
                        processed = excluded.processed
                    """,
                    (
                        summary_row["event_id"],
                        _TIMELINE_EVENT_FAMILY,
                        summary_row["event_type"],
                        summary_row["target_kind"],
                        summary_row["target_id"],
                        execution_id,
                        summary_row["repo_id"],
                        summary_row["resource_kind"],
                        summary_row["resource_id"],
                        summary_row["run_id"],
                        summary_row["timestamp"],
                        summary_row["status"],
                        _json_dumps(summary_row["payload"]),
                        1,
                    ),
                )
        if checkpoint is not None:
            current_hot_rows = [*keep_rows, _summary_row_to_timeline_row(summary_row)]
            refreshed_hot_state = _merge_checkpoint_hot_state(
                checkpoint.hot_projection_state,
                _build_hot_projection_state_from_rows(current_hot_rows),
            )
            checkpoint = dataclasses.replace(
                checkpoint,
                hot_projection_state=refreshed_hot_state,
                trace_manifest_id=(
                    manifest.trace_id
                    if manifest is not None
                    else checkpoint.trace_manifest_id
                ),
            )
            store.save_checkpoint(checkpoint)
        log_compaction(
            execution_id=execution_id,
            rows_before=len(baseline_rows),
            rows_after=len(keep_rows) + 1,
            rows_deleted=max(len(baseline_rows) - len(keep_rows), 0),
            cold_trace_preserved=manifest is not None,
            dry_run=False,
        )

    return ExecutionHistoryCompactionSummary(
        dry_run=dry_run,
        candidate_executions=len(compacted_ids),
        compacted_executions=len(compacted_ids),
        rows_before=rows_before,
        rows_after=rows_after,
        rows_deleted=rows_deleted,
        summary_rows_written=summary_rows_written,
        execution_ids=tuple(compacted_ids),
    )


def prune_execution_history_retention(
    hub_root: Path,
    *,
    policy: ExecutionHistoryMaintenancePolicy,
    dry_run: bool = False,
) -> ExecutionHistoryRetentionSummary:
    from .execution_history_diagnostics import log_retention_prune

    hot_cutoff = _iso_cutoff(policy.hot_history_retention_days)
    cold_cutoff = _iso_cutoff(policy.cold_trace_retention_days)
    store = ColdTraceStore(hub_root)
    pruned_execution_ids: list[str] = []
    pruned_trace_ids: list[str] = []
    hot_rows_deleted = 0
    checkpoints_deleted = 0
    manifests_deleted = 0
    trace_files_deleted = 0
    bytes_reclaimed = 0

    with open_orchestration_sqlite(hub_root) as conn:
        execution_rows = _select_execution_rows(conn)
        for execution_row in execution_rows:
            execution_id = str(execution_row["execution_id"] or "").strip()
            if not execution_id or not _is_terminal_execution_row(execution_row):
                continue
            finished_at = _execution_finished_at(execution_row)
            if hot_cutoff is None or not finished_at or finished_at > hot_cutoff:
                continue
            row_count = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                  FROM orch_event_projections
                 WHERE event_family = ?
                   AND execution_id = ?
                """,
                (_TIMELINE_EVENT_FAMILY, execution_id),
            ).fetchone()
            deleted_hot_rows = int(row_count["cnt"] or 0) if row_count else 0
            checkpoint_exists = (
                conn.execute(
                    """
                    SELECT 1 AS ok
                      FROM orch_execution_checkpoints
                     WHERE execution_id = ?
                     LIMIT 1
                    """,
                    (execution_id,),
                ).fetchone()
                is not None
            )
            if deleted_hot_rows or checkpoint_exists:
                pruned_execution_ids.append(execution_id)
            hot_rows_deleted += deleted_hot_rows
            if checkpoint_exists:
                checkpoints_deleted += 1
            if dry_run:
                continue
            with conn:
                conn.execute(
                    """
                    DELETE FROM orch_event_projections
                     WHERE event_family = ?
                       AND execution_id = ?
                    """,
                    (_TIMELINE_EVENT_FAMILY, execution_id),
                )
                conn.execute(
                    """
                    DELETE FROM orch_execution_checkpoints
                     WHERE execution_id = ?
                    """,
                    (execution_id,),
                )

        if cold_cutoff is not None:
            checkpoints_by_trace_manifest_id: dict[str, list[ExecutionCheckpoint]] = {}
            if not dry_run:
                for checkpoint in store.iter_checkpoints():
                    trace_manifest_id = str(checkpoint.trace_manifest_id or "").strip()
                    if not trace_manifest_id:
                        continue
                    checkpoints_by_trace_manifest_id.setdefault(
                        trace_manifest_id, []
                    ).append(checkpoint)
            manifests = list(store.iter_manifests())
            for manifest in manifests:
                manifest_timestamp = manifest.finished_at or manifest.started_at
                if not manifest_timestamp:
                    continue
                if not manifest_timestamp or manifest_timestamp > cold_cutoff:
                    continue
                artifact_path = _manifest_artifact_path(hub_root, manifest)
                if artifact_path.exists():
                    try:
                        file_bytes = artifact_path.stat().st_size
                    except OSError:
                        file_bytes = int(manifest.byte_count or 0)
                else:
                    file_bytes = 0
                pruned_trace_ids.append(manifest.trace_id)
                manifests_deleted += 1
                if artifact_path.exists():
                    trace_files_deleted += 1
                    bytes_reclaimed += file_bytes
                if dry_run:
                    continue
                affected_checkpoints = checkpoints_by_trace_manifest_id.get(
                    manifest.trace_id, []
                )
                for checkpoint in affected_checkpoints:
                    store.save_checkpoint(
                        dataclasses.replace(
                            checkpoint,
                            trace_manifest_id=None,
                        )
                    )
                with conn:
                    conn.execute(
                        """
                        DELETE FROM orch_cold_trace_manifests
                         WHERE trace_id = ?
                        """,
                        (manifest.trace_id,),
                    )
                if artifact_path.exists():
                    try:
                        artifact_path.unlink()
                    except OSError:
                        pass
                _remove_empty_trace_dirs(
                    artifact_path.parent,
                    resolve_hub_traces_root(hub_root),
                )

    log_retention_prune(
        pruned_execution_ids=len(pruned_execution_ids),
        pruned_trace_ids=len(pruned_trace_ids),
        hot_rows_deleted=hot_rows_deleted,
        bytes_reclaimed=bytes_reclaimed,
        dry_run=dry_run,
    )

    return ExecutionHistoryRetentionSummary(
        dry_run=dry_run,
        pruned_execution_ids=tuple(pruned_execution_ids),
        pruned_trace_ids=tuple(pruned_trace_ids),
        hot_rows_deleted=hot_rows_deleted,
        checkpoints_deleted=checkpoints_deleted,
        manifests_deleted=manifests_deleted,
        trace_files_deleted=trace_files_deleted,
        bytes_reclaimed=bytes_reclaimed,
    )


def export_execution_history_bundle(
    hub_root: Path,
    *,
    export_root: Path,
    execution_ids: Optional[Sequence[str]] = None,
) -> ExecutionHistoryExportSummary:
    store = ColdTraceStore(hub_root)
    normalized_ids = _normalized_execution_ids(execution_ids)
    export_root.mkdir(parents=True, exist_ok=True)
    traces_dest = export_root / "traces"
    traces_dest.mkdir(parents=True, exist_ok=True)

    manifests = list(store.iter_manifests())
    if normalized_ids is not None:
        manifests = [
            manifest
            for manifest in manifests
            if manifest.execution_id in normalized_ids
        ]
    execution_id_set = {
        manifest.execution_id for manifest in manifests if manifest.execution_id
    }
    checkpoints = [
        checkpoint
        for checkpoint in store.iter_checkpoints()
        if checkpoint.execution_id in execution_id_set
    ]
    total_bytes = 0
    trace_file_count = 0
    exported_execution_ids = sorted(execution_id_set)

    for manifest in manifests:
        source = _manifest_artifact_path(hub_root, manifest)
        if not source.exists():
            continue
        dest = traces_dest / manifest.artifact_relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        try:
            total_bytes += dest.stat().st_size
        except OSError:
            total_bytes += int(manifest.byte_count or 0)
        trace_file_count += 1

    (export_root / "manifests.json").write_text(
        json.dumps(
            [manifest.to_dict() for manifest in manifests],
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (export_root / "checkpoints.json").write_text(
        json.dumps(
            [checkpoint.to_dict() for checkpoint in checkpoints],
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (export_root / "export_meta.json").write_text(
        json.dumps(
            {
                "exported_at": now_iso(),
                "hub_root": str(hub_root),
                "execution_ids": exported_execution_ids,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    return ExecutionHistoryExportSummary(
        export_root=str(export_root),
        execution_count=len(exported_execution_ids),
        manifest_count=len(manifests),
        checkpoint_count=len(checkpoints),
        trace_file_count=trace_file_count,
        total_bytes=total_bytes,
        execution_ids=tuple(exported_execution_ids),
    )


def vacuum_execution_history(hub_root: Path) -> ExecutionHistoryVacuumSummary:
    from .execution_history_diagnostics import log_vacuum

    db_path = resolve_orchestration_sqlite_path(hub_root)
    size_before = db_path.stat().st_size if db_path.exists() else 0
    with open_orchestration_sqlite(hub_root) as conn:
        conn.execute("VACUUM")
    size_after = db_path.stat().st_size if db_path.exists() else 0
    reclaimed = max(size_before - size_after, 0)
    log_vacuum(
        database_path=str(db_path),
        size_before=size_before,
        size_after=size_after,
        reclaimed_bytes=reclaimed,
    )
    return ExecutionHistoryVacuumSummary(
        database_path=str(db_path),
        size_before=size_before,
        size_after=size_after,
        reclaimed_bytes=reclaimed,
    )


def execution_history_database_size_bytes(hub_root: Path) -> int:
    db_path = resolve_orchestration_sqlite_path(hub_root)
    try:
        return int(db_path.stat().st_size)
    except OSError:
        return 0


def collect_execution_history_database_health(
    hub_root: Path,
    *,
    warning_threshold_bytes: int = _DEFAULT_DB_SIZE_WARNING_BYTES,
    error_threshold_bytes: int = _DEFAULT_DB_SIZE_ERROR_BYTES,
) -> ExecutionHistoryDatabaseHealth:
    warning_threshold = max(0, int(warning_threshold_bytes))
    error_threshold = max(warning_threshold, int(error_threshold_bytes))
    size_bytes = execution_history_database_size_bytes(hub_root)
    if size_bytes >= error_threshold:
        status = "error"
    elif size_bytes >= warning_threshold:
        status = "warning"
    else:
        status = "ok"
    return ExecutionHistoryDatabaseHealth(
        database_path=str(resolve_orchestration_sqlite_path(hub_root)),
        size_bytes=size_bytes,
        status=status,
        warning_threshold_bytes=warning_threshold,
        error_threshold_bytes=error_threshold,
    )


def run_execution_history_housekeeping_once(
    hub_root: Path,
    *,
    policy: ExecutionHistoryMaintenancePolicy | None = None,
) -> ExecutionHistoryHousekeepingSummary:
    resolved_policy = policy or ExecutionHistoryMaintenancePolicy()
    started_at = now_iso()
    db_size_before = execution_history_database_size_bytes(hub_root)
    compaction = compact_completed_execution_history(
        hub_root,
        policy=resolved_policy,
    )
    retention = prune_execution_history_retention(
        hub_root,
        policy=resolved_policy,
    )
    vacuum_summary: Optional[ExecutionHistoryVacuumSummary] = None
    if compaction.rows_deleted > 0 or retention.hot_rows_deleted > 0:
        vacuum_summary = vacuum_execution_history(hub_root)
    db_size_after = (
        vacuum_summary.size_after
        if vacuum_summary is not None
        else execution_history_database_size_bytes(hub_root)
    )
    return ExecutionHistoryHousekeepingSummary(
        started_at=started_at,
        finished_at=now_iso(),
        policy=resolved_policy,
        database_size_before_bytes=db_size_before,
        database_size_after_bytes=db_size_after,
        compaction=compaction,
        retention=retention,
        vacuum=vacuum_summary,
    )


def _normalized_execution_ids(
    execution_ids: Optional[Sequence[str]],
) -> Optional[tuple[str, ...]]:
    if execution_ids is None:
        return None
    values = tuple(
        sorted(
            {
                str(execution_id).strip()
                for execution_id in execution_ids
                if str(execution_id).strip()
            }
        )
    )
    return values or None


def _select_execution_rows(
    conn: Any,
    *,
    execution_ids: Optional[Sequence[str]] = None,
) -> list[Any]:
    if execution_ids:
        placeholders = ",".join("?" for _ in execution_ids)
        rows = conn.execute(
            f"""
            SELECT e.*,
                   t.backend_thread_id,
                   t.repo_id,
                   t.resource_kind,
                   t.resource_id
              FROM orch_thread_executions AS e
              JOIN orch_thread_targets AS t
                ON t.thread_target_id = e.thread_target_id
             WHERE e.execution_id IN ({placeholders})
             ORDER BY e.created_at ASC
            """,
            tuple(execution_ids),
        ).fetchall()
        return cast(list[Any], rows)
    rows = conn.execute(
        """
        SELECT e.*,
               t.backend_thread_id,
               t.repo_id,
               t.resource_kind,
               t.resource_id
          FROM orch_thread_executions AS e
          JOIN orch_thread_targets AS t
            ON t.thread_target_id = e.thread_target_id
         ORDER BY e.created_at ASC
        """
    ).fetchall()
    return cast(list[Any], rows)


def _load_timeline_rows(conn: Any, execution_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT event_id,
               event_type,
               target_kind,
               target_id,
               execution_id,
               repo_id,
               resource_kind,
               resource_id,
               run_id,
               timestamp,
               status,
               payload_json
          FROM orch_event_projections
         INDEXED BY idx_orch_event_projections_family_execution_order
         WHERE event_family = ?
           AND execution_id = ?
         ORDER BY timestamp ASC, event_id ASC
        """,
        (_TIMELINE_EVENT_FAMILY, execution_id),
    ).fetchall()
    parsed_rows: list[dict[str, Any]] = []
    for row in rows:
        parsed_rows.append(
            {
                "event_id": str(row["event_id"] or ""),
                "event_type": str(row["event_type"] or ""),
                "target_kind": row["target_kind"],
                "target_id": row["target_id"],
                "execution_id": row["execution_id"],
                "repo_id": row["repo_id"],
                "resource_kind": row["resource_kind"],
                "resource_id": row["resource_id"],
                "run_id": row["run_id"],
                "timestamp": str(row["timestamp"] or ""),
                "status": str(row["status"] or ""),
                "payload": _decode_payload(row["payload_json"]),
            }
        )
    return parsed_rows


def _count_baseline_timeline_rows(conn: Any, execution_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
          FROM orch_event_projections
         INDEXED BY idx_orch_event_projections_family_execution_order
         WHERE event_family = ?
           AND execution_id = ?
           AND event_id NOT LIKE ?
        """,
        (_TIMELINE_EVENT_FAMILY, execution_id, f"%{_COMPACTION_SUMMARY_SUFFIX}"),
    ).fetchone()
    if row is None:
        return 0
    return int(row["cnt"] or 0)


def _decode_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_terminal_execution_row(row: Any) -> bool:
    status = str(row["status"] or "").strip().lower()
    if status in _TERMINAL_EXECUTION_STATUSES:
        return True
    return bool(str(row["finished_at"] or "").strip())


def _execution_finished_at(row: Any) -> Optional[str]:
    for key in ("finished_at", "started_at", "created_at"):
        value = str(row[key] or "").strip()
        if value:
            return value
    return None


def _infer_trace_event_family(
    event_type: str, payload: Mapping[str, Any]
) -> ExecutionHistoryEventFamily:
    payload_family = str(payload.get("event_family") or "").strip()
    if payload_family in {
        "tool_call",
        "tool_result",
        "output_delta",
        "run_notice",
        "token_usage",
        "terminal",
        "provider_raw",
    }:
        return cast(ExecutionHistoryEventFamily, payload_family)
    return cast(
        ExecutionHistoryEventFamily,
        timeline_hot_family_for_event_type(event_type) or "run_notice",
    )


def _backfill_manifest_from_timeline_rows(
    hub_root: Path,
    *,
    execution_row: Any,
    timeline_rows: Sequence[dict[str, Any]],
) -> ExecutionTraceManifest:
    store = ColdTraceStore(hub_root)
    with store.open_writer(
        execution_id=str(execution_row["execution_id"]),
        backend_thread_id=_optional_text(execution_row["backend_thread_id"]),
        backend_turn_id=_optional_text(execution_row["backend_turn_id"]),
    ) as writer:
        for row in timeline_rows:
            payload = dict(row.get("payload") or {})
            payload.setdefault("timestamp", row.get("timestamp"))
            payload.setdefault("status", row.get("status"))
            payload.setdefault("legacy_hot_projection", True)
            writer.append(
                event_family=_infer_trace_event_family(
                    str(row.get("event_type") or ""), payload
                ),
                event_type=str(row.get("event_type") or ""),
                payload=payload,
            )
        return writer.finalize()


def _build_checkpoint_from_execution_row(
    execution_row: Any,
    timeline_rows: Sequence[dict[str, Any]],
    *,
    trace_manifest_id: Optional[str],
) -> ExecutionCheckpoint:
    assistant_text = str(execution_row["assistant_text"] or "")
    if not assistant_text:
        assistant_text = _reconstruct_assistant_text(timeline_rows)
    progress_kind, progress_preview, progress_chars, last_progress_at = (
        _extract_progress_preview(timeline_rows)
    )
    token_usage = _extract_token_usage(timeline_rows)
    raw_event_count = len(timeline_rows)
    projection_event_cursor = max(
        (
            _coerce_event_index(row.get("payload")) or index
            for index, row in enumerate(timeline_rows, start=1)
        ),
        default=0,
    )
    terminal_signals = _extract_terminal_signals(timeline_rows)
    hot_state = _build_hot_projection_state_from_rows(timeline_rows)
    if progress_kind and progress_preview:
        latest_notice_by_kind = dict(hot_state.get("last_notice_by_kind") or {})
        latest_notice_by_kind[progress_kind] = progress_preview
        hot_state["last_notice_by_kind"] = dict(sorted(latest_notice_by_kind.items()))
    output_preview = _last_output_preview_by_type(timeline_rows)
    if output_preview:
        hot_state["last_output_by_type"] = output_preview
    checkpoint_status = (
        _optional_text(execution_row["status"])
        or (terminal_signals[-1].status if terminal_signals else None)
        or "completed"
    )
    failure_cause = _optional_text(
        execution_row["error_text"]
    ) or _extract_failure_cause(timeline_rows)
    return ExecutionCheckpoint(
        status=checkpoint_status or "completed",
        execution_id=_optional_text(execution_row["execution_id"]),
        thread_target_id=_optional_text(execution_row["thread_target_id"]),
        backend_thread_id=_optional_text(execution_row["backend_thread_id"]),
        backend_turn_id=_optional_text(execution_row["backend_turn_id"]),
        assistant_text_preview=_truncate_text(
            assistant_text,
            _CHECKPOINT_PREVIEW_CHARS,
            suffix="",
        ),
        assistant_char_count=len(assistant_text),
        progress_text_preview=progress_preview,
        progress_text_kind=progress_kind,
        progress_char_count=progress_chars,
        last_progress_at=last_progress_at,
        transport_status=_optional_text(execution_row["status"]),
        token_usage=token_usage,
        failure_cause=failure_cause,
        raw_event_count=raw_event_count,
        projection_event_cursor=projection_event_cursor,
        hot_projection_state=hot_state,
        terminal_signals=terminal_signals,
        trace_manifest_id=trace_manifest_id,
    )


def _reconstruct_assistant_text(timeline_rows: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in timeline_rows:
        if str(row.get("event_type") or "").strip() != "output_delta":
            continue
        event = row.get("payload", {}).get("event")
        if not isinstance(event, dict):
            continue
        delta_type = str(event.get("delta_type") or "").strip()
        if delta_type not in {"assistant_message", "assistant_stream"}:
            continue
        parts.append(str(event.get("content") or ""))
    return "".join(parts)


def _extract_progress_preview(
    timeline_rows: Sequence[dict[str, Any]],
) -> tuple[Optional[str], str, int, Optional[str]]:
    latest_kind: Optional[str] = None
    latest_message = ""
    latest_timestamp: Optional[str] = None
    for row in timeline_rows:
        if str(row.get("event_type") or "").strip() != "run_notice":
            continue
        event = row.get("payload", {}).get("event")
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "").strip()
        if kind not in {"progress", "thinking"}:
            continue
        latest_kind = kind
        latest_message = str(event.get("message") or "")
        latest_timestamp = str(row.get("timestamp") or "").strip() or latest_timestamp
    return (
        latest_kind,
        _truncate_text(latest_message, _CHECKPOINT_PREVIEW_CHARS, suffix=""),
        len(latest_message),
        latest_timestamp,
    )


def _extract_token_usage(
    timeline_rows: Sequence[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    for row in reversed(timeline_rows):
        if str(row.get("event_type") or "").strip() != "token_usage":
            continue
        event = row.get("payload", {}).get("event")
        if not isinstance(event, dict):
            continue
        usage = event.get("usage")
        if isinstance(usage, dict):
            return dict(usage)
    return None


def _extract_failure_cause(timeline_rows: Sequence[dict[str, Any]]) -> Optional[str]:
    for row in reversed(timeline_rows):
        if str(row.get("event_type") or "").strip() != "turn_failed":
            continue
        event = row.get("payload", {}).get("event")
        if not isinstance(event, dict):
            continue
        message = str(event.get("error_message") or event.get("message") or "").strip()
        if message:
            return message
    return None


def _extract_terminal_signals(
    timeline_rows: Sequence[dict[str, Any]],
) -> tuple[ExecutionCheckpointSignal, ...]:
    signals: list[ExecutionCheckpointSignal] = []
    for row in timeline_rows:
        event_type = str(row.get("event_type") or "").strip()
        if event_type not in _TERMINAL_EVENT_TYPES:
            continue
        signal_status: CheckpointSignalStatus = (
            "ok" if event_type == "turn_completed" else "error"
        )
        signals.append(
            ExecutionCheckpointSignal(
                source="legacy_hot_projection",
                status=signal_status,
                timestamp=str(row.get("timestamp") or ""),
            )
        )
    return tuple(signals)


def _last_output_preview_by_type(
    timeline_rows: Sequence[dict[str, Any]],
) -> dict[str, str]:
    previews: dict[str, str] = {}
    for row in timeline_rows:
        if str(row.get("event_type") or "").strip() != "output_delta":
            continue
        event = row.get("payload", {}).get("event")
        if not isinstance(event, dict):
            continue
        delta_type = str(event.get("delta_type") or "").strip()
        if not delta_type:
            continue
        previews[delta_type] = _truncate_text(
            str(event.get("content") or ""),
            512,
            suffix="",
        )
    return dict(sorted(previews.items()))


def _coerce_event_index(payload: Any) -> Optional[int]:
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("event_index")
    try:
        if raw is None:
            return None
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return None


def _build_hot_projection_state_from_rows(
    timeline_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    family_counts: Counter[str] = Counter()
    latest_notice_by_kind: dict[str, str] = {}
    latest_output_by_type: dict[str, str] = {}
    for row in timeline_rows:
        event_type = str(row.get("event_type") or "").strip()
        family = _infer_trace_event_family(event_type, row.get("payload") or {})
        family_counts[family] += 1
        event = row.get("payload", {}).get("event")
        if not isinstance(event, dict):
            continue
        if event_type == "run_notice":
            kind = str(event.get("kind") or "").strip()
            if kind:
                latest_notice_by_kind[kind] = _truncate_text(
                    str(event.get("message") or ""),
                    512,
                    suffix="",
                )
        elif event_type == "output_delta":
            delta_type = str(event.get("delta_type") or "").strip()
            if delta_type:
                latest_output_by_type[delta_type] = _truncate_text(
                    str(event.get("content") or ""),
                    512,
                    suffix="",
                )
    hot_state: dict[str, Any] = {"family_hot_rows": dict(sorted(family_counts.items()))}
    if latest_notice_by_kind:
        hot_state["last_notice_by_kind"] = dict(sorted(latest_notice_by_kind.items()))
    if latest_output_by_type:
        hot_state["last_output_by_type"] = dict(sorted(latest_output_by_type.items()))
    return hot_state


def _merge_checkpoint_hot_state(
    existing_hot_state: Any,
    refreshed_hot_state: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing_hot_state) if isinstance(existing_hot_state, dict) else {}
    for key in (
        "deduped_counts",
        "spilled_counts",
        "spilled_without_cold_trace_counts",
    ):
        value = merged.get(key)
        if isinstance(value, dict):
            refreshed_hot_state[key] = dict(sorted(value.items()))
    return refreshed_hot_state


def _select_compaction_keep_rows(
    timeline_rows: Sequence[dict[str, Any]],
    *,
    max_hot_rows: int,
) -> list[dict[str, Any]]:
    limit = max(max_hot_rows - 1, 0)
    if limit <= 0:
        return []

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    def _mark(row: Optional[dict[str, Any]]) -> None:
        if row is None:
            return
        if len(selected) >= limit:
            return
        event_id = str(row.get("event_id") or "").strip()
        if not event_id or event_id in selected_ids:
            return
        selected_ids.add(event_id)
        selected.append(row)

    _mark(
        next(
            (
                row
                for row in reversed(timeline_rows)
                if row["event_type"] in _TERMINAL_EVENT_TYPES
            ),
            None,
        )
    )
    _mark(
        next(
            (row for row in timeline_rows if row["event_type"] == "turn_started"),
            None,
        )
    )
    _mark(
        next(
            (
                row
                for row in reversed(timeline_rows)
                if row["event_type"] == "token_usage"
            ),
            None,
        )
    )

    latest_notice_by_kind: dict[str, dict[str, Any]] = {}
    latest_output_by_type: dict[str, dict[str, Any]] = {}
    for row in timeline_rows:
        event = row.get("payload", {}).get("event")
        if not isinstance(event, dict):
            continue
        if row["event_type"] == "run_notice":
            kind = str(event.get("kind") or "").strip()
            if kind:
                latest_notice_by_kind[kind] = row
        elif row["event_type"] == "output_delta":
            delta_type = str(event.get("delta_type") or "").strip()
            if delta_type:
                latest_output_by_type[delta_type] = row

    for row in sorted(
        latest_notice_by_kind.values(),
        key=lambda candidate: (
            str(candidate.get("timestamp") or ""),
            str(candidate.get("event_id") or ""),
        ),
        reverse=True,
    ):
        _mark(row)
    for row in sorted(
        latest_output_by_type.values(),
        key=lambda candidate: (
            str(candidate.get("timestamp") or ""),
            str(candidate.get("event_id") or ""),
        ),
        reverse=True,
    ):
        _mark(row)

    prioritized_recent_rows = [
        row
        for row in reversed(timeline_rows)
        if row["event_type"]
        in {"tool_call", "tool_result", "run_notice", "output_delta"}
    ]
    for row in prioritized_recent_rows:
        _mark(row)
    return sorted(
        selected,
        key=lambda row: (
            str(row.get("timestamp") or ""),
            str(row.get("event_id") or ""),
        ),
    )


def _summary_row_to_timeline_row(summary_row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(summary_row.get("event_id") or ""),
        "event_type": str(summary_row.get("event_type") or ""),
        "timestamp": str(summary_row.get("timestamp") or ""),
        "payload": dict(summary_row.get("payload") or {}),
    }


def _build_compaction_summary_row(
    *,
    execution_row: Any,
    keep_rows: Sequence[dict[str, Any]],
    original_rows: Sequence[dict[str, Any]],
    trace_manifest_id: Optional[str],
) -> dict[str, Any]:
    family_counts = Counter(
        _infer_trace_event_family(row["event_type"], row["payload"])
        for row in original_rows
    )
    kept_ids = {str(row.get("event_id") or "") for row in keep_rows}
    timestamp = (
        _execution_finished_at(execution_row)
        or (original_rows[-1]["timestamp"] if original_rows else now_iso())
        or now_iso()
    )
    execution_id = str(execution_row["execution_id"] or "")
    summary_event_index = (
        max(
            (
                _coerce_event_index(row.get("payload")) or index
                for index, row in enumerate(original_rows, start=1)
            ),
            default=0,
        )
        + 1
    )
    removed_rows = [
        row for row in original_rows if str(row.get("event_id") or "") not in kept_ids
    ]
    payload = {
        "event_index": summary_event_index,
        "event_type": "run_notice",
        "event_family": "run_notice",
        "storage_layer": "hot_projection",
        "hot_payload_contract": "structured_event",
        "captures_cold_trace": bool(trace_manifest_id),
        "updates_checkpoint": True,
        "trace_manifest_id": trace_manifest_id,
        "event": {
            "timestamp": timestamp,
            "kind": "compaction_summary",
            "message": (
                f"Compacted {len(removed_rows)} hot timeline rows; full detail remains in cold trace."
                if trace_manifest_id
                else f"Compacted {len(removed_rows)} hot timeline rows."
            ),
            "data": {
                "original_hot_rows": len(original_rows),
                "retained_hot_rows": len(keep_rows),
                "removed_hot_rows": len(removed_rows),
                "trace_manifest_id": trace_manifest_id,
                "family_counts": dict(sorted(family_counts.items())),
            },
        },
    }
    return {
        "event_id": f"turn-timeline:{execution_id}{_COMPACTION_SUMMARY_SUFFIX}",
        "event_type": "run_notice",
        "target_kind": (
            "thread_target"
            if _optional_text(execution_row["thread_target_id"]) is not None
            else None
        ),
        "target_id": _optional_text(execution_row["thread_target_id"]),
        "repo_id": _optional_text(execution_row["repo_id"]),
        "resource_kind": _optional_text(execution_row["resource_kind"]),
        "resource_id": _optional_text(execution_row["resource_id"]),
        "run_id": None,
        "timestamp": timestamp,
        "status": "recorded",
        "payload": payload,
    }


def _is_compaction_summary_row(row: Mapping[str, Any]) -> bool:
    event_id = str(row.get("event_id") or "")
    if event_id.endswith(_COMPACTION_SUMMARY_SUFFIX):
        return True
    payload = row.get("payload")
    if isinstance(payload, Mapping):
        event = payload.get("event")
        if isinstance(event, Mapping):
            return str(event.get("kind") or "").strip() == "compaction_summary"
    return False


def _manifest_artifact_path(hub_root: Path, manifest: ExecutionTraceManifest) -> Path:
    return resolve_hub_traces_root(hub_root) / manifest.artifact_relpath


def _remove_empty_trace_dirs(path: Path, root: Path) -> None:
    current = path
    while current != root and current.is_dir():
        try:
            next(current.iterdir())
            return
        except StopIteration:
            try:
                current.rmdir()
            except OSError:
                return
        current = current.parent


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _iso_cutoff(retention_days: int) -> Optional[str]:
    if retention_days <= 0:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(retention_days))
    return cutoff.isoformat().replace("+00:00", "Z")


__all__ = [
    "ExecutionHistoryAuditSummary",
    "ExecutionHistoryCompactionSummary",
    "ExecutionHistoryDatabaseHealth",
    "ExecutionHistoryExportSummary",
    "ExecutionHistoryHousekeepingSummary",
    "ExecutionHistoryMaintenancePolicy",
    "ExecutionHistoryMigrationSummary",
    "ExecutionHistoryRetentionSummary",
    "ExecutionHistoryVacuumSummary",
    "audit_execution_history",
    "backfill_legacy_execution_history",
    "collect_execution_history_database_health",
    "compact_completed_execution_history",
    "execution_history_database_size_bytes",
    "export_execution_history_bundle",
    "prune_execution_history_retention",
    "resolve_execution_history_maintenance_policy",
    "run_execution_history_housekeeping_once",
    "vacuum_execution_history",
]
