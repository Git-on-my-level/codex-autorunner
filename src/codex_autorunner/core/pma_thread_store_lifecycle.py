from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .freshness import parse_iso_datetime
from .managed_thread_status import ManagedThreadStatusReason
from .pma_thread_store_rows import PmaPendingQueueItem
from .text_utils import _json_dumps, _json_loads_object
from .time_utils import now_iso

_STALE_RUNNING_RECOVERY_ERROR = "stale_running_execution_recovered"


def thread_queue_lane_id(managed_thread_id: str) -> str:
    return f"thread:{managed_thread_id}"


class PmaThreadStoreLifecycle:
    def __init__(
        self,
        *,
        stale_running_threshold_seconds: int,
        execution_row_to_record: Callable[[Any], dict[str, Any]],
        transition_thread_status: Callable[..., Optional[dict[str, Any]]],
    ) -> None:
        self._stale_running_threshold_seconds = stale_running_threshold_seconds
        self._execution_row_to_record = execution_row_to_record
        self._transition_thread_status = transition_thread_status

    def find_stale_running_turn_ids(
        self,
        conn: Any,
        managed_thread_id: str,
        *,
        include_status_turn_age_recovery: bool = True,
    ) -> list[str]:
        running_rows = conn.execute(
            """
            SELECT
                execution_id,
                started_at,
                created_at,
                (
                    SELECT MAX(ep.timestamp)
                      FROM orch_event_projections AS ep
                     WHERE ep.execution_id = orch_thread_executions.execution_id
                ) AS last_event_at
              FROM orch_thread_executions
             WHERE thread_target_id = ?
               AND status = 'running'
             ORDER BY created_at ASC, execution_id ASC
            """,
            (managed_thread_id,),
        ).fetchall()
        if not running_rows:
            return []

        thread_row = conn.execute(
            """
            SELECT runtime_status, status_turn_id
              FROM orch_thread_targets
             WHERE thread_target_id = ?
            """,
            (managed_thread_id,),
        ).fetchone()
        runtime_status = (
            str(thread_row["runtime_status"]).strip().lower()
            if thread_row is not None and thread_row["runtime_status"] is not None
            else ""
        )
        status_turn_id = (
            str(thread_row["status_turn_id"]).strip()
            if thread_row is not None and thread_row["status_turn_id"] is not None
            else ""
        )

        if runtime_status in {"completed", "failed", "interrupted"}:
            return [str(row["execution_id"]) for row in running_rows]

        stale_execution_ids: list[str] = []
        now_dt = datetime.now(timezone.utc)
        for row in running_rows:
            execution_id = str(row["execution_id"])
            if status_turn_id and status_turn_id != execution_id:
                stale_execution_ids.append(execution_id)
                continue
            if not include_status_turn_age_recovery and status_turn_id == execution_id:
                continue

            last_activity_at = (
                parse_iso_datetime(row["last_event_at"])
                or parse_iso_datetime(row["started_at"])
                or parse_iso_datetime(row["created_at"])
            )
            if last_activity_at is None:
                continue
            age_seconds = max(0, int((now_dt - last_activity_at).total_seconds()))
            if age_seconds > self._stale_running_threshold_seconds:
                stale_execution_ids.append(execution_id)
        return stale_execution_ids

    def recover_stale_running_turns(
        self,
        conn: Any,
        managed_thread_id: str,
        *,
        include_status_turn_age_recovery: bool = True,
    ) -> int:
        stale_execution_ids = self.find_stale_running_turn_ids(
            conn,
            managed_thread_id,
            include_status_turn_age_recovery=include_status_turn_age_recovery,
        )
        if not stale_execution_ids:
            return 0
        recovered_at = now_iso()
        placeholders = ",".join("?" for _ in stale_execution_ids)
        with conn:
            conn.execute(
                f"""
                UPDATE orch_thread_executions
                   SET status = 'interrupted',
                       error_text = COALESCE(error_text, ?),
                       finished_at = ?
                 WHERE execution_id IN ({placeholders})
                   AND status = 'running'
                """,
                (
                    _STALE_RUNNING_RECOVERY_ERROR,
                    recovered_at,
                    *stale_execution_ids,
                ),
            )
            conn.execute(
                f"""
                UPDATE orch_queue_items
                   SET state = 'failed',
                       completed_at = ?,
                       updated_at = ?,
                       error_text = COALESCE(error_text, ?),
                       result_json = ?
                 WHERE source_kind = 'thread_execution'
                   AND source_key IN ({placeholders})
                   AND state = 'running'
                """,
                (
                    recovered_at,
                    recovered_at,
                    _STALE_RUNNING_RECOVERY_ERROR,
                    _json_dumps({"status": "interrupted"}),
                    *stale_execution_ids,
                ),
            )
        return len(stale_execution_ids)

    def list_pending_turn_queue_items(
        self, conn: Any, managed_thread_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = conn.execute(
            """
            SELECT
                q.queue_item_id,
                q.state,
                q.visible_at,
                q.created_at,
                e.execution_id,
                e.request_kind,
                e.prompt_text,
                e.model_id,
                e.reasoning_level,
                e.client_request_id
              FROM orch_queue_items AS q
              JOIN orch_thread_executions AS e
                ON e.execution_id = q.source_key
             WHERE q.source_kind = 'thread_execution'
               AND q.lane_id = ?
               AND e.thread_target_id = ?
               AND q.state IN ('pending', 'queued', 'waiting')
             ORDER BY COALESCE(q.visible_at, q.created_at) ASC, q.rowid ASC
             LIMIT ?
            """,
            (
                thread_queue_lane_id(managed_thread_id),
                managed_thread_id,
                limit,
            ),
        ).fetchall()
        return [PmaPendingQueueItem.from_queue_row(row).to_dict() for row in rows]

    def cancel_queued_turns(self, conn: Any, managed_thread_id: str) -> int:
        cancelled_at = now_iso()
        rows = conn.execute(
            """
            SELECT e.execution_id
              FROM orch_queue_items AS q
              JOIN orch_thread_executions AS e
                ON e.execution_id = q.source_key
             WHERE q.source_kind = 'thread_execution'
               AND q.lane_id = ?
               AND e.thread_target_id = ?
               AND e.status = 'queued'
               AND q.state IN ('pending', 'queued', 'waiting')
             ORDER BY COALESCE(q.visible_at, q.created_at) ASC, q.rowid ASC
            """,
            (
                thread_queue_lane_id(managed_thread_id),
                managed_thread_id,
            ),
        ).fetchall()
        execution_ids = [str(row["execution_id"]) for row in rows]
        if not execution_ids:
            return 0
        placeholders = ",".join("?" for _ in execution_ids)
        with conn:
            conn.execute(
                f"""
                UPDATE orch_thread_executions
                   SET status = 'interrupted',
                       error_text = COALESCE(error_text, 'interrupted'),
                       finished_at = ?
                 WHERE execution_id IN ({placeholders})
                   AND status = 'queued'
                """,
                (cancelled_at, *execution_ids),
            )
            conn.execute(
                f"""
                UPDATE orch_queue_items
                   SET state = 'failed',
                       completed_at = ?,
                       updated_at = ?,
                       error_text = COALESCE(error_text, 'interrupted'),
                       result_json = ?
                 WHERE source_kind = 'thread_execution'
                   AND lane_id = ?
                   AND source_key IN ({placeholders})
                   AND state IN ('pending', 'queued', 'waiting')
                """,
                (
                    cancelled_at,
                    cancelled_at,
                    _json_dumps({"status": "interrupted"}),
                    thread_queue_lane_id(managed_thread_id),
                    *execution_ids,
                ),
            )
        return len(execution_ids)

    def claim_next_queued_turn(
        self, conn: Any, managed_thread_id: str
    ) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
        claimed_at = now_iso()
        self.recover_stale_running_turns(conn, managed_thread_id)
        running = conn.execute(
            """
            SELECT 1
              FROM orch_thread_executions
             WHERE thread_target_id = ?
               AND status = 'running'
             LIMIT 1
            """,
            (managed_thread_id,),
        ).fetchone()
        if running is not None:
            return None
        row = conn.execute(
            """
            SELECT
                q.queue_item_id,
                q.payload_json,
                e.execution_id
              FROM orch_queue_items AS q
              JOIN orch_thread_executions AS e
                ON e.execution_id = q.source_key
             WHERE q.source_kind = 'thread_execution'
               AND q.lane_id = ?
               AND e.thread_target_id = ?
               AND e.status = 'queued'
               AND q.state IN ('pending', 'queued', 'waiting')
             ORDER BY COALESCE(q.visible_at, q.created_at) ASC, q.rowid ASC
             LIMIT 1
            """,
            (
                thread_queue_lane_id(managed_thread_id),
                managed_thread_id,
            ),
        ).fetchone()
        if row is None:
            return None
        with conn:
            cursor = conn.execute(
                """
                UPDATE orch_queue_items
                   SET state = 'running',
                       claimed_at = ?,
                       updated_at = ?
                 WHERE queue_item_id = ?
                   AND state IN ('pending', 'queued', 'waiting')
                """,
                (claimed_at, claimed_at, str(row["queue_item_id"])),
            )
            if cursor.rowcount == 0:
                return None
            conn.execute(
                """
                UPDATE orch_thread_executions
                   SET status = 'running',
                       started_at = ?
                 WHERE execution_id = ?
                   AND status = 'queued'
                """,
                (claimed_at, str(row["execution_id"])),
            )
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET last_execution_id = ?,
                       updated_at = ?
                 WHERE thread_target_id = ?
                """,
                (str(row["execution_id"]), claimed_at, managed_thread_id),
            )
        self._transition_thread_status(
            conn,
            managed_thread_id,
            reason=ManagedThreadStatusReason.TURN_STARTED,
            changed_at=claimed_at,
            turn_id=str(row["execution_id"]),
        )
        execution_row = conn.execute(
            """
            SELECT *
              FROM orch_thread_executions
             WHERE execution_id = ?
            """,
            (str(row["execution_id"]),),
        ).fetchone()
        if execution_row is None:
            return None
        return (
            self._execution_row_to_record(execution_row),
            _json_loads_object(row["payload_json"]),
        )


__all__ = [
    "PmaThreadStoreLifecycle",
    "_STALE_RUNNING_RECOVERY_ERROR",
    "thread_queue_lane_id",
]
