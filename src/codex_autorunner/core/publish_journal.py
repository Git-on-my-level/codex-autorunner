from __future__ import annotations

import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .orchestration.sqlite import open_orchestration_sqlite
from .text_utils import (
    _json_dumps,
    _json_loads_object,
    _normalize_limit,
    _normalize_text,
)
from .time_utils import now_iso

_ACTIVE_DEDUPE_STATES = ("pending", "running", "succeeded", "effect_applied")
_ACTIVE_ATTEMPT_STATES = ("claimed", "running")


def _normalize_json_object(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    raise ValueError(f"{field_name} must be a JSON object")


def _normalize_timestamp(value: Any, *, field_name: str) -> Optional[str]:
    normalized = _normalize_text(value)
    if normalized is None:
        return None
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    return normalized


@dataclass(frozen=True)
class PublishOperation:
    operation_id: str
    operation_key: str
    operation_kind: str
    state: str
    payload: dict[str, Any]
    response: dict[str, Any]
    created_at: str
    updated_at: str
    claimed_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    last_error_text: Optional[str] = None
    attempt_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _operation_from_row(row: sqlite3.Row) -> PublishOperation:
    return PublishOperation(
        operation_id=str(row["operation_id"]),
        operation_key=str(row["operation_key"]),
        operation_kind=str(row["operation_kind"]),
        state=str(row["state"]),
        payload=_json_loads_object(row["payload_json"]),
        response=_json_loads_object(row["response_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        claimed_at=_normalize_text(row["claimed_at"]),
        started_at=_normalize_text(row["started_at"]),
        finished_at=_normalize_text(row["finished_at"]),
        next_attempt_at=_normalize_text(row["next_attempt_at"]),
        last_error_text=_normalize_text(row["last_error_text"]),
        attempt_count=int(row["attempt_count"] or 0),
    )


def _coerce_row(value: Any) -> Optional[sqlite3.Row]:
    return value if isinstance(value, sqlite3.Row) else None


class PublishJournalStore:
    """SQLite-backed publish journal for idempotent automation operations.

    State model:
      pending -> running -> succeeded
                          -> effect_applied -> succeeded  (via reconcile)
                          -> pending  (retryable failure)
                          -> failed   (terminal failure)

    The ``effect_applied`` state captures the partial-success condition where
    external side effects completed but journal bookkeeping (``mark_succeeded``)
    failed.  It is reconciled to ``succeeded`` by ``reconcile_effect_applied``,
    which the drain loop calls on each cycle.
    """

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def create_operation(
        self,
        *,
        operation_key: str,
        operation_kind: str,
        payload: Optional[dict[str, Any]] = None,
        next_attempt_at: Optional[str] = None,
    ) -> tuple[PublishOperation, bool]:
        normalized_key = _normalize_text(operation_key)
        normalized_kind = _normalize_text(operation_kind)
        if normalized_key is None:
            raise ValueError("operation_key is required")
        if normalized_kind is None:
            raise ValueError("operation_kind is required")
        payload_object = _normalize_json_object(payload, field_name="payload")
        timestamp = now_iso()
        next_attempt = (
            _normalize_timestamp(next_attempt_at, field_name="next_attempt_at")
            or timestamp
        )

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            existing = self._find_dedupable_operation(conn, normalized_key)
            if existing is not None:
                return _operation_from_row(existing), True
            operation_id = uuid.uuid4().hex
            try:
                conn.execute(
                    """
                    INSERT INTO orch_publish_operations (
                        operation_id,
                        operation_key,
                        operation_kind,
                        state,
                        payload_json,
                        response_json,
                        created_at,
                        updated_at,
                        claimed_at,
                        started_at,
                        finished_at,
                        next_attempt_at,
                        last_error_text,
                        attempt_count
                    ) VALUES (?, ?, ?, 'pending', ?, '{}', ?, ?, NULL, NULL, NULL, ?, NULL, 0)
                    """,
                    (
                        operation_id,
                        normalized_key,
                        normalized_kind,
                        _json_dumps(payload_object),
                        timestamp,
                        timestamp,
                        next_attempt,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self._find_dedupable_operation(conn, normalized_key)
                if existing is None:
                    raise
                return _operation_from_row(existing), True
            created = self._load_operation_row(conn, operation_id)
        if created is None:
            raise RuntimeError("publish operation row missing after insert")
        return _operation_from_row(created), False

    def update_pending_operation(
        self,
        operation_id: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        next_attempt_at: Optional[str] = None,
    ) -> Optional[PublishOperation]:
        normalized_operation_id = _normalize_text(operation_id)
        if normalized_operation_id is None:
            return None
        payload_object = (
            _normalize_json_object(payload, field_name="payload")
            if payload is not None
            else None
        )
        normalized_next_attempt = (
            _normalize_timestamp(next_attempt_at, field_name="next_attempt_at")
            if next_attempt_at is not None
            else None
        )
        updated_at = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_operation_row(conn, normalized_operation_id)
            if row is None or str(row["state"]) != "pending":
                return None
            resolved_payload = (
                payload_object
                if payload_object is not None
                else _json_loads_object(row["payload_json"])
            )
            resolved_next_attempt = (
                normalized_next_attempt
                if normalized_next_attempt is not None
                else _normalize_text(row["next_attempt_at"]) or updated_at
            )
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_publish_operations
                       SET payload_json = ?,
                           next_attempt_at = ?,
                           updated_at = ?
                     WHERE operation_id = ?
                       AND state = 'pending'
                    """,
                    (
                        _json_dumps(resolved_payload),
                        resolved_next_attempt,
                        updated_at,
                        normalized_operation_id,
                    ),
                )
                if cursor.rowcount == 0:
                    return None
                refreshed = self._load_operation_row(conn, normalized_operation_id)
        return _operation_from_row(refreshed) if refreshed is not None else None

    def claim_pending_operations(
        self,
        *,
        limit: int = 10,
        now_timestamp: Optional[str] = None,
    ) -> list[PublishOperation]:
        resolved_limit = _normalize_limit(limit, default=10)
        if resolved_limit <= 0:
            return []
        claimed_at = (
            _normalize_timestamp(now_timestamp, field_name="now_timestamp") or now_iso()
        )

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_publish_operations
                 WHERE state = 'pending'
                   AND COALESCE(next_attempt_at, created_at) <= ?
                 ORDER BY COALESCE(next_attempt_at, created_at) ASC,
                          CASE operation_kind
                              WHEN 'react_pr_review_comment' THEN 0
                              WHEN 'enqueue_managed_turn' THEN 1
                              WHEN 'notify_chat' THEN 2
                              ELSE 50
                          END ASC,
                          created_at ASC,
                          operation_id ASC
                 LIMIT ?
                """,
                (claimed_at, resolved_limit),
            ).fetchall()
            claimed: list[PublishOperation] = []
            for row in rows:
                operation_id = str(row["operation_id"])
                attempt_number = int(row["attempt_count"] or 0) + 1
                cursor = conn.execute(
                    """
                    UPDATE orch_publish_operations
                       SET state = 'running',
                           updated_at = ?,
                           claimed_at = ?,
                           started_at = NULL,
                           finished_at = NULL,
                           next_attempt_at = NULL,
                           last_error_text = NULL,
                           response_json = '{}',
                           attempt_count = ?
                     WHERE operation_id = ?
                       AND state = 'pending'
                    """,
                    (
                        claimed_at,
                        claimed_at,
                        attempt_number,
                        operation_id,
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                attempt_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO orch_publish_attempts (
                        attempt_id,
                        operation_id,
                        attempt_number,
                        state,
                        response_json,
                        error_text,
                        claimed_at,
                        started_at,
                        finished_at,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, 'claimed', '{}', NULL, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        attempt_id,
                        operation_id,
                        attempt_number,
                        claimed_at,
                        claimed_at,
                        claimed_at,
                    ),
                )
                refreshed = self._load_operation_row(conn, operation_id)
                if refreshed is not None:
                    claimed.append(_operation_from_row(refreshed))
            conn.commit()
        return claimed

    def mark_running(self, operation_id: str) -> Optional[PublishOperation]:
        normalized_operation_id = _normalize_text(operation_id)
        if normalized_operation_id is None:
            return None
        started_at = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            operation_row = self._load_operation_row(conn, normalized_operation_id)
            if operation_row is None:
                return None
            latest_attempt = self._load_latest_attempt_row(
                conn, normalized_operation_id
            )
            if latest_attempt is None:
                return None
            attempt_state = str(latest_attempt["state"])
            if attempt_state == "running":
                return _operation_from_row(operation_row)
            if attempt_state != "claimed":
                return None
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_publish_attempts
                       SET state = 'running',
                           started_at = ?,
                           updated_at = ?
                     WHERE attempt_id = ?
                       AND state = 'claimed'
                    """,
                    (
                        started_at,
                        started_at,
                        str(latest_attempt["attempt_id"]),
                    ),
                )
                if cursor.rowcount == 0:
                    return None
                conn.execute(
                    """
                    UPDATE orch_publish_operations
                       SET started_at = ?,
                           updated_at = ?
                     WHERE operation_id = ?
                       AND state = 'running'
                    """,
                    (
                        started_at,
                        started_at,
                        normalized_operation_id,
                    ),
                )
                refreshed = self._load_operation_row(conn, normalized_operation_id)
        return _operation_from_row(refreshed) if refreshed is not None else None

    def mark_succeeded(
        self,
        operation_id: str,
        *,
        response: Optional[dict[str, Any]] = None,
    ) -> Optional[PublishOperation]:
        normalized_operation_id = _normalize_text(operation_id)
        if normalized_operation_id is None:
            return None
        response_object = _normalize_json_object(response, field_name="response")
        response_json = _json_dumps(response_object)
        finished_at = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            operation_row = self._load_operation_row(conn, normalized_operation_id)
            if operation_row is None:
                return None
            latest_attempt = self._load_latest_attempt_row(
                conn, normalized_operation_id
            )
            if latest_attempt is None:
                return None
            attempt_state = str(latest_attempt["state"])
            if operation_row["state"] == "succeeded" and attempt_state == "succeeded":
                return _operation_from_row(operation_row)
            if attempt_state not in _ACTIVE_ATTEMPT_STATES:
                return None
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_publish_attempts
                       SET state = 'succeeded',
                           response_json = ?,
                           error_text = NULL,
                           started_at = COALESCE(started_at, claimed_at),
                           finished_at = ?,
                           updated_at = ?
                     WHERE attempt_id = ?
                       AND state IN ('claimed', 'running')
                    """,
                    (
                        response_json,
                        finished_at,
                        finished_at,
                        str(latest_attempt["attempt_id"]),
                    ),
                )
                if cursor.rowcount == 0:
                    return None
                conn.execute(
                    """
                    UPDATE orch_publish_operations
                       SET state = 'succeeded',
                           response_json = ?,
                           updated_at = ?,
                           started_at = COALESCE(started_at, claimed_at),
                           finished_at = ?,
                           next_attempt_at = NULL,
                           last_error_text = NULL
                     WHERE operation_id = ?
                       AND state = 'running'
                    """,
                    (
                        response_json,
                        finished_at,
                        finished_at,
                        normalized_operation_id,
                    ),
                )
                refreshed = self._load_operation_row(conn, normalized_operation_id)
        return _operation_from_row(refreshed) if refreshed is not None else None

    def mark_failed(
        self,
        operation_id: str,
        *,
        error_text: Optional[str] = None,
        next_attempt_at: Optional[str] = None,
    ) -> Optional[PublishOperation]:
        normalized_operation_id = _normalize_text(operation_id)
        if normalized_operation_id is None:
            return None
        normalized_error = _normalize_text(error_text)
        retry_at = _normalize_timestamp(next_attempt_at, field_name="next_attempt_at")
        target_state = "pending" if retry_at is not None else "failed"
        finished_at = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            operation_row = self._load_operation_row(conn, normalized_operation_id)
            if operation_row is None:
                return None
            latest_attempt = self._load_latest_attempt_row(
                conn, normalized_operation_id
            )
            if latest_attempt is None:
                return None
            attempt_state = str(latest_attempt["state"])
            if operation_row["state"] == "failed" and attempt_state == "failed":
                return _operation_from_row(operation_row)
            if attempt_state not in _ACTIVE_ATTEMPT_STATES:
                return None
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_publish_attempts
                       SET state = 'failed',
                           response_json = '{}',
                           error_text = ?,
                           started_at = COALESCE(started_at, claimed_at),
                           finished_at = ?,
                           updated_at = ?
                     WHERE attempt_id = ?
                       AND state IN ('claimed', 'running')
                    """,
                    (
                        normalized_error,
                        finished_at,
                        finished_at,
                        str(latest_attempt["attempt_id"]),
                    ),
                )
                if cursor.rowcount == 0:
                    return None
                conn.execute(
                    """
                    UPDATE orch_publish_operations
                       SET state = ?,
                           response_json = '{}',
                           updated_at = ?,
                           started_at = COALESCE(started_at, claimed_at),
                           finished_at = ?,
                           next_attempt_at = ?,
                           last_error_text = ?
                     WHERE operation_id = ?
                       AND state = 'running'
                    """,
                    (
                        target_state,
                        finished_at,
                        finished_at,
                        retry_at,
                        normalized_error,
                        normalized_operation_id,
                    ),
                )
                refreshed = self._load_operation_row(conn, normalized_operation_id)
        return _operation_from_row(refreshed) if refreshed is not None else None

    def mark_effect_applied(
        self,
        operation_id: str,
        *,
        response: Optional[dict[str, Any]] = None,
        error_text: Optional[str] = None,
    ) -> Optional[PublishOperation]:
        """Record that external side effects succeeded but journal completion failed.

        Transitions the operation from ``running`` to ``effect_applied``,
        preserving the side-effect response and the bookkeeping error text.
        The operation is later reconciled to ``succeeded`` by
        ``reconcile_effect_applied``.
        """
        normalized_operation_id = _normalize_text(operation_id)
        if normalized_operation_id is None:
            return None
        response_object = _normalize_json_object(response, field_name="response")
        response_json = _json_dumps(response_object)
        normalized_error = _normalize_text(error_text)
        finished_at = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            operation_row = self._load_operation_row(conn, normalized_operation_id)
            if operation_row is None:
                return None
            latest_attempt = self._load_latest_attempt_row(
                conn, normalized_operation_id
            )
            if latest_attempt is None:
                return None
            attempt_state = str(latest_attempt["state"])
            if operation_row["state"] == "effect_applied":
                return _operation_from_row(operation_row)
            if attempt_state not in _ACTIVE_ATTEMPT_STATES:
                return None
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_publish_attempts
                       SET state = 'succeeded',
                           response_json = ?,
                           error_text = NULL,
                           started_at = COALESCE(started_at, claimed_at),
                           finished_at = ?,
                           updated_at = ?
                     WHERE attempt_id = ?
                       AND state IN ('claimed', 'running')
                    """,
                    (
                        response_json,
                        finished_at,
                        finished_at,
                        str(latest_attempt["attempt_id"]),
                    ),
                )
                if cursor.rowcount == 0:
                    return None
                conn.execute(
                    """
                    UPDATE orch_publish_operations
                       SET state = 'effect_applied',
                           response_json = ?,
                           updated_at = ?,
                           started_at = COALESCE(started_at, claimed_at),
                           finished_at = ?,
                           next_attempt_at = NULL,
                           last_error_text = ?
                     WHERE operation_id = ?
                       AND state = 'running'
                    """,
                    (
                        response_json,
                        finished_at,
                        finished_at,
                        normalized_error,
                        normalized_operation_id,
                    ),
                )
                refreshed = self._load_operation_row(conn, normalized_operation_id)
        return _operation_from_row(refreshed) if refreshed is not None else None

    def reconcile_effect_applied(self, operation_id: str) -> Optional[PublishOperation]:
        """Promote an ``effect_applied`` operation to ``succeeded``.

        Called by the drain loop on each cycle to finalize operations whose
        side effects completed but whose journal bookkeeping initially failed.
        Clears ``last_error_text`` as part of the promotion.
        """
        normalized_operation_id = _normalize_text(operation_id)
        if normalized_operation_id is None:
            return None
        updated_at = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            operation_row = self._load_operation_row(conn, normalized_operation_id)
            if operation_row is None:
                return None
            if str(operation_row["state"]) != "effect_applied":
                return None
            with conn:
                conn.execute(
                    """
                    UPDATE orch_publish_operations
                       SET state = 'succeeded',
                           updated_at = ?,
                           last_error_text = NULL
                     WHERE operation_id = ?
                       AND state = 'effect_applied'
                    """,
                    (
                        updated_at,
                        normalized_operation_id,
                    ),
                )
                refreshed = self._load_operation_row(conn, normalized_operation_id)
        return _operation_from_row(refreshed) if refreshed is not None else None

    def list_operations(
        self,
        *,
        state: Optional[str] = None,
        operation_kind: Optional[str] = None,
        limit: Optional[int] = None,
        newest_first: bool = False,
    ) -> list[PublishOperation]:
        where_clauses: list[str] = []
        params: list[Any] = []
        normalized_state = _normalize_text(state)
        normalized_kind = _normalize_text(operation_kind)
        if normalized_state is not None:
            where_clauses.append("state = ?")
            params.append(normalized_state)
        if normalized_kind is not None:
            where_clauses.append("operation_kind = ?")
            params.append(normalized_kind)
        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)
        limit_sql = ""
        resolved_limit = _normalize_limit(limit, default=0)
        if limit is not None:
            limit_sql = "LIMIT ?"
            params.append(resolved_limit)

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_publish_operations
                  {where_sql}
                 ORDER BY created_at {"DESC" if newest_first else "ASC"},
                          operation_id {"DESC" if newest_first else "ASC"}
                  {limit_sql}
                """,
                params,
            ).fetchall()
        return [_operation_from_row(row) for row in rows]

    @staticmethod
    def _find_dedupable_operation(
        conn: sqlite3.Connection,
        operation_key: str,
    ) -> Optional[sqlite3.Row]:
        placeholders = ",".join("?" for _ in _ACTIVE_DEDUPE_STATES)
        row = conn.execute(
            f"""
            SELECT *
              FROM orch_publish_operations
             WHERE operation_key = ?
               AND state IN ({placeholders})
             ORDER BY CASE state
                          WHEN 'running' THEN 0
                          WHEN 'pending' THEN 1
                          WHEN 'succeeded' THEN 2
                          ELSE 3
                      END,
                      created_at DESC,
                      operation_id DESC
             LIMIT 1
            """,
            (operation_key, *_ACTIVE_DEDUPE_STATES),
        ).fetchone()
        return _coerce_row(row)

    @staticmethod
    def _load_operation_row(
        conn: sqlite3.Connection,
        operation_id: str,
    ) -> Optional[sqlite3.Row]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_publish_operations
             WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        return _coerce_row(row)

    @staticmethod
    def _load_latest_attempt_row(
        conn: sqlite3.Connection,
        operation_id: str,
    ) -> Optional[sqlite3.Row]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_publish_attempts
             WHERE operation_id = ?
             ORDER BY attempt_number DESC, created_at DESC, attempt_id DESC
             LIMIT 1
            """,
            (operation_id,),
        ).fetchone()
        return _coerce_row(row)


__all__ = [
    "PublishJournalStore",
    "PublishOperation",
]
