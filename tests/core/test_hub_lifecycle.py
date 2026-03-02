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
            assert completed.wait(timeout=1.0)
        finally:
            worker.stop()

    assert attempts >= 2
    assert worker.running is False
    assert "Error in lifecycle event processor" in caplog.text
