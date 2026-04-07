from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .models import FlowEventType, FlowRunRecord
from .store import FlowStore

_logger = logging.getLogger(__name__)

_WIRE_EVENT_TYPES = frozenset(
    {FlowEventType.APP_SERVER_EVENT, FlowEventType.AGENT_STREAM_DELTA}
)

_RETAINED_RECORD_METHODS = frozenset(
    {
        "item/completed",
        "message.updated",
        "message.completed",
        "message.part.updated",
    }
)

_HIGH_SIGNAL_ITEM_TYPES = frozenset(
    {"agentMessage", "commandExecution", "fileSearch", "functionCall", "reasoning"}
)


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


@dataclass
class ExportPlan:
    runs_total: int = 0
    runs_terminal: int = 0
    runs_active: int = 0
    runs_skipped: int = 0
    events_to_export: int = 0
    events_to_prune: int = 0
    events_to_retain: int = 0
    estimated_archive_bytes: int = 0


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
                }
                for r in self.records
            ],
        }


def _is_wire_telemetry_event(event_type: str) -> bool:
    try:
        return FlowEventType(event_type) in _WIRE_EVENT_TYPES
    except ValueError:
        return False


def _is_retained_app_server_event(data: Dict[str, Any]) -> bool:
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        return False
    method = str(message.get("method") or "").strip()
    if method in _RETAINED_RECORD_METHODS:
        return True
    return False


def _extract_item_type(data: Dict[str, Any]) -> Optional[str]:
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    return item.get("type")


def _is_high_signal_app_server_event(data: Dict[str, Any]) -> bool:
    if _is_retained_app_server_event(data):
        item_type = _extract_item_type(data)
        if item_type and item_type in _HIGH_SIGNAL_ITEM_TYPES:
            return True
    role = data.get("role")
    if isinstance(role, str) and role.strip() == "user":
        return True
    return False


def classify_events_for_run(
    store: FlowStore,
    run_id: str,
    *,
    is_terminal: bool,
) -> tuple[list[dict], list[int], list[int], list[int]]:
    """Classify flow_events for a single run.

    Returns (events_to_export, app_server_seqs_to_prune, delta_seqs_to_prune, retained_seqs).
    For active runs, nothing is prunable.
    For terminal runs, app_server_events that are not high-signal are pruned;
    agent_stream_delta rows are all pruned since the archive captures the
    full wire telemetry and makes individual deltas redundant.
    """
    conn = store._get_conn()
    rows = conn.execute(
        """
        SELECT seq, id, run_id, event_type, timestamp, data, step_id
        FROM flow_events
        WHERE run_id = ? AND event_type IN (?, ?)
        ORDER BY seq ASC
        """,
        (
            run_id,
            FlowEventType.APP_SERVER_EVENT.value,
            FlowEventType.AGENT_STREAM_DELTA.value,
        ),
    ).fetchall()

    events_to_export: list[dict] = []
    app_server_seqs_to_prune: list[int] = []
    delta_seqs_to_prune: list[int] = []
    retained_seqs: list[int] = []

    for row in rows:
        seq = row["seq"]
        event_type = row["event_type"]
        raw_data = row["data"]
        try:
            data = (
                json.loads(raw_data) if isinstance(raw_data, str) else (raw_data or {})
            )
        except (json.JSONDecodeError, TypeError):
            data = {}

        event_record = {
            "seq": seq,
            "id": row["id"],
            "run_id": row["run_id"],
            "event_type": event_type,
            "timestamp": row["timestamp"],
            "data": data,
            "step_id": row["step_id"],
        }
        events_to_export.append(event_record)

        if not is_terminal:
            retained_seqs.append(seq)
            continue

        if event_type == FlowEventType.APP_SERVER_EVENT.value:
            if _is_high_signal_app_server_event(data):
                retained_seqs.append(seq)
            else:
                app_server_seqs_to_prune.append(seq)

        elif event_type == FlowEventType.AGENT_STREAM_DELTA.value:
            delta_seqs_to_prune.append(seq)

    return (
        events_to_export,
        app_server_seqs_to_prune,
        delta_seqs_to_prune,
        retained_seqs,
    )


def plan_export(store: FlowStore) -> ExportPlan:
    """Build an export plan without mutating anything."""
    plan = ExportPlan()
    records = store.list_flow_runs()
    plan.runs_total = len(records)

    for record in records:
        if record.status.is_terminal():
            plan.runs_terminal += 1
        else:
            plan.runs_active += 1
            continue

        events, prune_app, prune_delta, retained = classify_events_for_run(
            store, record.id, is_terminal=True
        )
        plan.events_to_export += len(events)
        plan.events_to_prune += len(prune_app) + len(prune_delta)
        plan.events_to_retain += len(retained)
        for ev in events:
            plan.estimated_archive_bytes += len(
                json.dumps(ev, ensure_ascii=False).encode("utf-8")
            )

    return plan


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
    if not seqs:
        return 0
    conn = store._get_conn()
    placeholders = ",".join("?" for _ in seqs)
    cursor = conn.execute(
        f"DELETE FROM flow_events WHERE seq IN ({placeholders})",
        list(seqs),
    )
    return cursor.rowcount


def export_run(
    repo_root: Path,
    store: FlowStore,
    record: FlowRunRecord,
    *,
    dry_run: bool = False,
) -> ExportRecord:
    """Export wire telemetry for a single run and optionally prune redundant rows."""
    is_terminal = record.status.is_terminal()
    if not is_terminal:
        return ExportRecord(
            run_id=record.id,
            run_status=record.status.value,
            skipped=True,
            skip_reason=f"run is active ({record.status.value})",
        )

    events, prune_app_seqs, prune_delta_seqs, retained_seqs = classify_events_for_run(
        store, record.id, is_terminal=True
    )

    if not events:
        return ExportRecord(
            run_id=record.id,
            run_status=record.status.value,
            skipped=True,
            skip_reason="no wire telemetry events found",
            retained_events=len(retained_seqs),
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
            prunable_app_server_events=len(prune_app_seqs),
            prunable_stream_deltas=len(prune_delta_seqs),
            retained_events=len(retained_seqs),
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
        pruned += _prune_events(store, prune_app_seqs)
        pruned += _prune_events(store, prune_delta_seqs)

    _logger.info(
        "Pruned %d redundant events for run %s (app_server=%d, deltas=%d)",
        pruned,
        record.id,
        len(prune_app_seqs),
        len(prune_delta_seqs),
    )

    return ExportRecord(
        run_id=record.id,
        run_status=record.status.value,
        archive_path=str(archive_path),
        exported_events=len(events),
        exported_bytes=bytes_written,
        prunable_app_server_events=len(prune_app_seqs),
        prunable_stream_deltas=len(prune_delta_seqs),
        retained_events=len(retained_seqs),
    )


def export_all_runs(
    repo_root: Path,
    store: FlowStore,
    *,
    dry_run: bool = False,
    run_ids: Optional[Sequence[str]] = None,
) -> ExportResult:
    """Export wire telemetry for all terminal runs (or specific runs if provided)."""
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
    "ExportPlan",
    "ExportRecord",
    "ExportResult",
    "classify_events_for_run",
    "export_all_runs",
    "export_run",
    "plan_export",
]
