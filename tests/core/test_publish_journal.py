from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.publish_journal import PublishJournalStore


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
