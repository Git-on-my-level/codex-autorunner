from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.publish_executor import (
    PublishExecutorRegistry,
    PublishOperationProcessor,
    TerminalPublishError,
    drain_pending_publish_operations,
)
from codex_autorunner.core.publish_journal import PublishJournalStore


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class _QueuedClock:
    def __init__(self, *timestamps: str) -> None:
        self._timestamps = [_parse_utc(value) for value in timestamps]
        self._index = 0

    def __call__(self) -> datetime:
        if not self._timestamps:
            raise AssertionError("clock requires at least one timestamp")
        index = min(self._index, len(self._timestamps) - 1)
        self._index += 1
        return self._timestamps[index]


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


def test_drain_pending_publish_operations_marks_success_and_does_not_replay_same_call(
    tmp_path: Path,
) -> None:
    store = PublishJournalStore(tmp_path)
    created, _ = store.create_operation(
        operation_key="notify:thread-1",
        operation_kind="notify_chat",
        payload={"body": "ready"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    calls: list[tuple[str, int, str]] = []

    def executor(operation):
        calls.append((operation.operation_id, operation.attempt_count, operation.state))
        return {"delivered": True, "remote_id": "msg-123"}

    processed = drain_pending_publish_operations(
        store,
        executor_registry=PublishExecutorRegistry({"notify_chat": executor}),
        limit=10,
        now_fn=_QueuedClock("2026-03-25T00:00:00Z"),
    )
    assert [operation.operation_id for operation in processed] == [created.operation_id]
    assert processed[0].state == "succeeded"
    assert processed[0].response == {"delivered": True, "remote_id": "msg-123"}
    assert calls == [(created.operation_id, 1, "running")]

    repeated = drain_pending_publish_operations(
        store,
        executor_registry=PublishExecutorRegistry({"notify_chat": executor}),
        limit=10,
        now_fn=_QueuedClock("2026-03-25T00:01:00Z"),
    )
    assert repeated == []
    assert calls == [(created.operation_id, 1, "running")]

    attempts = _attempt_rows(tmp_path, created.operation_id)
    assert len(attempts) == 1
    assert attempts[0]["state"] == "succeeded"
    assert json.loads(str(attempts[0]["response_json"])) == {
        "delivered": True,
        "remote_id": "msg-123",
    }


def test_process_now_applies_bounded_retry_schedule_for_generic_failures(
    tmp_path: Path,
) -> None:
    store = PublishJournalStore(tmp_path)
    created, _ = store.create_operation(
        operation_key="comment:pr-1",
        operation_kind="post_pr_comment",
        payload={"body": "hello"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    call_count = 0

    def failing_executor(_operation):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("temporary outage")

    processor = PublishOperationProcessor(
        store,
        executors={"post_pr_comment": failing_executor},
        now_fn=_QueuedClock(
            "2026-03-25T00:00:00Z",
            "2026-03-25T00:00:00Z",
            "2026-03-25T00:00:30Z",
            "2026-03-25T00:05:30Z",
        ),
    )

    first = processor.process_now(limit=10)
    assert call_count == 1
    assert [operation.operation_id for operation in first] == [created.operation_id]
    assert first[0].state == "pending"
    assert first[0].attempt_count == 1
    assert first[0].next_attempt_at == "2026-03-25T00:00:00Z"

    second = processor.process_now(limit=10)
    assert call_count == 2
    assert second[0].state == "pending"
    assert second[0].attempt_count == 2
    assert second[0].next_attempt_at == "2026-03-25T00:00:30Z"

    third = processor.process_now(limit=10)
    assert call_count == 3
    assert third[0].state == "pending"
    assert third[0].attempt_count == 3
    assert third[0].next_attempt_at == "2026-03-25T00:05:30Z"

    fourth = processor.process_now(limit=10)
    assert call_count == 4
    assert fourth[0].state == "failed"
    assert fourth[0].attempt_count == 4
    assert fourth[0].next_attempt_at is None
    assert fourth[0].last_error_text == "RuntimeError: temporary outage"

    attempts = _attempt_rows(tmp_path, created.operation_id)
    assert len(attempts) == 4
    assert [attempt["state"] for attempt in attempts] == [
        "failed",
        "failed",
        "failed",
        "failed",
    ]


def test_process_now_marks_terminal_failure_without_retry(
    tmp_path: Path,
) -> None:
    store = PublishJournalStore(tmp_path)
    created, _ = store.create_operation(
        operation_key="comment:pr-terminal",
        operation_kind="post_pr_comment",
        payload={"body": "bad request"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    call_count = 0

    def terminal_executor(_operation):
        nonlocal call_count
        call_count += 1
        raise TerminalPublishError("permanent rejection")

    processor = PublishOperationProcessor(
        store,
        executors={"post_pr_comment": terminal_executor},
        now_fn=_QueuedClock("2026-03-25T00:00:00Z", "2026-03-25T00:01:00Z"),
    )

    processed = processor.process_now(limit=10)
    assert call_count == 1
    assert [operation.operation_id for operation in processed] == [created.operation_id]
    assert processed[0].state == "failed"
    assert processed[0].next_attempt_at is None
    assert processed[0].last_error_text == "TerminalPublishError: permanent rejection"

    repeated = processor.process_now(limit=10)
    assert repeated == []
    assert call_count == 1


def test_process_now_replays_pending_operation_after_processor_restart(
    tmp_path: Path,
) -> None:
    first_store = PublishJournalStore(tmp_path)
    created, _ = first_store.create_operation(
        operation_key="enqueue:thread-1",
        operation_kind="enqueue_managed_turn",
        payload={"thread_target_id": "thread-1"},
        next_attempt_at="2026-03-25T00:00:00Z",
    )

    def first_executor(_operation):
        raise RuntimeError("transient failure")

    first_processor = PublishOperationProcessor(
        first_store,
        executors={"enqueue_managed_turn": first_executor},
        now_fn=_QueuedClock("2026-03-25T00:00:00Z"),
    )
    first_result = first_processor.process_now(limit=10)
    assert first_result[0].state == "pending"
    assert first_result[0].attempt_count == 1
    assert first_result[0].next_attempt_at == "2026-03-25T00:00:00Z"

    second_store = PublishJournalStore(tmp_path)

    def second_executor(operation):
        return {"queued": True, "operation_id": operation.operation_id}

    second_processor = PublishOperationProcessor(
        second_store,
        executors={"enqueue_managed_turn": second_executor},
        now_fn=_QueuedClock("2026-03-25T00:00:00Z"),
    )
    second_result = second_processor.process_now(limit=10)
    assert [operation.operation_id for operation in second_result] == [
        created.operation_id
    ]
    assert second_result[0].state == "succeeded"
    assert second_result[0].attempt_count == 2
    assert second_result[0].response == {
        "queued": True,
        "operation_id": created.operation_id,
    }

    attempts = _attempt_rows(tmp_path, created.operation_id)
    assert len(attempts) == 2
    assert [attempt["state"] for attempt in attempts] == ["failed", "succeeded"]
