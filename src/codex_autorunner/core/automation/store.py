from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

from ..orchestration.sqlite import open_orchestration_sqlite
from ..pma_domain.automation_lifecycle import cancel_schedule_state
from ..text_utils import _json_dumps, _json_loads_object
from ..time_utils import now_iso
from .models import (
    JOB_CANCELLED,
    JOB_CLAIMED,
    JOB_DEAD_LETTERED,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_SKIPPED,
    JOB_SUCCEEDED,
    AutomationChildExecutionEdge,
    AutomationEvent,
    AutomationJob,
    AutomationJobAttempt,
    AutomationRule,
    AutomationSchedule,
    normalize_bool,
    normalize_non_negative_int,
    normalize_timestamp,
    validate_job_transition,
)


def _json_object_from_row(row: sqlite3.Row, column: str) -> dict[str, Any]:
    return _json_loads_object(row[column])


class AutomationStore:
    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = Path(hub_root)
        self._durable = durable

    @property
    def hub_root(self) -> Path:
        return self._hub_root

    def upsert_rule(self, rule: AutomationRule) -> AutomationRule:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                existing = self.get_rule(rule.rule_id, conn=conn)
                conn.execute(
                    """
                    INSERT INTO orch_automation_rules (
                        rule_id, name, enabled, system_owned, trigger_kind,
                        trigger_json, filters_json, target_policy, target_json,
                        executor_kind, executor_json, policy_json, metadata_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(rule_id) DO UPDATE SET
                        name = excluded.name,
                        enabled = excluded.enabled,
                        system_owned = excluded.system_owned,
                        trigger_kind = excluded.trigger_kind,
                        trigger_json = excluded.trigger_json,
                        filters_json = excluded.filters_json,
                        target_policy = excluded.target_policy,
                        target_json = excluded.target_json,
                        executor_kind = excluded.executor_kind,
                        executor_json = excluded.executor_json,
                        policy_json = excluded.policy_json,
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at
                    """,
                    self._rule_params(rule),
                )
                if existing is not None:
                    self._record_rule_version(conn, existing)
        saved = self.get_rule(rule.rule_id)
        if saved is None:
            raise RuntimeError("failed to persist automation rule")
        return saved

    def create_rule(self, **kwargs: Any) -> AutomationRule:
        return self.upsert_rule(AutomationRule.create(**kwargs))

    def get_rule(
        self, rule_id: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[AutomationRule]:
        def query(connection: sqlite3.Connection) -> Optional[AutomationRule]:
            row = connection.execute(
                "SELECT * FROM orch_automation_rules WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()
            return self._row_to_rule(row) if row is not None else None

        if conn is not None:
            return query(conn)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as opened:
            return query(opened)

    def list_rules(
        self, *, enabled: Optional[bool] = None, trigger_kind: Optional[str] = None
    ) -> list[AutomationRule]:
        clauses: list[str] = []
        params: list[Any] = []
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(1 if enabled else 0)
        if trigger_kind is not None:
            clauses.append("trigger_kind = ?")
            params.append(trigger_kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_automation_rules
                  {where}
                 ORDER BY created_at ASC, rule_id ASC
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_rule(row) for row in rows]

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> Optional[AutomationRule]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE orch_automation_rules
                       SET enabled = ?, updated_at = ?
                     WHERE rule_id = ?
                    """,
                    (1 if enabled else 0, now_iso(), rule_id),
                )
        return self.get_rule(rule_id)

    def cancel_schedule(self, schedule_id: str) -> Optional[AutomationSchedule]:
        stamp = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                row = conn.execute(
                    "SELECT * FROM orch_automation_schedules WHERE schedule_id = ?",
                    (schedule_id,),
                ).fetchone()
                if row is None:
                    return None
                next_state, changed = cancel_schedule_state(str(row["state"]))
                if not changed:
                    return self._row_to_schedule(row)
                conn.execute(
                    """
                    UPDATE orch_automation_schedules
                       SET state = ?,
                           next_fire_at = NULL,
                           updated_at = ?
                     WHERE schedule_id = ?
                    """,
                    (next_state, stamp, schedule_id),
                )
        return self.get_schedule(schedule_id)

    def record_event(self, event: AutomationEvent) -> AutomationEvent:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_automation_events (
                        event_id, event_type, source, observed_at, repo_id,
                        target_json, payload_json, raw_payload_json, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO NOTHING
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.source,
                        event.observed_at,
                        event.repo_id,
                        _json_dumps(event.target),
                        _json_dumps(event.payload),
                        _json_dumps(event.raw_payload),
                        _json_dumps(event.metadata),
                    ),
                )
        saved = self.get_event(event.event_id)
        if saved is None:
            raise RuntimeError("failed to persist automation event")
        return saved

    def create_event(self, **kwargs: Any) -> AutomationEvent:
        return self.record_event(AutomationEvent.create(**kwargs))

    def get_event(self, event_id: str) -> Optional[AutomationEvent]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            row = conn.execute(
                "SELECT * FROM orch_automation_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def list_events(
        self, *, event_type: Optional[str] = None, limit: Optional[int] = None
    ) -> list[AutomationEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = "LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(max(0, int(limit)))
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_automation_events
                  {where}
                 ORDER BY observed_at DESC, event_id ASC
                  {limit_sql}
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def enqueue_job(self, job: AutomationJob) -> tuple[AutomationJob, bool]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                existing = conn.execute(
                    "SELECT * FROM orch_automation_jobs WHERE dedupe_key = ?",
                    (job.dedupe_key,),
                ).fetchone()
                if existing is not None:
                    return self._row_to_job(existing), True
                conn.execute(
                    """
                    INSERT INTO orch_automation_jobs (
                        job_id, rule_id, event_id, state, dedupe_key, batch_key,
                        lock_key, available_at, claimed_at, started_at, finished_at,
                        updated_at, attempt_count, max_attempts, next_attempt_at,
                        retry_backoff_seconds, created_at, target_json, executor_json,
                        policy_json, payload_json, managed_thread_target_id,
                        managed_thread_execution_id, pma_lane_id, pma_queue_item_id,
                        ticket_flow_repo_id, ticket_flow_run_id,
                        ticket_flow_worktree_id, publish_operation_id, result_summary,
                        error_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._job_params(job),
                )
        saved = self.get_job(job.job_id)
        if saved is None:
            raise RuntimeError("failed to persist automation job")
        return saved, False

    def create_job(self, **kwargs: Any) -> tuple[AutomationJob, bool]:
        return self.enqueue_job(AutomationJob.create(**kwargs))

    def get_job(self, job_id: str) -> Optional[AutomationJob]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            row = conn.execute(
                "SELECT * FROM orch_automation_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def get_job_by_dedupe_key(self, dedupe_key: str) -> Optional[AutomationJob]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            row = conn.execute(
                "SELECT * FROM orch_automation_jobs WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def list_jobs(
        self,
        *,
        state: Optional[str] = None,
        rule_id: Optional[str] = None,
        limit: Optional[int] = None,
        order: Literal["available", "newest"] = "available",
    ) -> list[AutomationJob]:
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if rule_id is not None:
            clauses.append("rule_id = ?")
            params.append(rule_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = "LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(max(0, int(limit)))
        order_sql = (
            "updated_at DESC, available_at DESC, job_id DESC"
            if order == "newest"
            else "available_at ASC, updated_at ASC, job_id ASC"
        )
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_automation_jobs
                  {where}
                 ORDER BY {order_sql}
                  {limit_sql}
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def recent_jobs_by_rule(
        self, rule_ids: list[str], *, per_rule_limit: int = 25
    ) -> dict[str, list[AutomationJob]]:
        normalized_rule_ids = [
            rule_id for rule_id in dict.fromkeys(rule_ids) if rule_id
        ]
        if not normalized_rule_ids:
            return {}
        bounded_limit = max(0, min(int(per_rule_limit), 100))
        if bounded_limit == 0:
            return {rule_id: [] for rule_id in normalized_rule_ids}
        placeholders = ",".join("?" for _ in normalized_rule_ids)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM (
                    SELECT jobs.*,
                           ROW_NUMBER() OVER (
                             PARTITION BY rule_id
                             ORDER BY updated_at DESC, available_at DESC, job_id DESC
                           ) AS row_number
                      FROM orch_automation_jobs AS jobs
                     WHERE rule_id IN ({placeholders})
                  )
                 WHERE row_number <= ?
                 ORDER BY rule_id ASC, updated_at DESC, available_at DESC, job_id DESC
                """,
                (*normalized_rule_ids, bounded_limit),
            ).fetchall()
        grouped: dict[str, list[AutomationJob]] = {
            rule_id: [] for rule_id in normalized_rule_ids
        }
        for row in rows:
            grouped.setdefault(str(row["rule_id"]), []).append(self._row_to_job(row))
        return grouped

    def count_jobs(
        self, *, state: Optional[str] = None, rule_id: Optional[str] = None
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if rule_id is not None:
            clauses.append("rule_id = ?")
            params.append(rule_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM orch_automation_jobs {where}",
                tuple(params),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def job_counts_by_rule(self, rule_ids: list[str]) -> dict[str, int]:
        normalized_rule_ids = [
            rule_id for rule_id in dict.fromkeys(rule_ids) if rule_id
        ]
        if not normalized_rule_ids:
            return {}
        placeholders = ",".join("?" for _ in normalized_rule_ids)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            rows = conn.execute(
                f"""
                SELECT rule_id, COUNT(*) AS count
                  FROM orch_automation_jobs
                 WHERE rule_id IN ({placeholders})
                 GROUP BY rule_id
                """,
                tuple(normalized_rule_ids),
            ).fetchall()
        counts = {rule_id: 0 for rule_id in normalized_rule_ids}
        counts.update({str(row["rule_id"]): int(row["count"]) for row in rows})
        return counts

    def claim_next_job(
        self, *, lock_key: Optional[str] = None, now: Optional[str] = None
    ) -> Optional[AutomationJob]:
        stamp = normalize_timestamp(now)
        claimed: sqlite3.Row | None = None
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT *
                      FROM orch_automation_jobs
                     WHERE state = ? AND available_at <= ?
                     ORDER BY available_at ASC, updated_at ASC, job_id ASC
                     LIMIT 100
                    """,
                    (JOB_PENDING, stamp),
                ).fetchall()
                for row in rows:
                    candidate = self._row_to_job(row)
                    if self._job_concurrency_saturated(conn, candidate):
                        continue
                    validate_job_transition(str(row["state"]), JOB_CLAIMED)
                    cursor = conn.execute(
                        """
                        UPDATE orch_automation_jobs
                           SET state = ?,
                               lock_key = COALESCE(?, lock_key),
                               claimed_at = ?,
                               updated_at = ?
                         WHERE job_id = ? AND state = ?
                        """,
                        (
                            JOB_CLAIMED,
                            lock_key,
                            stamp,
                            stamp,
                            row["job_id"],
                            JOB_PENDING,
                        ),
                    )
                    if int(cursor.rowcount or 0) != 1:
                        continue
                    claimed = conn.execute(
                        "SELECT * FROM orch_automation_jobs WHERE job_id = ?",
                        (row["job_id"],),
                    ).fetchone()
                    break
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._row_to_job(claimed) if claimed is not None else None

    def start_job(self, job_id: str, *, now: Optional[str] = None) -> AutomationJob:
        return self._transition_job(
            job_id,
            JOB_RUNNING,
            now=now,
            increment_attempt=True,
            started=True,
        )

    def complete_job(
        self,
        job_id: str,
        *,
        result_summary: Optional[str] = None,
        execution_refs: Optional[dict[str, Any]] = None,
        now: Optional[str] = None,
    ) -> AutomationJob:
        return self._transition_job(
            job_id,
            JOB_SUCCEEDED,
            now=now,
            result_summary=result_summary,
            execution_refs=execution_refs,
            finished=True,
        )

    def update_running_job(
        self,
        job_id: str,
        *,
        result_summary: Optional[str] = None,
        execution_refs: Optional[dict[str, Any]] = None,
        now: Optional[str] = None,
    ) -> AutomationJob:
        self._transition_job(
            job_id,
            JOB_RUNNING,
            now=now,
            result_summary=result_summary,
            execution_refs=execution_refs,
        )
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE orch_automation_jobs
                       SET lock_key = NULL,
                           claimed_at = NULL,
                           updated_at = ?
                     WHERE job_id = ?
                    """,
                    (normalize_timestamp(now), job_id),
                )
        saved = self.get_job(job_id)
        if saved is None:
            raise RuntimeError("failed to update automation job")
        return saved

    def fail_job(
        self,
        job_id: str,
        *,
        error_text: str,
        dead_letter: bool = False,
        execution_refs: Optional[dict[str, Any]] = None,
        now: Optional[str] = None,
    ) -> AutomationJob:
        return self._transition_job(
            job_id,
            JOB_DEAD_LETTERED if dead_letter else JOB_FAILED,
            now=now,
            error_text=error_text,
            execution_refs=execution_refs,
            finished=True,
        )

    def retry_job(
        self,
        job_id: str,
        *,
        available_at: Optional[str] = None,
        retry_backoff_seconds: Optional[int] = None,
    ) -> AutomationJob:
        stamp = normalize_timestamp(available_at)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                row = conn.execute(
                    "SELECT * FROM orch_automation_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"Unknown automation job: {job_id}")
                validate_job_transition(str(row["state"]), JOB_PENDING)
                conn.execute(
                    """
                    UPDATE orch_automation_jobs
                       SET state = ?,
                           available_at = ?,
                           next_attempt_at = ?,
                           retry_backoff_seconds = COALESCE(?, retry_backoff_seconds),
                           lock_key = NULL,
                           claimed_at = NULL,
                           updated_at = ?
                     WHERE job_id = ?
                    """,
                    (
                        JOB_PENDING,
                        stamp,
                        stamp,
                        retry_backoff_seconds,
                        stamp,
                        job_id,
                    ),
                )
                saved = conn.execute(
                    "SELECT * FROM orch_automation_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
        if saved is None:
            raise RuntimeError("failed to retry automation job")
        return self._row_to_job(saved)

    def revive_dead_lettered_job(
        self,
        job_id: str,
        *,
        available_at: Optional[str] = None,
    ) -> AutomationJob:
        stamp = normalize_timestamp(available_at)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                row = conn.execute(
                    "SELECT * FROM orch_automation_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"Unknown automation job: {job_id}")
                if str(row["state"]) != JOB_DEAD_LETTERED:
                    raise ValueError(
                        "Only dead_lettered automation jobs can be revived"
                    )
                conn.execute(
                    """
                    UPDATE orch_automation_jobs
                       SET state = ?,
                           available_at = ?,
                           next_attempt_at = ?,
                           lock_key = NULL,
                           claimed_at = NULL,
                           finished_at = NULL,
                           error_text = NULL,
                           updated_at = ?
                     WHERE job_id = ?
                    """,
                    (JOB_PENDING, stamp, stamp, stamp, job_id),
                )
                saved = conn.execute(
                    "SELECT * FROM orch_automation_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
        if saved is None:
            raise RuntimeError("failed to revive automation job")
        return self._row_to_job(saved)

    def cancel_job(
        self,
        job_id: str,
        *,
        execution_refs: Optional[dict[str, Any]] = None,
        now: Optional[str] = None,
    ) -> AutomationJob:
        return self._transition_job(
            job_id,
            JOB_CANCELLED,
            now=now,
            execution_refs=execution_refs,
            finished=True,
        )

    def skip_job(
        self, job_id: str, *, result_summary: Optional[str] = None
    ) -> AutomationJob:
        return self._transition_job(
            job_id,
            JOB_SKIPPED,
            result_summary=result_summary,
            finished=True,
        )

    def pause_job(
        self,
        job_id: str,
        *,
        result_summary: Optional[str] = None,
        execution_refs: Optional[dict[str, Any]] = None,
        now: Optional[str] = None,
    ) -> AutomationJob:
        return self._transition_job(
            job_id,
            JOB_PAUSED,
            now=now,
            result_summary=result_summary,
            execution_refs=execution_refs,
        )

    def record_attempt(self, attempt: AutomationJobAttempt) -> AutomationJobAttempt:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_automation_job_attempts (
                        attempt_id, job_id, attempt_number, status, started_at,
                        finished_at, error_text, executor_result_json,
                        execution_refs_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(attempt_id) DO UPDATE SET
                        status = excluded.status,
                        finished_at = excluded.finished_at,
                        error_text = excluded.error_text,
                        executor_result_json = excluded.executor_result_json,
                        execution_refs_json = excluded.execution_refs_json
                    """,
                    (
                        attempt.attempt_id,
                        attempt.job_id,
                        attempt.attempt_number,
                        attempt.status,
                        attempt.started_at,
                        attempt.finished_at,
                        attempt.error_text,
                        _json_dumps(attempt.executor_result),
                        _json_dumps(attempt.execution_refs),
                        attempt.created_at,
                    ),
                )
        saved = self.get_attempt(attempt.attempt_id)
        if saved is None:
            raise RuntimeError("failed to persist automation job attempt")
        return saved

    def get_attempt(self, attempt_id: str) -> Optional[AutomationJobAttempt]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            row = conn.execute(
                "SELECT * FROM orch_automation_job_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return self._row_to_attempt(row) if row is not None else None

    def list_attempts(self, job_id: str) -> list[AutomationJobAttempt]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_automation_job_attempts
                 WHERE job_id = ?
                 ORDER BY attempt_number ASC, created_at ASC
                """,
                (job_id,),
            ).fetchall()
        return [self._row_to_attempt(row) for row in rows]

    def upsert_child_execution_edge(
        self, edge: AutomationChildExecutionEdge
    ) -> AutomationChildExecutionEdge:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_automation_child_execution_edges (
                        edge_id, parent_job_id, child_kind, child_id,
                        authoritative_for_parent_completion,
                        requested_runtime_json, actual_runtime_json,
                        terminal_mapping_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(parent_job_id, child_kind, child_id) DO UPDATE SET
                        authoritative_for_parent_completion =
                            excluded.authoritative_for_parent_completion,
                        requested_runtime_json = excluded.requested_runtime_json,
                        actual_runtime_json = excluded.actual_runtime_json,
                        terminal_mapping_json = excluded.terminal_mapping_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        edge.edge_id,
                        edge.parent_job_id,
                        edge.child_kind,
                        edge.child_id,
                        1 if edge.authoritative_for_parent_completion else 0,
                        _json_dumps(edge.requested_runtime.to_dict()),
                        (
                            _json_dumps(edge.actual_runtime.to_dict())
                            if edge.actual_runtime is not None
                            else None
                        ),
                        _json_dumps(edge.terminal_mapping),
                        edge.created_at,
                        edge.updated_at,
                    ),
                )
        saved = self.get_child_execution_edge(edge.edge_id)
        if saved is None:
            saved = self.get_child_execution_edge_by_child(
                parent_job_id=edge.parent_job_id,
                child_kind=edge.child_kind,
                child_id=edge.child_id,
            )
        if saved is None:
            raise RuntimeError("failed to persist automation child execution edge")
        return saved

    def get_child_execution_edge(
        self, edge_id: str
    ) -> Optional[AutomationChildExecutionEdge]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_automation_child_execution_edges
                 WHERE edge_id = ?
                """,
                (edge_id,),
            ).fetchone()
        return self._row_to_child_execution_edge(row) if row is not None else None

    def get_child_execution_edge_by_child(
        self, *, parent_job_id: str, child_kind: str, child_id: str
    ) -> Optional[AutomationChildExecutionEdge]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_automation_child_execution_edges
                 WHERE parent_job_id = ?
                   AND child_kind = ?
                   AND child_id = ?
                """,
                (parent_job_id, child_kind, child_id),
            ).fetchone()
        return self._row_to_child_execution_edge(row) if row is not None else None

    def list_child_execution_edges(
        self, parent_job_id: str
    ) -> list[AutomationChildExecutionEdge]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_automation_child_execution_edges
                 WHERE parent_job_id = ?
                 ORDER BY created_at ASC, edge_id ASC
                """,
                (parent_job_id,),
            ).fetchall()
        return [self._row_to_child_execution_edge(row) for row in rows]

    def upsert_schedule(self, schedule: AutomationSchedule) -> AutomationSchedule:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_automation_schedules (
                        schedule_id, rule_id, schedule_kind, timezone, next_fire_at,
                        last_fire_at, misfire_policy, schedule_json, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(schedule_id) DO UPDATE SET
                        rule_id = excluded.rule_id,
                        schedule_kind = excluded.schedule_kind,
                        timezone = excluded.timezone,
                        next_fire_at = excluded.next_fire_at,
                        last_fire_at = excluded.last_fire_at,
                        misfire_policy = excluded.misfire_policy,
                        schedule_json = excluded.schedule_json,
                        state = excluded.state,
                        updated_at = excluded.updated_at
                    """,
                    (
                        schedule.schedule_id,
                        schedule.rule_id,
                        schedule.schedule_kind,
                        schedule.timezone,
                        schedule.next_fire_at,
                        schedule.last_fire_at,
                        schedule.misfire_policy,
                        _json_dumps(schedule.schedule),
                        schedule.state,
                        schedule.created_at,
                        schedule.updated_at,
                    ),
                )
        saved = self.get_schedule(schedule.schedule_id)
        if saved is None:
            raise RuntimeError("failed to persist automation schedule")
        return saved

    def get_schedule(self, schedule_id: str) -> Optional[AutomationSchedule]:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            row = conn.execute(
                "SELECT * FROM orch_automation_schedules WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
        return self._row_to_schedule(row) if row is not None else None

    def list_schedules(
        self, *, rule_id: Optional[str] = None
    ) -> list[AutomationSchedule]:
        params: list[Any] = []
        where = ""
        if rule_id is not None:
            where = "WHERE rule_id = ?"
            params.append(rule_id)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_automation_schedules
                  {where}
                 ORDER BY next_fire_at ASC, created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    def schedules_by_rule(
        self, rule_ids: list[str]
    ) -> dict[str, list[AutomationSchedule]]:
        normalized_rule_ids = [
            rule_id for rule_id in dict.fromkeys(rule_ids) if rule_id
        ]
        if not normalized_rule_ids:
            return {}
        placeholders = ",".join("?" for _ in normalized_rule_ids)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_automation_schedules
                 WHERE rule_id IN ({placeholders})
                 ORDER BY rule_id ASC, next_fire_at ASC, created_at ASC
                """,
                tuple(normalized_rule_ids),
            ).fetchall()
        grouped: dict[str, list[AutomationSchedule]] = {
            rule_id: [] for rule_id in normalized_rule_ids
        }
        for row in rows:
            grouped.setdefault(str(row["rule_id"]), []).append(
                self._row_to_schedule(row)
            )
        return grouped

    def list_due_schedules(
        self, *, now: Optional[str] = None, limit: int = 100
    ) -> list[AutomationSchedule]:
        stamp = normalize_timestamp(now)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_automation_schedules
                 WHERE state = 'active'
                   AND next_fire_at IS NOT NULL
                   AND next_fire_at <= ?
                 ORDER BY next_fire_at ASC, schedule_id ASC
                 LIMIT ?
                """,
                (stamp, max(0, int(limit))),
            ).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    def update_schedule_fire(
        self,
        schedule_id: str,
        *,
        last_fire_at: str,
        next_fire_at: Optional[str],
        state: str = "active",
        now: Optional[str] = None,
    ) -> Optional[AutomationSchedule]:
        stamp = normalize_timestamp(now)
        normalized_last = normalize_timestamp(last_fire_at)
        normalized_next = normalize_timestamp(next_fire_at) if next_fire_at else None
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE orch_automation_schedules
                       SET last_fire_at = ?,
                           next_fire_at = ?,
                           state = ?,
                           updated_at = ?
                     WHERE schedule_id = ?
                    """,
                    (normalized_last, normalized_next, state, stamp, schedule_id),
                )
        return self.get_schedule(schedule_id)

    def release_stale_claims(
        self, *, stale_before: str, now: Optional[str] = None
    ) -> int:
        stamp = normalize_timestamp(now)
        threshold = normalize_timestamp(stale_before)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_automation_jobs
                       SET state = ?,
                           lock_key = NULL,
                           claimed_at = NULL,
                           started_at = CASE WHEN state = ? THEN NULL ELSE started_at END,
                           available_at = CASE WHEN available_at > ? THEN ? ELSE available_at END,
                           next_attempt_at = CASE WHEN next_attempt_at > ? THEN ? ELSE next_attempt_at END,
                           updated_at = ?
                     WHERE state IN (?, ?)
                       AND (
                           (claimed_at IS NOT NULL AND claimed_at <= ?)
                           OR (
                               state = ?
                               AND started_at IS NOT NULL
                               AND started_at <= ?
                               AND managed_thread_execution_id IS NULL
                               AND pma_queue_item_id IS NULL
                               AND ticket_flow_run_id IS NULL
                               AND publish_operation_id IS NULL
                           )
                       )
                    """,
                    (
                        JOB_PENDING,
                        JOB_RUNNING,
                        stamp,
                        stamp,
                        stamp,
                        stamp,
                        stamp,
                        JOB_CLAIMED,
                        JOB_RUNNING,
                        threshold,
                        JOB_RUNNING,
                        threshold,
                    ),
                )
                return int(cursor.rowcount or 0)

    def count_running_jobs(
        self,
        *,
        rule_id: Optional[str] = None,
        target: Optional[dict[str, Any]] = None,
    ) -> int:
        clauses = ["state = ?"]
        params: list[Any] = [JOB_RUNNING]
        if rule_id is not None:
            clauses.append("rule_id = ?")
            params.append(rule_id)
        if target is not None:
            clauses.append("target_json = ?")
            params.append(_json_dumps(target))
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM orch_automation_jobs WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()
        return int(row["c"] if row is not None else 0)

    def count_active_jobs(
        self,
        *,
        rule_id: Optional[str] = None,
        target: Optional[dict[str, Any]] = None,
    ) -> int:
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            return self._count_active_jobs(conn, rule_id=rule_id, target=target)

    def has_recent_job_for_rule(self, rule_id: str, *, since: str) -> bool:
        return self.count_jobs_for_rule_since(rule_id, since=since) > 0

    def count_jobs_for_rule_since(self, rule_id: str, *, since: str) -> int:
        stamp = normalize_timestamp(since)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                  FROM orch_automation_jobs
                 WHERE rule_id = ?
                   AND created_at >= ?
                   AND state IN (?, ?, ?)
                """,
                (rule_id, stamp, JOB_RUNNING, JOB_SUCCEEDED, JOB_PENDING),
            ).fetchone()
        return int(row["c"] if row is not None else 0)

    def migrate_legacy_pma_automation(self) -> dict[str, Any]:
        from ..pma_automation_unified import PmaUnifiedAutomationAdapter

        return PmaUnifiedAutomationAdapter(self).migrate_legacy_rows().to_dict()

    def backfill_legacy_pma_automation(self) -> dict[str, Any]:
        raise RuntimeError(
            "PMA legacy automation backfill was removed; run "
            "migrate_legacy_pma_automation() explicitly and handle diagnostics."
        )

    def _transition_job(
        self,
        job_id: str,
        to_state: str,
        *,
        now: Optional[str] = None,
        increment_attempt: bool = False,
        started: bool = False,
        finished: bool = False,
        result_summary: Optional[str] = None,
        error_text: Optional[str] = None,
        execution_refs: Optional[dict[str, Any]] = None,
    ) -> AutomationJob:
        stamp = normalize_timestamp(now)
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                row = conn.execute(
                    "SELECT * FROM orch_automation_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"Unknown automation job: {job_id}")
                validate_job_transition(str(row["state"]), to_state)
                refs = dict(execution_refs or {})
                updates = {
                    "managed_thread_target_id": refs.get("managed_thread_target_id")
                    or row["managed_thread_target_id"],
                    "managed_thread_execution_id": refs.get(
                        "managed_thread_execution_id"
                    )
                    or row["managed_thread_execution_id"],
                    "pma_lane_id": refs.get("pma_lane_id") or row["pma_lane_id"],
                    "pma_queue_item_id": refs.get("pma_queue_item_id")
                    or row["pma_queue_item_id"],
                    "ticket_flow_repo_id": refs.get("ticket_flow_repo_id")
                    or row["ticket_flow_repo_id"],
                    "ticket_flow_run_id": refs.get("ticket_flow_run_id")
                    or row["ticket_flow_run_id"],
                    "ticket_flow_worktree_id": refs.get("ticket_flow_worktree_id")
                    or row["ticket_flow_worktree_id"],
                    "publish_operation_id": refs.get("publish_operation_id")
                    or row["publish_operation_id"],
                }
                conn.execute(
                    """
                    UPDATE orch_automation_jobs
                       SET state = ?,
                           started_at = CASE WHEN ? THEN COALESCE(started_at, ?) ELSE started_at END,
                           finished_at = CASE WHEN ? THEN ? ELSE finished_at END,
                           updated_at = ?,
                           attempt_count = attempt_count + ?,
                           result_summary = COALESCE(?, result_summary),
                           error_text = COALESCE(?, error_text),
                           managed_thread_target_id = ?,
                           managed_thread_execution_id = ?,
                           pma_lane_id = ?,
                           pma_queue_item_id = ?,
                           ticket_flow_repo_id = ?,
                           ticket_flow_run_id = ?,
                           ticket_flow_worktree_id = ?,
                           publish_operation_id = ?
                     WHERE job_id = ?
                    """,
                    (
                        to_state,
                        1 if started else 0,
                        stamp,
                        1 if finished else 0,
                        stamp,
                        stamp,
                        1 if increment_attempt else 0,
                        result_summary,
                        error_text,
                        updates["managed_thread_target_id"],
                        updates["managed_thread_execution_id"],
                        updates["pma_lane_id"],
                        updates["pma_queue_item_id"],
                        updates["ticket_flow_repo_id"],
                        updates["ticket_flow_run_id"],
                        updates["ticket_flow_worktree_id"],
                        updates["publish_operation_id"],
                        job_id,
                    ),
                )
                saved = conn.execute(
                    "SELECT * FROM orch_automation_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
        if saved is None:
            raise RuntimeError("failed to update automation job")
        return self._row_to_job(saved)

    def _upsert_legacy_rule(self, rule: AutomationRule) -> tuple[AutomationRule, bool]:
        existed = self.get_rule(rule.rule_id) is not None
        return self.upsert_rule(rule), not existed

    def _record_rule_version(
        self, conn: sqlite3.Connection, rule: AutomationRule
    ) -> None:
        conn.execute(
            """
            INSERT INTO orch_automation_rule_versions (
                version_id, rule_id, rule_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                f"{rule.rule_id}:{uuid.uuid4()}",
                rule.rule_id,
                _json_dumps(rule.to_dict()),
                now_iso(),
            ),
        )

    def _job_concurrency_saturated(
        self, conn: sqlite3.Connection, job: AutomationJob
    ) -> bool:
        per_rule = int(job.policy.get("max_concurrent_per_rule") or 1)
        per_target = int(job.policy.get("max_concurrent_per_target") or 1)
        if (
            per_rule > 0
            and self._count_active_jobs(conn, rule_id=job.rule_id) >= per_rule
        ):
            return True
        if (
            per_target > 0
            and self._count_active_jobs(conn, target=job.target) >= per_target
        ):
            return True
        return False

    def _count_active_jobs(
        self,
        conn: sqlite3.Connection,
        *,
        rule_id: Optional[str] = None,
        target: Optional[dict[str, Any]] = None,
    ) -> int:
        clauses = ["state IN (?, ?)"]
        params: list[Any] = [JOB_CLAIMED, JOB_RUNNING]
        if rule_id is not None:
            clauses.append("rule_id = ?")
            params.append(rule_id)
        if target is not None:
            clauses.append("target_json = ?")
            params.append(_json_dumps(target))
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM orch_automation_jobs WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchone()
        return int(row["c"] if row is not None else 0)

    def _row_to_rule(self, row: sqlite3.Row) -> AutomationRule:
        return AutomationRule.create(
            rule_id=row["rule_id"],
            name=row["name"],
            enabled=normalize_bool(row["enabled"]),
            system_owned=normalize_bool(row["system_owned"]),
            trigger_kind=row["trigger_kind"],
            trigger=_json_object_from_row(row, "trigger_json"),
            filters=_json_object_from_row(row, "filters_json"),
            target_policy=row["target_policy"],
            target=_json_object_from_row(row, "target_json"),
            executor_kind=row["executor_kind"],
            executor=_json_object_from_row(row, "executor_json"),
            policy=_json_object_from_row(row, "policy_json"),
            metadata=_json_object_from_row(row, "metadata_json"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_event(self, row: sqlite3.Row) -> AutomationEvent:
        return AutomationEvent.create(
            event_id=row["event_id"],
            event_type=row["event_type"],
            observed_at=row["observed_at"],
            source=row["source"],
            repo_id=row["repo_id"],
            target=_json_object_from_row(row, "target_json"),
            payload=_json_object_from_row(row, "payload_json"),
            raw_payload=_json_object_from_row(row, "raw_payload_json"),
            metadata=_json_object_from_row(row, "metadata_json"),
        )

    def _row_to_job(self, row: sqlite3.Row) -> AutomationJob:
        job = AutomationJob.create(
            job_id=row["job_id"],
            rule_id=row["rule_id"],
            event_id=row["event_id"],
            state=row["state"],
            dedupe_key=row["dedupe_key"],
            batch_key=row["batch_key"],
            lock_key=row["lock_key"],
            available_at=row["available_at"],
            max_attempts=int(row["max_attempts"] or 3),
            target=_json_object_from_row(row, "target_json"),
            executor=_json_object_from_row(row, "executor_json"),
            policy=_json_object_from_row(row, "policy_json"),
            payload=_json_object_from_row(row, "payload_json"),
            created_at=row["created_at"],
        )
        job.claimed_at = row["claimed_at"]
        job.started_at = row["started_at"]
        job.finished_at = row["finished_at"]
        job.updated_at = row["updated_at"]
        job.attempt_count = int(row["attempt_count"] or 0)
        job.next_attempt_at = row["next_attempt_at"]
        job.retry_backoff_seconds = normalize_non_negative_int(
            row["retry_backoff_seconds"]
        )
        job.managed_thread_target_id = row["managed_thread_target_id"]
        job.managed_thread_execution_id = row["managed_thread_execution_id"]
        job.pma_lane_id = row["pma_lane_id"]
        job.pma_queue_item_id = row["pma_queue_item_id"]
        job.ticket_flow_repo_id = row["ticket_flow_repo_id"]
        job.ticket_flow_run_id = row["ticket_flow_run_id"]
        job.ticket_flow_worktree_id = row["ticket_flow_worktree_id"]
        job.publish_operation_id = row["publish_operation_id"]
        job.result_summary = row["result_summary"]
        job.error_text = row["error_text"]
        return job

    def _row_to_attempt(self, row: sqlite3.Row) -> AutomationJobAttempt:
        return AutomationJobAttempt.create(
            attempt_id=row["attempt_id"],
            job_id=row["job_id"],
            attempt_number=int(row["attempt_number"] or 1),
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_text=row["error_text"],
            executor_result=_json_object_from_row(row, "executor_result_json"),
            execution_refs=_json_object_from_row(row, "execution_refs_json"),
        )

    def _row_to_child_execution_edge(
        self, row: sqlite3.Row
    ) -> AutomationChildExecutionEdge:
        return AutomationChildExecutionEdge.create(
            edge_id=row["edge_id"],
            parent_job_id=row["parent_job_id"],
            child_kind=row["child_kind"],
            child_id=row["child_id"],
            authoritative_for_parent_completion=normalize_bool(
                row["authoritative_for_parent_completion"]
            ),
            requested_runtime=_json_loads_object(row["requested_runtime_json"]),
            actual_runtime=(
                _json_loads_object(row["actual_runtime_json"])
                if row["actual_runtime_json"] is not None
                else None
            ),
            terminal_mapping=_json_loads_object(row["terminal_mapping_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_schedule(self, row: sqlite3.Row) -> AutomationSchedule:
        return AutomationSchedule.create(
            schedule_id=row["schedule_id"],
            rule_id=row["rule_id"],
            schedule_kind=row["schedule_kind"],
            timezone=row["timezone"],
            next_fire_at=row["next_fire_at"],
            last_fire_at=row["last_fire_at"],
            misfire_policy=row["misfire_policy"],
            schedule=_json_object_from_row(row, "schedule_json"),
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _rule_params(self, rule: AutomationRule) -> tuple[Any, ...]:
        return (
            rule.rule_id,
            rule.name,
            1 if rule.enabled else 0,
            1 if rule.system_owned else 0,
            rule.trigger_kind,
            _json_dumps(rule.trigger),
            _json_dumps(rule.filters),
            rule.target_policy,
            _json_dumps(rule.target),
            rule.executor_kind,
            _json_dumps(rule.executor),
            _json_dumps(rule.policy),
            _json_dumps(rule.metadata),
            rule.created_at,
            rule.updated_at,
        )

    def _job_params(self, job: AutomationJob) -> tuple[Any, ...]:
        return (
            job.job_id,
            job.rule_id,
            job.event_id,
            job.state,
            job.dedupe_key,
            job.batch_key,
            job.lock_key,
            job.available_at,
            job.claimed_at,
            job.started_at,
            job.finished_at,
            job.updated_at,
            job.attempt_count,
            job.max_attempts,
            job.next_attempt_at,
            job.retry_backoff_seconds,
            job.created_at,
            _json_dumps(job.target),
            _json_dumps(job.executor),
            _json_dumps(job.policy),
            _json_dumps(job.payload),
            job.managed_thread_target_id,
            job.managed_thread_execution_id,
            job.pma_lane_id,
            job.pma_queue_item_id,
            job.ticket_flow_repo_id,
            job.ticket_flow_run_id,
            job.ticket_flow_worktree_id,
            job.publish_operation_id,
            job.result_summary,
            job.error_text,
        )


__all__ = ["AutomationStore"]
