from __future__ import annotations

import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
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


class PublishOperationState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    EFFECT_APPLIED = "effect_applied"
    FAILED = "failed"


class PublishAttemptState(str, Enum):
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PublishOperationTransition(str, Enum):
    CREATE = "create"
    CLAIM = "claim"
    RETRY = "retry"
    SUCCEED = "succeed"
    EFFECT_APPLIED = "effect_applied"
    RECONCILE_EFFECT_APPLIED = "reconcile_effect_applied"
    FAIL = "fail"


class PublishAttemptTransition(str, Enum):
    CLAIM = "claim"
    START = "start"
    SUCCEED = "succeed"
    FAIL = "fail"


_ACTIVE_DEDUPE_STATES = (
    PublishOperationState.PENDING,
    PublishOperationState.RUNNING,
    PublishOperationState.SUCCEEDED,
    PublishOperationState.EFFECT_APPLIED,
)
_ACTIVE_ATTEMPT_STATES = (
    PublishAttemptState.CLAIMED,
    PublishAttemptState.RUNNING,
)
_OPERATION_TRANSITIONS: dict[
    PublishOperationTransition,
    dict[PublishOperationState | None, PublishOperationState],
] = {
    PublishOperationTransition.CREATE: {
        None: PublishOperationState.PENDING,
    },
    PublishOperationTransition.CLAIM: {
        PublishOperationState.PENDING: PublishOperationState.RUNNING,
    },
    PublishOperationTransition.RETRY: {
        PublishOperationState.RUNNING: PublishOperationState.PENDING,
    },
    PublishOperationTransition.SUCCEED: {
        PublishOperationState.RUNNING: PublishOperationState.SUCCEEDED,
    },
    PublishOperationTransition.EFFECT_APPLIED: {
        PublishOperationState.RUNNING: PublishOperationState.EFFECT_APPLIED,
    },
    PublishOperationTransition.RECONCILE_EFFECT_APPLIED: {
        PublishOperationState.EFFECT_APPLIED: PublishOperationState.SUCCEEDED,
    },
    PublishOperationTransition.FAIL: {
        PublishOperationState.RUNNING: PublishOperationState.FAILED,
    },
}
_ATTEMPT_TRANSITIONS: dict[
    PublishAttemptTransition,
    dict[PublishAttemptState | None, PublishAttemptState],
] = {
    PublishAttemptTransition.CLAIM: {
        None: PublishAttemptState.CLAIMED,
    },
    PublishAttemptTransition.START: {
        PublishAttemptState.CLAIMED: PublishAttemptState.RUNNING,
    },
    PublishAttemptTransition.SUCCEED: {
        PublishAttemptState.CLAIMED: PublishAttemptState.SUCCEEDED,
        PublishAttemptState.RUNNING: PublishAttemptState.SUCCEEDED,
    },
    PublishAttemptTransition.FAIL: {
        PublishAttemptState.CLAIMED: PublishAttemptState.FAILED,
        PublishAttemptState.RUNNING: PublishAttemptState.FAILED,
    },
}


class _PublishJournalConcurrentTransition(RuntimeError):
    pass


def _operation_state(
    value: PublishOperationState | str | None,
) -> PublishOperationState | None:
    if value is None:
        return None
    if isinstance(value, PublishOperationState):
        return value
    try:
        return PublishOperationState(value)
    except ValueError as exc:
        raise RuntimeError(
            f"unknown publish operation lifecycle state: {value!r}"
        ) from exc


def _required_operation_state(value: str) -> PublishOperationState:
    state = _operation_state(value)
    if state is None:
        raise RuntimeError("publish operation lifecycle state is required")
    return state


def _attempt_state(
    value: PublishAttemptState | str | None,
) -> PublishAttemptState | None:
    if value is None:
        return None
    if isinstance(value, PublishAttemptState):
        return value
    try:
        return PublishAttemptState(value)
    except ValueError as exc:
        raise RuntimeError(
            f"unknown publish attempt lifecycle state: {value!r}"
        ) from exc


def _required_attempt_state(value: str) -> PublishAttemptState:
    state = _attempt_state(value)
    if state is None:
        raise RuntimeError("publish attempt lifecycle state is required")
    return state


def transition_publish_operation_state(
    transition: PublishOperationTransition,
    *,
    current_state: PublishOperationState | str | None,
) -> PublishOperationState:
    resolved_current = _operation_state(current_state)
    next_state = _OPERATION_TRANSITIONS[transition].get(resolved_current)
    if next_state is None:
        raise RuntimeError(
            "invalid publish operation lifecycle transition: "
            f"{transition.value} from "
            f"{resolved_current.value if resolved_current is not None else '<new>'}"
        )
    return next_state


def transition_publish_attempt_state(
    transition: PublishAttemptTransition,
    *,
    current_state: PublishAttemptState | str | None,
) -> PublishAttemptState:
    resolved_current = _attempt_state(current_state)
    next_state = _ATTEMPT_TRANSITIONS[transition].get(resolved_current)
    if next_state is None:
        raise RuntimeError(
            "invalid publish attempt lifecycle transition: "
            f"{transition.value} from "
            f"{resolved_current.value if resolved_current is not None else '<new>'}"
        )
    return next_state


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
    state = _required_operation_state(str(row["state"]))
    return PublishOperation(
        operation_id=str(row["operation_id"]),
        operation_key=str(row["operation_key"]),
        operation_kind=str(row["operation_kind"]),
        state=state.value,
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


def _attempt_state_from_row(row: sqlite3.Row) -> PublishAttemptState:
    return _required_attempt_state(str(row["state"]))


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
            self._ensure_known_operation_states(conn)
            existing = self._find_dedupable_operation(conn, normalized_key)
            if existing is not None:
                return _operation_from_row(existing), True
            operation_id = uuid.uuid4().hex
            created_state = transition_publish_operation_state(
                PublishOperationTransition.CREATE,
                current_state=None,
            )
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
                    ) VALUES (?, ?, ?, ?, ?, '{}', ?, ?, NULL, NULL, NULL, ?, NULL, 0)
                    """,
                    (
                        operation_id,
                        normalized_key,
                        normalized_kind,
                        created_state.value,
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
            if row is None:
                return None
            state = _required_operation_state(str(row["state"]))
            if state != PublishOperationState.PENDING:
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
                       AND state = ?
                    """,
                    (
                        _json_dumps(resolved_payload),
                        resolved_next_attempt,
                        updated_at,
                        normalized_operation_id,
                        PublishOperationState.PENDING.value,
                    ),
                )
                if cursor.rowcount == 0:
                    return None
                refreshed = self._load_operation_row(conn, normalized_operation_id)
        return _operation_from_row(refreshed) if refreshed is not None else None

    def patch_running_operation_payload(
        self,
        operation_id: str,
        payload_patch: dict[str, Any],
    ) -> Optional[PublishOperation]:
        """Shallow-merge ``payload_patch`` into ``payload_json`` for a running operation.

        Used to persist executor-local retry hints (e.g. dependency wait counters)
        before transitioning back to ``pending`` on retryable failure.
        """
        normalized_operation_id = _normalize_text(operation_id)
        if normalized_operation_id is None:
            return None
        patch = _normalize_json_object(payload_patch, field_name="payload_patch")
        updated_at = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_operation_row(conn, normalized_operation_id)
            if row is None:
                return None
            state = _required_operation_state(str(row["state"]))
            if state != PublishOperationState.RUNNING:
                return None
            payload = _json_loads_object(row["payload_json"])
            payload.update(patch)
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_publish_operations
                       SET payload_json = ?,
                           updated_at = ?
                     WHERE operation_id = ?
                       AND state = ?
                    """,
                    (
                        _json_dumps(payload),
                        updated_at,
                        normalized_operation_id,
                        PublishOperationState.RUNNING.value,
                    ),
                )
                if cursor.rowcount == 0:
                    return None
                refreshed = self._load_operation_row(conn, normalized_operation_id)
        return _operation_from_row(refreshed) if refreshed is not None else None

    def get_operation(self, operation_id: str) -> Optional[PublishOperation]:
        normalized_operation_id = _normalize_text(operation_id)
        if normalized_operation_id is None:
            return None
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_operation_row(conn, normalized_operation_id)
        return _operation_from_row(row) if row is not None else None

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
            self._ensure_known_operation_states(conn)
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_publish_operations
                 WHERE state = ?
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
                (PublishOperationState.PENDING.value, claimed_at, resolved_limit),
            ).fetchall()
            claimed: list[PublishOperation] = []
            for row in rows:
                operation_id = str(row["operation_id"])
                attempt_number = int(row["attempt_count"] or 0) + 1
                next_operation_state = transition_publish_operation_state(
                    PublishOperationTransition.CLAIM,
                    current_state=str(row["state"]),
                )
                new_attempt_state = transition_publish_attempt_state(
                    PublishAttemptTransition.CLAIM,
                    current_state=None,
                )
                cursor = conn.execute(
                    """
                    UPDATE orch_publish_operations
                       SET state = ?,
                           updated_at = ?,
                           claimed_at = ?,
                           started_at = NULL,
                           finished_at = NULL,
                           next_attempt_at = NULL,
                           last_error_text = NULL,
                           response_json = '{}',
                           attempt_count = ?
                     WHERE operation_id = ?
                       AND state = ?
                    """,
                    (
                        next_operation_state.value,
                        claimed_at,
                        claimed_at,
                        attempt_number,
                        operation_id,
                        PublishOperationState.PENDING.value,
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
                    ) VALUES (?, ?, ?, ?, '{}', NULL, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        attempt_id,
                        operation_id,
                        attempt_number,
                        new_attempt_state.value,
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
            operation_state = _required_operation_state(str(operation_row["state"]))
            if operation_state != PublishOperationState.RUNNING:
                return None
            attempt_state = _attempt_state_from_row(latest_attempt)
            if attempt_state == PublishAttemptState.RUNNING:
                return _operation_from_row(operation_row)
            if attempt_state != PublishAttemptState.CLAIMED:
                return None
            next_attempt_state = transition_publish_attempt_state(
                PublishAttemptTransition.START,
                current_state=attempt_state,
            )
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_publish_attempts
                       SET state = ?,
                           started_at = ?,
                           updated_at = ?
                     WHERE attempt_id = ?
                       AND state = ?
                    """,
                    (
                        next_attempt_state.value,
                        started_at,
                        started_at,
                        str(latest_attempt["attempt_id"]),
                        PublishAttemptState.CLAIMED.value,
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
                       AND state = ?
                    """,
                    (
                        started_at,
                        started_at,
                        normalized_operation_id,
                        PublishOperationState.RUNNING.value,
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
            operation_state = _required_operation_state(str(operation_row["state"]))
            attempt_state = _attempt_state_from_row(latest_attempt)
            if (
                operation_state == PublishOperationState.SUCCEEDED
                and attempt_state == PublishAttemptState.SUCCEEDED
            ):
                return _operation_from_row(operation_row)
            if attempt_state not in _ACTIVE_ATTEMPT_STATES:
                return None
            next_attempt_state = transition_publish_attempt_state(
                PublishAttemptTransition.SUCCEED,
                current_state=attempt_state,
            )
            next_operation_state = transition_publish_operation_state(
                PublishOperationTransition.SUCCEED,
                current_state=operation_state,
            )
            try:
                with conn:
                    cursor = conn.execute(
                        """
                        UPDATE orch_publish_attempts
                           SET state = ?,
                               response_json = ?,
                               error_text = NULL,
                               started_at = COALESCE(started_at, claimed_at),
                               finished_at = ?,
                               updated_at = ?
                         WHERE attempt_id = ?
                           AND state IN (?, ?)
                        """,
                        (
                            next_attempt_state.value,
                            response_json,
                            finished_at,
                            finished_at,
                            str(latest_attempt["attempt_id"]),
                            PublishAttemptState.CLAIMED.value,
                            PublishAttemptState.RUNNING.value,
                        ),
                    )
                    if cursor.rowcount == 0:
                        raise _PublishJournalConcurrentTransition
                    cursor = conn.execute(
                        """
                        UPDATE orch_publish_operations
                           SET state = ?,
                               response_json = ?,
                               updated_at = ?,
                               started_at = COALESCE(started_at, claimed_at),
                               finished_at = ?,
                               next_attempt_at = NULL,
                               last_error_text = NULL
                         WHERE operation_id = ?
                           AND state = ?
                        """,
                        (
                            next_operation_state.value,
                            response_json,
                            finished_at,
                            finished_at,
                            normalized_operation_id,
                            PublishOperationState.RUNNING.value,
                        ),
                    )
                    if cursor.rowcount == 0:
                        raise _PublishJournalConcurrentTransition
                    refreshed = self._load_operation_row(conn, normalized_operation_id)
            except _PublishJournalConcurrentTransition:
                return None
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
        transition = (
            PublishOperationTransition.RETRY
            if retry_at is not None
            else PublishOperationTransition.FAIL
        )
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
            operation_state = _required_operation_state(str(operation_row["state"]))
            attempt_state = _attempt_state_from_row(latest_attempt)
            if (
                operation_state == PublishOperationState.FAILED
                and attempt_state == PublishAttemptState.FAILED
            ):
                return _operation_from_row(operation_row)
            if attempt_state not in _ACTIVE_ATTEMPT_STATES:
                return None
            next_attempt_state = transition_publish_attempt_state(
                PublishAttemptTransition.FAIL,
                current_state=attempt_state,
            )
            next_operation_state = transition_publish_operation_state(
                transition,
                current_state=operation_state,
            )
            try:
                with conn:
                    cursor = conn.execute(
                        """
                        UPDATE orch_publish_attempts
                           SET state = ?,
                               response_json = '{}',
                               error_text = ?,
                               started_at = COALESCE(started_at, claimed_at),
                               finished_at = ?,
                               updated_at = ?
                         WHERE attempt_id = ?
                           AND state IN (?, ?)
                        """,
                        (
                            next_attempt_state.value,
                            normalized_error,
                            finished_at,
                            finished_at,
                            str(latest_attempt["attempt_id"]),
                            PublishAttemptState.CLAIMED.value,
                            PublishAttemptState.RUNNING.value,
                        ),
                    )
                    if cursor.rowcount == 0:
                        raise _PublishJournalConcurrentTransition
                    cursor = conn.execute(
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
                           AND state = ?
                        """,
                        (
                            next_operation_state.value,
                            finished_at,
                            finished_at,
                            retry_at,
                            normalized_error,
                            normalized_operation_id,
                            PublishOperationState.RUNNING.value,
                        ),
                    )
                    if cursor.rowcount == 0:
                        raise _PublishJournalConcurrentTransition
                    refreshed = self._load_operation_row(conn, normalized_operation_id)
            except _PublishJournalConcurrentTransition:
                return None
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
            operation_state = _required_operation_state(str(operation_row["state"]))
            attempt_state = _attempt_state_from_row(latest_attempt)
            if operation_state == PublishOperationState.EFFECT_APPLIED:
                return _operation_from_row(operation_row)
            if attempt_state not in _ACTIVE_ATTEMPT_STATES:
                return None
            next_attempt_state = transition_publish_attempt_state(
                PublishAttemptTransition.SUCCEED,
                current_state=attempt_state,
            )
            next_operation_state = transition_publish_operation_state(
                PublishOperationTransition.EFFECT_APPLIED,
                current_state=operation_state,
            )
            try:
                with conn:
                    cursor = conn.execute(
                        """
                        UPDATE orch_publish_attempts
                           SET state = ?,
                               response_json = ?,
                               error_text = NULL,
                               started_at = COALESCE(started_at, claimed_at),
                               finished_at = ?,
                               updated_at = ?
                         WHERE attempt_id = ?
                           AND state IN (?, ?)
                        """,
                        (
                            next_attempt_state.value,
                            response_json,
                            finished_at,
                            finished_at,
                            str(latest_attempt["attempt_id"]),
                            PublishAttemptState.CLAIMED.value,
                            PublishAttemptState.RUNNING.value,
                        ),
                    )
                    if cursor.rowcount == 0:
                        raise _PublishJournalConcurrentTransition
                    cursor = conn.execute(
                        """
                        UPDATE orch_publish_operations
                           SET state = ?,
                               response_json = ?,
                               updated_at = ?,
                               started_at = COALESCE(started_at, claimed_at),
                               finished_at = ?,
                               next_attempt_at = NULL,
                               last_error_text = ?
                         WHERE operation_id = ?
                           AND state = ?
                        """,
                        (
                            next_operation_state.value,
                            response_json,
                            finished_at,
                            finished_at,
                            normalized_error,
                            normalized_operation_id,
                            PublishOperationState.RUNNING.value,
                        ),
                    )
                    if cursor.rowcount == 0:
                        raise _PublishJournalConcurrentTransition
                    refreshed = self._load_operation_row(conn, normalized_operation_id)
            except _PublishJournalConcurrentTransition:
                return None
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
            operation_state = _required_operation_state(str(operation_row["state"]))
            if operation_state != PublishOperationState.EFFECT_APPLIED:
                return None
            next_operation_state = transition_publish_operation_state(
                PublishOperationTransition.RECONCILE_EFFECT_APPLIED,
                current_state=operation_state,
            )
            with conn:
                conn.execute(
                    """
                    UPDATE orch_publish_operations
                       SET state = ?,
                           updated_at = ?,
                           last_error_text = NULL
                     WHERE operation_id = ?
                       AND state = ?
                    """,
                    (
                        next_operation_state.value,
                        updated_at,
                        normalized_operation_id,
                        PublishOperationState.EFFECT_APPLIED.value,
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
            normalized_state = _required_operation_state(normalized_state).value
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
            self._ensure_known_operation_states(conn)
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
        active_states = tuple(state.value for state in _ACTIVE_DEDUPE_STATES)
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
            (operation_key, *active_states),
        ).fetchone()
        return _coerce_row(row)

    @staticmethod
    def _ensure_known_operation_states(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT DISTINCT state
              FROM orch_publish_operations
            """
        ).fetchall()
        for row in rows:
            _required_operation_state(str(row["state"]))

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
    "PublishAttemptState",
    "PublishAttemptTransition",
    "PublishJournalStore",
    "PublishOperation",
    "PublishOperationState",
    "PublishOperationTransition",
    "transition_publish_attempt_state",
    "transition_publish_operation_state",
]
