from __future__ import annotations

import json
from typing import Any, Optional, Sequence, cast

TIMELINE_EVENT_FAMILY = "turn.timeline"
COMPACTION_SUMMARY_SUFFIX = ":compaction-summary"


def select_execution_rows(
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


def load_timeline_rows(conn: Any, execution_id: str) -> list[dict[str, Any]]:
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
        (TIMELINE_EVENT_FAMILY, execution_id),
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


def count_baseline_timeline_rows(conn: Any, execution_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
          FROM orch_event_projections
         INDEXED BY idx_orch_event_projections_family_execution_order
         WHERE event_family = ?
           AND execution_id = ?
           AND event_id NOT LIKE ?
        """,
        (TIMELINE_EVENT_FAMILY, execution_id, f"%{COMPACTION_SUMMARY_SUFFIX}"),
    ).fetchone()
    if row is None:
        return 0
    return int(row["cnt"] or 0)


def count_all_timeline_rows_for_execution(conn: Any, execution_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
          FROM orch_event_projections
         WHERE event_family = ?
           AND execution_id = ?
        """,
        (TIMELINE_EVENT_FAMILY, execution_id),
    ).fetchone()
    if row is None:
        return 0
    return int(row["cnt"] or 0)


def timeline_row_counts_by_execution(conn: Any) -> dict[str, int]:
    return {
        str(row["execution_id"]): int(row["cnt"] or 0)
        for row in conn.execute(
            """
            SELECT execution_id, COUNT(*) AS cnt
              FROM orch_event_projections
             WHERE event_family = ?
               AND execution_id IS NOT NULL
             GROUP BY execution_id
            """,
            (TIMELINE_EVENT_FAMILY,),
        ).fetchall()
        if row["execution_id"] is not None
    }


def checkpoint_exists_for_execution(conn: Any, execution_id: str) -> bool:
    return (
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


def delete_timeline_rows_for_execution(conn: Any, execution_id: str) -> None:
    conn.execute(
        """
        DELETE FROM orch_event_projections
         WHERE event_family = ?
           AND execution_id = ?
        """,
        (TIMELINE_EVENT_FAMILY, execution_id),
    )


def delete_checkpoint_for_execution(conn: Any, execution_id: str) -> None:
    conn.execute(
        """
        DELETE FROM orch_execution_checkpoints
         WHERE execution_id = ?
        """,
        (execution_id,),
    )


def _decode_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "COMPACTION_SUMMARY_SUFFIX",
    "TIMELINE_EVENT_FAMILY",
    "checkpoint_exists_for_execution",
    "count_all_timeline_rows_for_execution",
    "count_baseline_timeline_rows",
    "delete_checkpoint_for_execution",
    "delete_timeline_rows_for_execution",
    "load_timeline_rows",
    "select_execution_rows",
    "timeline_row_counts_by_execution",
]
