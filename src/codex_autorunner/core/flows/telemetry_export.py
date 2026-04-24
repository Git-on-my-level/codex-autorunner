from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..state_lifecycle import DEFAULT_STATE_LIFECYCLE_CONTROLLER
from .models import FlowRunRecord
from .store import FlowStore

_logger = logging.getLogger(__name__)


@dataclass
class ExportRecord:
    run_id: str
    run_status: str
    archive_path: Optional[str] = None
    exported_events: int = 0
    exported_bytes: int = 0
    prunable_app_server_events: int = 0
    prunable_stream_deltas: int = 0
    retained_events: int = 0
    skipped: bool = False
    skip_reason: Optional[str] = None
    lifecycle_summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExportResult:
    records: List[ExportRecord] = field(default_factory=list)
    archive_files: List[str] = field(default_factory=list)
    total_exported_events: int = 0
    total_pruned_events: int = 0
    total_exported_bytes: int = 0
    errors: List[str] = field(default_factory=list)

    def dry_run_summary(self) -> Dict[str, Any]:
        return {
            "runs_total": sum(1 for r in self.records if not r.skipped),
            "runs_skipped": sum(1 for r in self.records if r.skipped),
            "events_to_export": self.total_exported_events,
            "events_to_prune": self.total_pruned_events,
            "events_to_retain": sum(r.retained_events for r in self.records),
            "estimated_bytes": self.total_exported_bytes,
            "run_details": [
                {
                    "run_id": r.run_id,
                    "run_status": r.run_status,
                    "skipped": r.skipped,
                    "skip_reason": r.skip_reason,
                    "exported_events": r.exported_events,
                    "prunable_app_server_events": r.prunable_app_server_events,
                    "prunable_stream_deltas": r.prunable_stream_deltas,
                    "retained_events": r.retained_events,
                    "lifecycle_summary": r.lifecycle_summary,
                }
                for r in self.records
            ],
        }


def classify_events_for_run(
    store: FlowStore,
    run_id: str,
    *,
    is_terminal: bool,
) -> tuple[list[dict], list[int], list[int], list[int], list[int]]:
    """Classify flow_events and flow_telemetry for a single run.

    Returns:
        events_to_export: all wire events for archival
        events_app_server_seqs_to_prune: app_server seqs to prune from flow_events
        telemetry_app_server_seqs_to_prune: app_server seqs to prune from flow_telemetry
        delta_seqs_to_prune: agent_stream_delta seqs to prune from flow_events
        retained_seqs: seqs to keep (from both tables)
    """
    plan = DEFAULT_STATE_LIFECYCLE_CONTROLLER.classify_run_telemetry(
        store,
        run_id,
        is_terminal=is_terminal,
    )
    return (
        list(plan.exported_events),
        list(plan.flow_event_app_prune_seqs),
        list(plan.flow_telemetry_app_prune_seqs),
        list(plan.delta_prune_seqs),
        list(plan.retained_seqs),
    )


def _archive_path_for_run(
    repo_root: Path, run_id: str, timestamp: Optional[str] = None
) -> Path:
    flows_dir = repo_root / ".codex-autorunner" / "flows" / run_id
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return flows_dir / f"archive-{ts}.jsonl.gz"


def _write_jsonl_gz(events: Sequence[dict], path: Path) -> int:
    """Write events as JSONL gzip, return bytes written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    with gzip.open(path, "wb") as f:
        for event in events:
            line = json.dumps(event, ensure_ascii=False) + "\n"
            encoded = line.encode("utf-8")
            f.write(encoded)
            total_bytes += len(encoded)
    return total_bytes


def _prune_events(store: FlowStore, seqs: Sequence[int]) -> int:
    return store.delete_events_by_seqs(list(seqs))


def _prune_telemetry(store: FlowStore, seqs: Sequence[int]) -> int:
    if not seqs:
        return 0
    return store.delete_telemetry_by_seqs(list(seqs))


def export_run(
    repo_root: Path,
    store: FlowStore,
    record: FlowRunRecord,
    *,
    dry_run: bool = False,
) -> ExportRecord:
    """Export wire telemetry for a single non-active run.

    Terminal runs export and prune redundant rows. Non-terminal inactive runs
    export only, preserving their live database rows.
    """
    is_terminal = record.status.is_terminal()
    if record.status.is_active():
        return ExportRecord(
            run_id=record.id,
            run_status=record.status.value,
            skipped=True,
            skip_reason=f"run is active ({record.status.value})",
            lifecycle_summary={
                "total": 1,
                "actions": {"keep": 1},
                "reasons": {"active_run_guard": 1},
                "families": {
                    "run_wire_telemetry": {
                        "total": 1,
                        "actions": {"keep": 1},
                        "reasons": {"active_run_guard": 1},
                    }
                },
            },
        )

    telemetry_plan = DEFAULT_STATE_LIFECYCLE_CONTROLLER.classify_run_telemetry(
        store,
        record.id,
        is_terminal=is_terminal,
    )
    events = list(telemetry_plan.exported_events)
    ev_app_seqs = list(telemetry_plan.flow_event_app_prune_seqs)
    tel_app_seqs = list(telemetry_plan.flow_telemetry_app_prune_seqs)
    prune_delta_seqs = list(telemetry_plan.delta_prune_seqs)
    retained_seqs = list(telemetry_plan.retained_seqs)
    lifecycle_summary = DEFAULT_STATE_LIFECYCLE_CONTROLLER.summarize_decisions(
        telemetry_plan.lifecycle_decisions
    )

    if not events:
        return ExportRecord(
            run_id=record.id,
            run_status=record.status.value,
            skipped=True,
            skip_reason="no wire telemetry events found",
            retained_events=len(retained_seqs),
            lifecycle_summary=lifecycle_summary,
        )

    archive_path = _archive_path_for_run(repo_root, record.id)

    if dry_run:
        estimated_bytes = sum(
            len(json.dumps(ev, ensure_ascii=False).encode("utf-8")) for ev in events
        )
        return ExportRecord(
            run_id=record.id,
            run_status=record.status.value,
            archive_path=str(archive_path),
            exported_events=len(events),
            exported_bytes=estimated_bytes,
            prunable_app_server_events=len(ev_app_seqs) + len(tel_app_seqs),
            prunable_stream_deltas=len(prune_delta_seqs),
            retained_events=len(retained_seqs),
            lifecycle_summary=lifecycle_summary,
        )

    bytes_written = _write_jsonl_gz(events, archive_path)
    _logger.info(
        "Exported %d events (%d bytes) for run %s to %s",
        len(events),
        bytes_written,
        record.id,
        archive_path,
    )

    pruned = 0
    with store.transaction():
        pruned += _prune_events(store, ev_app_seqs)
        pruned += _prune_telemetry(store, tel_app_seqs)
        pruned += _prune_events(store, prune_delta_seqs)

    _logger.info(
        "Pruned %d redundant events for run %s (events_app=%d, telemetry_app=%d, deltas=%d)",
        pruned,
        record.id,
        len(ev_app_seqs),
        len(tel_app_seqs),
        len(prune_delta_seqs),
    )

    return ExportRecord(
        run_id=record.id,
        run_status=record.status.value,
        archive_path=str(archive_path),
        exported_events=len(events),
        exported_bytes=bytes_written,
        prunable_app_server_events=len(ev_app_seqs) + len(tel_app_seqs),
        prunable_stream_deltas=len(prune_delta_seqs),
        retained_events=len(retained_seqs),
        lifecycle_summary=lifecycle_summary,
    )


def export_all_runs(
    repo_root: Path,
    store: FlowStore,
    *,
    dry_run: bool = False,
    run_ids: Optional[Sequence[str]] = None,
) -> ExportResult:
    """Export wire telemetry for terminal runs (or specific runs if provided).

    When listing all runs (no ``run_ids``), paused runs are skipped so periodic
    export sweeps do not repeatedly archive in-progress paused work.
    """
    result = ExportResult()

    if run_ids:
        records = []
        for rid in run_ids:
            r = store.get_flow_run(rid)
            if r is not None:
                records.append(r)
            else:
                result.errors.append(f"run {rid} not found")
    else:
        records = store.list_flow_runs()

    for record in records:
        if run_ids is None and record.status.is_paused():
            result.records.append(
                ExportRecord(
                    run_id=record.id,
                    run_status=record.status.value,
                    skipped=True,
                    skip_reason="run is paused",
                )
            )
            continue
        try:
            export_rec = export_run(repo_root, store, record, dry_run=dry_run)
        except Exception as exc:
            _logger.warning("Failed to export run %s: %s", record.id, exc)
            result.errors.append(f"run {record.id}: {exc}")
            result.records.append(
                ExportRecord(
                    run_id=record.id,
                    run_status=record.status.value,
                    skipped=True,
                    skip_reason=str(exc),
                )
            )
            continue

        result.records.append(export_rec)
        if not export_rec.skipped:
            result.total_exported_events += export_rec.exported_events
            result.total_exported_bytes += export_rec.exported_bytes
            result.total_pruned_events += (
                export_rec.prunable_app_server_events
                + export_rec.prunable_stream_deltas
            )
            if export_rec.archive_path:
                result.archive_files.append(export_rec.archive_path)

    return result


__all__ = [
    "ExportRecord",
    "ExportResult",
    "classify_events_for_run",
    "export_all_runs",
    "export_run",
]
