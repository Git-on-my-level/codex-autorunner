from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from codex_autorunner.core.hub_lifecycle import (
    HubLifecycleWorker,
    LifecycleEventProcessor,
    LifecycleRetryPolicy,
)
from codex_autorunner.core.lifecycle_events import LifecycleEvent, LifecycleEventType
from tests.support.waits import wait_for_thread_event


class _StoreStub:
    def __init__(self, events: list[LifecycleEvent]) -> None:
        self.events = events
        self.requested_limits: list[int] = []
        self.updated_event_ids: list[str] = []
        self.marked_processed_ids: list[str] = []

    def get_unprocessed(self, *, limit: int = 100) -> list[LifecycleEvent]:
        self.requested_limits.append(limit)
        return [event for event in self.events if not event.processed][:limit]

    def update_event(
        self,
        event_id: str,
        *,
        data: dict | None = None,
        processed: bool | None = None,
    ) -> LifecycleEvent | None:
        for event in self.events:
            if event.event_id != event_id:
                continue
            if data is not None:
                event.data = dict(data)
            if processed is not None:
                event.processed = bool(processed)
            self.updated_event_ids.append(event_id)
            return event
        return None

    def mark_processed(self, event_id: str) -> LifecycleEvent | None:
        event = self.update_event(event_id, processed=True)
        if event is not None:
            self.marked_processed_ids.append(event_id)
        return event


def test_lifecycle_event_processor_continues_after_event_failure(caplog) -> None:
    first = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_FAILED,
        repo_id="repo-1",
        run_id="run-1",
    )
    second = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_FAILED,
        repo_id="repo-2",
        run_id="run-2",
    )
    store = _StoreStub([first, second])
    processed_ids: list[str] = []
    logger = logging.getLogger("test.hub_lifecycle.processor")

    def _process_event(event: LifecycleEvent) -> None:
        processed_ids.append(event.event_id)
        if event.event_id == first.event_id:
            raise RuntimeError("boom")

    processor = LifecycleEventProcessor(
        store=store,
        process_event=_process_event,
        retry_policy=LifecycleRetryPolicy(
            max_attempts=3,
            initial_backoff_seconds=0.0,
            max_backoff_seconds=0.0,
        ),
        logger=logger,
    )

    with caplog.at_level(logging.WARNING, logger=logger.name):
        processor.process_events(limit=50)

    assert store.requested_limits == [50]
    assert processed_ids == [first.event_id, second.event_id]
    retry_meta = first.data.get("lifecycle_retry")
    assert isinstance(retry_meta, dict)
    assert retry_meta.get("attempts") == 1
    assert retry_meta.get("status") == "retry_scheduled"
    assert first.processed is False
    assert "Scheduled lifecycle event retry" in caplog.text


def test_lifecycle_event_processor_quarantines_after_max_attempts() -> None:
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_FAILED,
        repo_id="repo-1",
        run_id="run-1",
    )
    store = _StoreStub([event])

    def _now_fn() -> datetime:
        return datetime(2026, 3, 1, tzinfo=timezone.utc)

    processor = LifecycleEventProcessor(
        store=store,
        process_event=lambda _event: (_ for _ in ()).throw(RuntimeError("boom")),
        retry_policy=LifecycleRetryPolicy(
            max_attempts=2,
            initial_backoff_seconds=0.0,
            max_backoff_seconds=0.0,
        ),
        now_fn=_now_fn,
    )

    processor.process_events()
    first_attempt_meta = event.data.get("lifecycle_retry")
    assert isinstance(first_attempt_meta, dict)
    assert first_attempt_meta.get("attempts") == 1
    assert first_attempt_meta.get("status") == "retry_scheduled"
    assert event.processed is False

    processor.process_events()
    second_attempt_meta = event.data.get("lifecycle_retry")
    assert isinstance(second_attempt_meta, dict)
    assert second_attempt_meta.get("attempts") == 2
    assert second_attempt_meta.get("status") == "quarantined"
    assert second_attempt_meta.get("quarantine_reason") == "max_attempts_exceeded"
    assert second_attempt_meta.get("dead_lettered_at")
    assert event.processed is True


def test_lifecycle_event_processor_respects_retry_backoff_window() -> None:
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_FAILED,
        repo_id="repo-1",
        run_id="run-1",
        data={
            "lifecycle_retry": {
                "attempts": 1,
                "status": "retry_scheduled",
                "next_retry_at": (now + timedelta(seconds=60)).isoformat(),
            }
        },
    )
    store = _StoreStub([event])
    attempts = 0

    def _process_event(_event: LifecycleEvent) -> None:
        nonlocal attempts
        attempts += 1

    processor = LifecycleEventProcessor(
        store=store,
        process_event=_process_event,
        retry_policy=LifecycleRetryPolicy(
            max_attempts=3,
            initial_backoff_seconds=1.0,
            max_backoff_seconds=10.0,
        ),
        now_fn=lambda: now,
    )

    processor.process_events()
    assert attempts == 0
    assert event.processed is False


def test_hub_lifecycle_worker_logs_and_keeps_polling_after_failure(caplog) -> None:
    attempts = 0
    completed = threading.Event()
    logger = logging.getLogger("test.hub_lifecycle.worker")

    def _process_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        completed.set()

    worker = HubLifecycleWorker(
        process_once=_process_once,
        poll_interval_seconds=0.01,
        join_timeout_seconds=0.5,
        logger=logger,
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        worker.start()
        try:
            wait_for_thread_event(
                completed,
                timeout_seconds=1.0,
                description="lifecycle worker retry cycle to complete",
            )
        finally:
            worker.stop()

    assert attempts >= 2
    assert worker.running is False
    assert "Error in lifecycle event processor" in caplog.text


def test_hub_lifecycle_worker_stops_after_unrecoverable_schema_error(caplog) -> None:
    attempts = 0
    attempted = threading.Event()
    logger = logging.getLogger("test.hub_lifecycle.worker.schema")

    def _process_once() -> None:
        nonlocal attempts
        attempts += 1
        attempted.set()
        raise RuntimeError(
            "orchestration.sqlite3 schema is newer than this build supports"
        )

    worker = HubLifecycleWorker(
        process_once=_process_once,
        poll_interval_seconds=0.01,
        join_timeout_seconds=0.5,
        logger=logger,
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        worker.start()
        try:
            wait_for_thread_event(
                attempted,
                timeout_seconds=1.0,
                description="lifecycle worker to attempt processing once",
            )
        finally:
            worker.stop()

    assert attempts >= 1
    assert worker.running is False
    assert "Stopping lifecycle event processor after unrecoverable error" in caplog.text


def test_hub_lifecycle_worker_stop_clears_dead_thread_after_self_termination() -> None:
    logger = logging.getLogger("test.hub_lifecycle.worker.stop")

    def _process_once() -> None:
        raise RuntimeError(
            "orchestration.sqlite3 schema is newer than this build supports"
        )

    worker = HubLifecycleWorker(
        process_once=_process_once,
        poll_interval_seconds=0.01,
        join_timeout_seconds=0.5,
        logger=logger,
    )

    worker.start()
    thread = worker._thread
    assert thread is not None
    thread.join(timeout=1.0)
    assert thread.is_alive() is False

    worker.stop()

    assert worker._thread is None
    assert worker.running is False


def test_hub_lifecycle_worker_adaptive_backoff_on_idle() -> None:
    call_count = 0
    completed = threading.Event()
    logger = logging.getLogger("test.hub_lifecycle.worker.backoff")

    def _process_once():
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            completed.set()
        return False

    worker = HubLifecycleWorker(
        process_once=_process_once,
        poll_interval_seconds=0.01,
        max_poll_interval_seconds=10.0,
        join_timeout_seconds=0.5,
        logger=logger,
    )

    worker.start()
    try:
        wait_for_thread_event(
            completed,
            timeout_seconds=5.0,
            description="lifecycle worker idle backoff cycles",
        )
    finally:
        worker.stop()

    assert call_count >= 3
    assert worker._idle_streak > 0
    assert worker._current_interval() > worker._base_poll_interval_seconds


def test_hub_lifecycle_worker_resets_backoff_on_productive() -> None:
    call_count = 0
    completed = threading.Event()
    logger = logging.getLogger("test.hub_lifecycle.worker.reset")

    def _process_once():
        nonlocal call_count
        call_count += 1
        if call_count >= 4:
            completed.set()
        return call_count == 3

    worker = HubLifecycleWorker(
        process_once=_process_once,
        poll_interval_seconds=0.01,
        max_poll_interval_seconds=0.5,
        join_timeout_seconds=0.5,
        logger=logger,
    )

    worker.start()
    try:
        wait_for_thread_event(
            completed,
            timeout_seconds=5.0,
            description="lifecycle worker backoff reset cycle",
        )
    finally:
        worker.stop()

    assert call_count >= 4


def test_hub_lifecycle_worker_wake_resets_idle_streak() -> None:
    call_count = 0
    first_call = threading.Event()
    wake_done = threading.Event()
    second_call = threading.Event()
    streak_after_wake = None
    logger = logging.getLogger("test.hub_lifecycle.worker.wake")

    def _process_once():
        nonlocal call_count, streak_after_wake
        call_count += 1
        if call_count == 1:
            first_call.set()
            wake_done.wait(timeout=2.0)
            streak_after_wake = worker._idle_streak
        elif call_count == 2:
            second_call.set()
        return False

    worker = HubLifecycleWorker(
        process_once=_process_once,
        poll_interval_seconds=0.01,
        max_poll_interval_seconds=5.0,
        join_timeout_seconds=0.5,
        logger=logger,
    )

    worker.start()
    try:
        wait_for_thread_event(
            first_call,
            timeout_seconds=1.0,
            description="lifecycle worker first call",
        )
        worker._idle_streak = 5
        worker.wake()
        wake_done.set()
        wait_for_thread_event(
            second_call,
            timeout_seconds=1.0,
            description="lifecycle worker second call after wake",
        )
    finally:
        worker.stop()

    assert call_count >= 2
    assert streak_after_wake == 0
