from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Protocol

from .lifecycle_events import LifecycleEvent


class LifecycleEventStoreProtocol(Protocol):
    def get_unprocessed(self, *, limit: int = 100) -> list[LifecycleEvent]: ...


class LifecycleEventProcessor:
    """Processes unprocessed lifecycle events from a store."""

    def __init__(
        self,
        *,
        store: LifecycleEventStoreProtocol,
        process_event: Callable[[LifecycleEvent], None],
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._store = store
        self._process_event = process_event
        self._logger = logger or logging.getLogger("codex_autorunner.hub")

    def process_events(self, *, limit: int = 100) -> None:
        events = self._store.get_unprocessed(limit=limit)
        if not events:
            return
        for event in events:
            try:
                self._process_event(event)
            except Exception as exc:
                self._logger.exception(
                    "Failed to process lifecycle event %s: %s", event.event_id, exc
                )


class HubLifecycleWorker:
    """Threaded poller for lifecycle event processing."""

    def __init__(
        self,
        *,
        process_once: Callable[[], None],
        poll_interval_seconds: float = 5.0,
        join_timeout_seconds: float = 2.0,
        thread_name: str = "lifecycle-event-processor",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._process_once = process_once
        self._poll_interval_seconds = poll_interval_seconds
        self._join_timeout_seconds = join_timeout_seconds
        self._thread_name = thread_name
        self._logger = logger or logging.getLogger("codex_autorunner.hub")
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        if self._thread is not None:
            return

        def _process_loop() -> None:
            while not self._stop_event.wait(self._poll_interval_seconds):
                try:
                    self._process_once()
                except Exception:
                    self._logger.exception("Error in lifecycle event processor")

        self._thread = threading.Thread(
            target=_process_loop,
            daemon=True,
            name=self._thread_name,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=self._join_timeout_seconds)
        self._thread = None
