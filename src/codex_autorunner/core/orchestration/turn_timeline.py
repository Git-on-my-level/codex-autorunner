from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunEvent,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from ..text_utils import _json_dumps
from .cold_trace_store import ColdTraceWriter
from .execution_history import build_hot_projection_envelope, route_run_event
from .sqlite import open_orchestration_sqlite

_EVENT_FAMILY = "turn.timeline"


def iso_from_epoch_millis(epoch_millis: Any) -> Optional[str]:
    if not isinstance(epoch_millis, (int, float)):
        return None
    return (
        datetime.fromtimestamp(
            float(epoch_millis) / 1000.0,
            tz=timezone.utc,
        )
        .isoformat()
        .replace("+00:00", "Z")
    )


def _event_type_and_status(event: RunEvent) -> tuple[str, str]:
    if isinstance(event, Started):
        return "turn_started", "recorded"
    if isinstance(event, OutputDelta):
        return "output_delta", "recorded"
    if isinstance(event, ToolCall):
        return "tool_call", "recorded"
    if isinstance(event, ToolResult):
        return "tool_result", event.status or "recorded"
    if isinstance(event, ApprovalRequested):
        return "approval_requested", "recorded"
    if isinstance(event, TokenUsage):
        return "token_usage", "recorded"
    if isinstance(event, RunNotice):
        return "run_notice", "recorded"
    if isinstance(event, Completed):
        return "turn_completed", "ok"
    if isinstance(event, Failed):
        return "turn_failed", "error"
    return "unknown", "recorded"


def persist_turn_timeline(
    hub_root,
    *,
    execution_id: str,
    target_kind: Optional[str],
    target_id: Optional[str],
    repo_id: Optional[str] = None,
    run_id: Optional[str] = None,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    events: Iterable[RunEvent],
    start_index: int = 1,
    cold_trace_writer: Optional[ColdTraceWriter] = None,
) -> int:
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_execution_id:
        return 0

    base_metadata = dict(metadata or {})
    if cold_trace_writer is not None:
        base_metadata["trace_manifest_id"] = cold_trace_writer.trace_id
    count = 0
    next_index = max(int(start_index or 1), 1)
    with open_orchestration_sqlite(hub_root) as conn:
        for event in events:
            index = next_index + count
            event_type, status = _event_type_and_status(event)
            routing = route_run_event(event)

            if cold_trace_writer is not None and routing.capture_cold_trace:
                try:
                    cold_trace_writer.append(
                        event_family=routing.event_family,
                        event_type=event_type,
                        payload=asdict(event),
                    )
                except Exception:
                    pass

            if not routing.persist_hot_projection:
                continue
            event_payload = build_hot_projection_envelope(
                event_index=index,
                event_type=event_type,
                event=event,
                metadata=base_metadata,
                routing=routing,
            ).to_payload()
            event_id = f"turn-timeline:{normalized_execution_id}:{index:04d}"
            event_timestamp = str(getattr(event, "timestamp", "") or "").strip()
            if not event_timestamp:
                continue
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
                    event_id,
                    _EVENT_FAMILY,
                    event_type,
                    target_kind,
                    target_id,
                    normalized_execution_id,
                    repo_id,
                    resource_kind,
                    resource_id,
                    run_id,
                    event_timestamp,
                    status,
                    _json_dumps(event_payload),
                    1,
                ),
            )
            count += 1
    return count


def list_turn_timeline(hub_root, *, execution_id: str) -> list[dict[str, Any]]:
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_execution_id:
        return []
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            """
            SELECT event_id, event_type, timestamp, status, payload_json
              FROM orch_event_projections
             WHERE event_family = ?
               AND execution_id = ?
             ORDER BY timestamp ASC, event_id ASC
            """,
            (_EVENT_FAMILY, normalized_execution_id),
        ).fetchall()
    entries: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (ValueError, TypeError):
            payload = {}
        data = dict(payload) if isinstance(payload, dict) else {}
        data.setdefault("event_id", str(row["event_id"] or ""))
        data.setdefault("event_type", str(row["event_type"] or ""))
        data.setdefault("timestamp", str(row["timestamp"] or ""))
        data.setdefault("status", str(row["status"] or ""))
        entries.append(data)
    return entries


__all__ = [
    "iso_from_epoch_millis",
    "list_turn_timeline",
    "persist_turn_timeline",
]
