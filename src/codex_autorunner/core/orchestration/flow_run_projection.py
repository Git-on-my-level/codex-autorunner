from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..flows.models import FlowRunRecord, FlowRunStatus
from .sqlite import open_orchestration_sqlite


def project_ticket_flow_run_records(
    hub_root: Path,
    repo_root: Path,
    repo_id: str,
    records: Iterable[FlowRunRecord],
    *,
    ticket_flow_summary: Optional[Mapping[str, Any]] = None,
    durable: bool = True,
) -> list[dict[str, Any]]:
    """Upsert flow-run read-model rows from canonical ticket-flow records."""

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = [
        _projection_row(
            repo_root=repo_root,
            repo_id=repo_id,
            record=record,
            ticket_flow_summary=(
                ticket_flow_summary
                if ticket_flow_summary
                and str(ticket_flow_summary.get("run_id") or "") == str(record.id)
                else None
            ),
            updated_at=now,
        )
        for record in records
    ]
    if not rows:
        return []
    with open_orchestration_sqlite(hub_root, durable=durable, migrate=True) as conn:
        with conn:
            conn.executemany(
                """
                INSERT INTO orch_flow_run_projections (
                    flow_run_id,
                    repo_id,
                    flow_type,
                    status,
                    summary_json,
                    started_at,
                    finished_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(flow_run_id) DO UPDATE SET
                    repo_id = excluded.repo_id,
                    flow_type = excluded.flow_type,
                    status = excluded.status,
                    summary_json = excluded.summary_json,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        row["flow_run_id"],
                        row["repo_id"],
                        row["flow_type"],
                        row["status"],
                        row["summary_json"],
                        row["started_at"],
                        row["finished_at"],
                        row["updated_at"],
                    )
                    for row in rows
                ],
            )
    return [dict(row["public"]) for row in rows]


def list_projected_flow_runs(
    hub_root: Path,
    *,
    repo_id: Optional[str] = None,
    flow_type: Optional[str] = None,
    limit: int = 100,
    durable: bool = True,
) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit or 100), 500))
    clauses = ["1 = 1"]
    params: list[Any] = []
    if repo_id:
        clauses.append("repo_id = ?")
        params.append(str(repo_id))
    if flow_type:
        clauses.append("flow_type = ?")
        params.append(str(flow_type))
    params.append(bounded)
    with open_orchestration_sqlite(hub_root, durable=durable, migrate=True) as conn:
        rows = conn.execute(
            f"""
            SELECT *
              FROM orch_flow_run_projections
             WHERE {' AND '.join(clauses)}
             ORDER BY COALESCE(started_at, updated_at) DESC,
                      updated_at DESC,
                      flow_run_id ASC
             LIMIT ?
            """,
            params,
        ).fetchall()
    return [_public_projection(row) for row in rows]


def _projection_row(
    *,
    repo_root: Path,
    repo_id: str,
    record: FlowRunRecord,
    ticket_flow_summary: Optional[Mapping[str, Any]],
    updated_at: str,
) -> dict[str, Any]:
    status = _status_value(record.status)
    summary = _summary_payload(
        repo_root=repo_root,
        repo_id=repo_id,
        record=record,
        ticket_flow_summary=ticket_flow_summary,
    )
    return {
        "flow_run_id": record.id,
        "repo_id": repo_id,
        "flow_type": record.flow_type,
        "status": status,
        "summary_json": json.dumps(summary, sort_keys=True, separators=(",", ":")),
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "updated_at": updated_at,
        "public": {
            "run_id": record.id,
            "flow_run_id": record.id,
            "repo_id": repo_id,
            "flow_type": record.flow_type,
            "status": status,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "updated_at": updated_at,
            **summary,
        },
    }


def _summary_payload(
    *,
    repo_root: Path,
    repo_id: str,
    record: FlowRunRecord,
    ticket_flow_summary: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    state = record.state if isinstance(record.state, dict) else {}
    ticket_engine = state.get("ticket_engine")
    ticket_engine = ticket_engine if isinstance(ticket_engine, dict) else {}
    current_ticket = _text(ticket_engine.get("current_ticket")) or _text(
        state.get("current_ticket")
    )
    total_turns = _int_or_none(ticket_engine.get("total_turns"))
    ticket_turns = _int_or_none(ticket_engine.get("ticket_turns"))
    total_count = _int_or_none(
        ticket_flow_summary.get("total_count") if ticket_flow_summary else None
    )
    done_count = _int_or_none(
        ticket_flow_summary.get("done_count") if ticket_flow_summary else None
    )
    progress_percent = None
    if total_count is not None and done_count is not None and total_count > 0:
        progress_percent = max(0, min(100, round((done_count / total_count) * 100)))
    status = _status_value(record.status)
    return {
        "workspace_root": str(repo_root.resolve()),
        "current_ticket": current_ticket,
        "current_step": record.current_step,
        "ticket_engine": {
            "status": _text(ticket_engine.get("status")) or status,
            "current_ticket": current_ticket,
            "ticket_turns": ticket_turns,
            "total_turns": total_turns,
            "reason": _text(ticket_engine.get("reason")),
            "reason_details": ticket_engine.get("reason_details"),
        },
        "progress": {
            "done_count": done_count,
            "total_count": total_count,
            "progress_percent": progress_percent,
        },
        "archive_ready": _is_terminal_status(record.status),
        "error_message": record.error_message,
        "metadata": dict(record.metadata or {}),
        "projection_source": "repo_flow_store",
        "projection_owner": "orchestration",
        "repo_id": repo_id,
    }


def _public_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    summary = _json_object(row.get("summary_json"))
    return {
        "run_id": str(row.get("flow_run_id") or ""),
        "flow_run_id": str(row.get("flow_run_id") or ""),
        "repo_id": row.get("repo_id"),
        "flow_type": row.get("flow_type"),
        "status": row.get("status"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "updated_at": row.get("updated_at"),
        **summary,
    }


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _status_value(status: FlowRunStatus | str) -> str:
    return status.value if isinstance(status, FlowRunStatus) else str(status or "")


def _is_terminal_status(status: FlowRunStatus | str) -> bool:
    if isinstance(status, FlowRunStatus):
        return status.is_terminal()
    return str(status or "").strip().lower() in {
        "completed",
        "failed",
        "stopped",
        "superseded",
    }


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
