from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.publish_journal import (
    PublishAttemptState,
    PublishAttemptTransition,
    PublishJournalStore,
    PublishOperationState,
    PublishOperationTransition,
    transition_publish_attempt_state,
    transition_publish_operation_state,
)


def _attempt_rows(hub_root: Path, operation_id: str) -> list[dict[str, object]]:
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            """
            SELECT attempt_number, state, response_json, error_text, claimed_at, started_at, finished_at
              FROM orch_publish_attempts
             WHERE operation_id = ?
             ORDER BY attempt_number ASC
            """,
            (operation_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_publish_lifecycle_contract_accepts_named_legal_transitions() -> None:
    assert (
        transition_publish_operation_state(
            PublishOperationTransition.CREATE,
            current_state=None,
        )
        == PublishOperationState.PENDING
    )
    assert (
        transition_publish_operation_state(
            PublishOperationTransition.CLAIM,
            current_state=PublishOperationState.PENDING,
        )
        == PublishOperationState.RUNNING
    )
    assert (
        transition_publish_operation_state(
            PublishOperationTransition.RETRY,
            current_state=PublishOperationState.RUNNING,
        )
        == PublishOperationState.PENDING
    )
    assert (
        transition_publish_operation_state(
            PublishOperationTransition.EFFECT_APPLIED,
            current_state=PublishOperationState.RUNNING,
        )
        == PublishOperationState.EFFECT_APPLIED
    )
    assert (
        transition_publish_operation_state(
            PublishOperationTransition.RECONCILE_EFFECT_APPLIED,
            current_state=PublishOperationState.EFFECT_APPLIED,
        )
        == PublishOperationState.SUCCEEDED
    )
    assert (
        transition_publish_attempt_state(
            PublishAttemptTransition.CLAIM,
            current_state=None,
        )
        == PublishAttemptState.CLAIMED
    )
    assert (
        transition_publish_attempt_state(
            PublishAttemptTransition.START,
            current_state=PublishAttemptState.CLAIMED,
        )
        == PublishAttemptState.RUNNING
    )
    assert (
        transition_publish_attempt_state(
            PublishAttemptTransition.SUCCEED,
            current_state=PublishAttemptState.RUNNING,
        )
        == PublishAttemptState.SUCCEEDED
    )


def test_publish_lifecycle_contract_rejects_terminal_rewinds() -> None:
    with pytest.raises(RuntimeError, match="invalid publish operation"):
        transition_publish_operation_state(
            PublishOperationTransition.CLAIM,
            current_state=PublishOperationState.SUCCEEDED,
        )
    with pytest.raises(RuntimeError, match="invalid publish operation"):
        transition_publish_operation_state(
            PublishOperationTransition.RETRY,
            current_state=PublishOperationState.FAILED,
        )
    with pytest.raises(RuntimeError, match="invalid publish attempt"):
        transition_publish_attempt_state(
            PublishAttemptTransition.START,
            current_state=PublishAttemptState.SUCCEEDED,
        )


def test_create_operation_dedupes_active_operation_key(tmp_path: Path) -> None:
    store = PublishJournalStore(tmp_path)

    created, deduped = store.create_operation(
        operation_key="github:comment:1",
        operation_kind="github_comment",
        payload={"body": "hello"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )
    assert deduped is False
    assert created.state == "pending"
    assert created.payload == {"body": "hello"}
    assert created.response == {}
    assert created.attempt_count == 0

    duplicate, deduped = store.create_operation(
        operation_key="github:comment:1",
        operation_kind="github_comment",
        payload={"body": "different"},
        next_attempt_at="2026-03-25T01:00:00Z",
    )
    assert deduped is True
    assert duplicate.operation_id == created.operation_id
    assert duplicate.payload == {"body": "hello"}

    operations = store.list_operations()
    assert [operation.operation_id for operation in operations] == [
        created.operation_id
    ]

    with open_orchestration_sqlite(tmp_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
              FROM orch_publish_operations
             WHERE operation_key = ?
            """,
            ("github:comment:1",),
        ).fetchone()
    assert row is not None
    assert int(row["count"] or 0) == 1


def test_claim_and_mark_succeeded_persist_attempt_metadata(tmp_path: Path) -> None:
    store = PublishJournalStore(tmp_path)

    first, _ = store.create_operation(
        operation_key="github:comment:ready",
        operation_kind="github_comment",
        payload={"body": "ready"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )
    later, _ = store.create_operation(
        operation_key="github:comment:later",
        operation_kind="github_comment",
        payload={"body": "later"},
        next_attempt_at="2026-03-25T03:00:00Z",
    )

    claimed = store.claim_pending_operations(
        limit=10,
        now_timestamp="2026-03-25T01:00:00Z",
    )
    assert [operation.operation_id for operation in claimed] == [first.operation_id]
    assert claimed[0].state == "running"
    assert claimed[0].claimed_at == "2026-03-25T01:00:00Z"
    assert claimed[0].attempt_count == 1

    running = store.mark_running(first.operation_id)
    assert running is not None
    assert running.state == "running"
    assert running.started_at == running.updated_at

    succeeded = store.mark_succeeded(
        first.operation_id,
        response={"remote_id": "comment-123", "status": "ok"},
    )
    assert succeeded is not None
    assert succeeded.state == "succeeded"
    assert succeeded.response == {"remote_id": "comment-123", "status": "ok"}
    assert succeeded.finished_at is not None

    listed = store.list_operations()
    assert {operation.operation_id for operation in listed} == {
        first.operation_id,
        later.operation_id,
    }

    attempts = _attempt_rows(tmp_path, first.operation_id)
    assert len(attempts) == 1
    assert attempts[0]["attempt_number"] == 1
    assert attempts[0]["state"] == "succeeded"
    assert json.loads(str(attempts[0]["response_json"])) == {
        "remote_id": "comment-123",
        "status": "ok",
    }
    assert attempts[0]["started_at"] is not None
    assert attempts[0]["finished_at"] is not None


def test_mark_failed_supports_retryable_and_terminal_flows(tmp_path: Path) -> None:
    store = PublishJournalStore(tmp_path)

    created, _ = store.create_operation(
        operation_key="github:status:1",
        operation_kind="github_status",
        payload={"state": "failure"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    claimed = store.claim_pending_operations(
        now_timestamp="2026-03-25T00:30:00Z",
    )
    assert [operation.operation_id for operation in claimed] == [created.operation_id]

    retryable = store.mark_failed(
        created.operation_id,
        error_text="temporary outage",
        next_attempt_at="2026-03-25T02:00:00Z",
    )
    assert retryable is not None
    assert retryable.state == "pending"
    assert retryable.next_attempt_at == "2026-03-25T02:00:00Z"
    assert retryable.last_error_text == "temporary outage"
    assert retryable.attempt_count == 1

    duplicate, deduped = store.create_operation(
        operation_key="github:status:1",
        operation_kind="github_status",
        payload={"state": "failure"},
    )
    assert deduped is True
    assert duplicate.operation_id == created.operation_id

    assert (
        store.claim_pending_operations(
            now_timestamp="2026-03-25T01:59:59Z",
        )
        == []
    )

    retried = store.claim_pending_operations(
        now_timestamp="2026-03-25T02:00:00Z",
    )
    assert [operation.operation_id for operation in retried] == [created.operation_id]
    assert retried[0].attempt_count == 2

    terminal = store.mark_failed(
        created.operation_id,
        error_text="permanent rejection",
    )
    assert terminal is not None
    assert terminal.state == "failed"
    assert terminal.next_attempt_at is None
    assert terminal.last_error_text == "permanent rejection"

    replacement, deduped = store.create_operation(
        operation_key="github:status:1",
        operation_kind="github_status",
        payload={"state": "success"},
        next_attempt_at="2026-03-25T03:00:00Z",
    )
    assert deduped is False
    assert replacement.operation_id != created.operation_id

    attempts = _attempt_rows(tmp_path, created.operation_id)
    assert len(attempts) == 2
    assert [attempt["state"] for attempt in attempts] == ["failed", "failed"]
    assert attempts[0]["error_text"] == "temporary outage"
    assert attempts[1]["error_text"] == "permanent rejection"


def test_mark_effect_applied_records_partial_success(tmp_path: Path) -> None:
    store = PublishJournalStore(tmp_path)

    created, _ = store.create_operation(
        operation_key="github:comment:effect-applied",
        operation_kind="github_comment",
        payload={"body": "hello"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    claimed = store.claim_pending_operations(
        now_timestamp="2026-03-25T00:30:00Z",
    )
    assert [op.operation_id for op in claimed] == [created.operation_id]

    running = store.mark_running(created.operation_id)
    assert running is not None
    assert running.state == "running"

    effect_applied = store.mark_effect_applied(
        created.operation_id,
        response={"remote_id": "comment-456", "status": "ok"},
        error_text="journal completion failed: sqlite3.OperationalError: database is locked",
    )
    assert effect_applied is not None
    assert effect_applied.state == "effect_applied"
    assert effect_applied.response == {"remote_id": "comment-456", "status": "ok"}
    assert effect_applied.finished_at is not None
    assert (
        effect_applied.last_error_text
        == "journal completion failed: sqlite3.OperationalError: database is locked"
    )
    assert effect_applied.next_attempt_at is None

    attempts = _attempt_rows(tmp_path, created.operation_id)
    assert len(attempts) == 1
    assert attempts[0]["state"] == "succeeded"
    assert json.loads(str(attempts[0]["response_json"])) == {
        "remote_id": "comment-456",
        "status": "ok",
    }


def test_mark_effect_applied_is_idempotent(tmp_path: Path) -> None:
    store = PublishJournalStore(tmp_path)

    created, _ = store.create_operation(
        operation_key="github:comment:effect-idempotent",
        operation_kind="github_comment",
        payload={"body": "hello"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    store.claim_pending_operations(now_timestamp="2026-03-25T00:30:00Z")
    store.mark_running(created.operation_id)

    first = store.mark_effect_applied(
        created.operation_id,
        response={"delivered": True},
        error_text="bookkeeping failed",
    )
    assert first is not None
    assert first.state == "effect_applied"

    second = store.mark_effect_applied(
        created.operation_id,
        response={"delivered": True},
        error_text="bookkeeping failed",
    )
    assert second is not None
    assert second.state == "effect_applied"
    assert second.operation_id == first.operation_id


def test_reconcile_effect_applied_transitions_to_succeeded(tmp_path: Path) -> None:
    store = PublishJournalStore(tmp_path)

    created, _ = store.create_operation(
        operation_key="github:comment:reconcile",
        operation_kind="github_comment",
        payload={"body": "hello"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    store.claim_pending_operations(now_timestamp="2026-03-25T00:30:00Z")
    store.mark_running(created.operation_id)

    store.mark_effect_applied(
        created.operation_id,
        response={"remote_id": "comment-789"},
        error_text="journal completion failed",
    )

    reconciled = store.reconcile_effect_applied(created.operation_id)
    assert reconciled is not None
    assert reconciled.state == "succeeded"
    assert reconciled.response == {"remote_id": "comment-789"}
    assert reconciled.last_error_text is None


def test_reconcile_effect_applied_is_noop_for_non_effect_applied(
    tmp_path: Path,
) -> None:
    store = PublishJournalStore(tmp_path)

    created, _ = store.create_operation(
        operation_key="github:comment:no-reconcile",
        operation_kind="github_comment",
        payload={"body": "hello"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    assert store.reconcile_effect_applied(created.operation_id) is None

    store.claim_pending_operations(now_timestamp="2026-03-25T00:30:00Z")
    store.mark_running(created.operation_id)
    store.mark_succeeded(created.operation_id, response={"delivered": True})

    assert store.reconcile_effect_applied(created.operation_id) is None


def test_effect_applied_state_prevents_duplicate_operation_creation(
    tmp_path: Path,
) -> None:
    store = PublishJournalStore(tmp_path)

    created, _ = store.create_operation(
        operation_key="github:comment:dedupe-effect",
        operation_kind="github_comment",
        payload={"body": "original"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    store.claim_pending_operations(now_timestamp="2026-03-25T00:30:00Z")
    store.mark_running(created.operation_id)
    store.mark_effect_applied(
        created.operation_id,
        response={"delivered": True},
        error_text="bookkeeping failed",
    )

    duplicate, deduped = store.create_operation(
        operation_key="github:comment:dedupe-effect",
        operation_kind="github_comment",
        payload={"body": "replacement"},
        next_attempt_at="2026-03-25T03:00:00Z",
    )
    assert deduped is True
    assert duplicate.operation_id == created.operation_id
    assert duplicate.payload == {"body": "original"}

    operations = store.list_operations()
    assert len(operations) == 1
    assert operations[0].state == "effect_applied"


def test_list_operations_filters_effect_applied(tmp_path: Path) -> None:
    store = PublishJournalStore(tmp_path)

    created, _ = store.create_operation(
        operation_key="github:comment:list-effect",
        operation_kind="github_comment",
        payload={"body": "hello"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    store.claim_pending_operations(now_timestamp="2026-03-25T00:30:00Z")
    store.mark_running(created.operation_id)
    store.mark_effect_applied(
        created.operation_id,
        response={"delivered": True},
        error_text="bookkeeping failed",
    )

    all_ops = store.list_operations()
    assert len(all_ops) == 1
    assert all_ops[0].state == "effect_applied"

    filtered = store.list_operations(state="effect_applied")
    assert len(filtered) == 1
    assert filtered[0].operation_id == created.operation_id

    assert store.list_operations(state="succeeded") == []
    assert store.list_operations(state="failed") == []


def test_unknown_persisted_operation_state_rejected_at_admission_boundaries(
    tmp_path: Path,
) -> None:
    store = PublishJournalStore(tmp_path)
    created, _ = store.create_operation(
        operation_key="github:comment:unknown-operation",
        operation_kind="github_comment",
        payload={"body": "hello"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    with open_orchestration_sqlite(tmp_path) as conn:
        conn.execute(
            """
            UPDATE orch_publish_operations
               SET state = 'mystery'
             WHERE operation_id = ?
            """,
            (created.operation_id,),
        )

    with pytest.raises(RuntimeError, match="unknown publish operation"):
        store.get_operation(created.operation_id)
    with pytest.raises(RuntimeError, match="unknown publish operation"):
        store.list_operations()
    with pytest.raises(RuntimeError, match="unknown publish operation"):
        store.claim_pending_operations(now_timestamp="2026-03-25T01:00:00Z")
    with pytest.raises(RuntimeError, match="unknown publish operation"):
        store.create_operation(
            operation_key="github:comment:unknown-operation",
            operation_kind="github_comment",
        )


def test_unknown_persisted_attempt_state_rejected_at_write_boundary(
    tmp_path: Path,
) -> None:
    store = PublishJournalStore(tmp_path)
    created, _ = store.create_operation(
        operation_key="github:comment:unknown-attempt",
        operation_kind="github_comment",
        payload={"body": "hello"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )
    store.claim_pending_operations(now_timestamp="2026-03-25T00:30:00Z")

    with open_orchestration_sqlite(tmp_path) as conn:
        conn.execute(
            """
            UPDATE orch_publish_attempts
               SET state = 'mystery'
             WHERE operation_id = ?
            """,
            (created.operation_id,),
        )

    with pytest.raises(RuntimeError, match="unknown publish attempt"):
        store.mark_running(created.operation_id)
    with pytest.raises(RuntimeError, match="unknown publish attempt"):
        store.mark_succeeded(created.operation_id, response={"delivered": True})
    with pytest.raises(RuntimeError, match="unknown publish attempt"):
        store.mark_failed(created.operation_id, error_text="temporary")


def test_succeeded_operation_cannot_be_rewound_to_failed_or_effect_applied(
    tmp_path: Path,
) -> None:
    store = PublishJournalStore(tmp_path)
    created, _ = store.create_operation(
        operation_key="github:comment:terminal-rewind",
        operation_kind="github_comment",
        payload={"body": "hello"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )
    store.claim_pending_operations(now_timestamp="2026-03-25T00:30:00Z")
    store.mark_running(created.operation_id)
    succeeded = store.mark_succeeded(created.operation_id, response={"ok": True})
    assert succeeded is not None
    assert succeeded.state == "succeeded"

    assert store.mark_failed(created.operation_id, error_text="late failure") is None
    assert (
        store.mark_effect_applied(
            created.operation_id,
            response={"ok": True},
            error_text="late partial",
        )
        is None
    )
    assert store.get_operation(created.operation_id).state == "succeeded"
