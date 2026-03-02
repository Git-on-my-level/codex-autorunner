from __future__ import annotations

import logging
import threading

from codex_autorunner.core.hub_lifecycle import (
    HubLifecycleWorker,
    LifecycleEventProcessor,
)
from codex_autorunner.core.lifecycle_events import LifecycleEvent, LifecycleEventType


class _StoreStub:
    def __init__(self, events: list[LifecycleEvent]) -> None:
        self.events = events
        self.requested_limits: list[int] = []

    def get_unprocessed(self, *, limit: int = 100) -> list[LifecycleEvent]:
        self.requested_limits.append(limit)
        return self.events[:limit]


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
        logger=logger,
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        processor.process_events(limit=50)

    assert store.requested_limits == [50]
    assert processed_ids == [first.event_id, second.event_id]
    assert "Failed to process lifecycle event" in caplog.text


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
