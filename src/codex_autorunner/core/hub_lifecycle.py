from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol

from .config import HubConfig
from .hub_lifecycle_routing import LifecycleEventRouter
from .hub_topology import RepoSnapshot
from .lifecycle_events import (
    LifecycleEvent,
    LifecycleEventEmitter,
    LifecycleEventStore,
)
from .pma_automation_store import PmaAutomationStore
from .pma_safety import PmaSafetyChecker
from .text_utils import _parse_iso_timestamp

LIFECYCLE_RETRY_METADATA_KEY = "lifecycle_retry"


class LifecycleEventStoreProtocol(Protocol):
    def get_unprocessed(self, *, limit: int = 100) -> list[LifecycleEvent]: ...

    def update_event(
        self,
        event_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        processed: Optional[bool] = None,
    ) -> Optional[LifecycleEvent]: ...

    def mark_processed(self, event_id: str) -> Optional[LifecycleEvent]: ...


@dataclass(frozen=True)
class LifecycleRetryPolicy:
    max_attempts: int = 3
    initial_backoff_seconds: float = 5.0
    max_backoff_seconds: float = 300.0

    def __post_init__(self) -> None:
        max_attempts = max(1, int(self.max_attempts))
        initial_backoff_seconds = max(0.0, float(self.initial_backoff_seconds))
        max_backoff_seconds = max(
            initial_backoff_seconds,
            float(self.max_backoff_seconds),
        )
        object.__setattr__(self, "max_attempts", max_attempts)
        object.__setattr__(
            self,
            "initial_backoff_seconds",
            initial_backoff_seconds,
        )
        object.__setattr__(self, "max_backoff_seconds", max_backoff_seconds)

    def delay_seconds(self, attempts: int) -> float:
        if attempts <= 0:
            return 0.0
        delay = self.initial_backoff_seconds * (2 ** max(0, attempts - 1))
        return float(min(self.max_backoff_seconds, delay))


class LifecycleEventProcessor:
    """Processes unprocessed lifecycle events from a store."""

    def __init__(
        self,
        *,
        store: LifecycleEventStoreProtocol,
        process_event: Callable[[LifecycleEvent], None],
        retry_policy: Optional[LifecycleRetryPolicy] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._store = store
        self._process_event = process_event
        self._retry_policy = retry_policy or LifecycleRetryPolicy()
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._logger = logger or logging.getLogger("codex_autorunner.hub")

    @staticmethod
    def _to_iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _read_attempts(value: Any) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, str):
            try:
                parsed = int(value.strip())
                return max(0, parsed)
            except (TypeError, ValueError):
                return 0
        return 0

    def _retry_metadata(self, event: LifecycleEvent) -> dict[str, Any]:
        data = event.data if isinstance(event.data, dict) else {}
        raw = data.get(LIFECYCLE_RETRY_METADATA_KEY)
        return dict(raw) if isinstance(raw, dict) else {}

    def _should_attempt_now(self, event: LifecycleEvent, *, now: datetime) -> bool:
        metadata = self._retry_metadata(event)
        status = str(metadata.get("status", "")).strip().lower()
        if status == "quarantined":
            self._store.mark_processed(event.event_id)
            return False
        next_retry_at = _parse_iso_timestamp(metadata.get("next_retry_at"))
        if next_retry_at is not None and now < next_retry_at:
            return False
        return True

    def _record_failure(self, event: LifecycleEvent, exc: Exception) -> None:
        failed_at = self._now_fn()
        failed_at_iso = self._to_iso(failed_at)
        current_data = dict(event.data or {})
        metadata = self._retry_metadata(event)
        attempts = self._read_attempts(metadata.get("attempts")) + 1
        first_failed_at = metadata.get("first_failed_at")
        if not isinstance(first_failed_at, str) or not first_failed_at.strip():
            first_failed_at = failed_at_iso

        metadata.update(
            {
                "attempts": attempts,
                "max_attempts": self._retry_policy.max_attempts,
                "first_failed_at": first_failed_at,
                "last_failed_at": failed_at_iso,
                "last_error": str(exc),
            }
        )

        if attempts >= self._retry_policy.max_attempts:
            metadata.update(
                {
                    "status": "quarantined",
                    "quarantined": True,
                    "quarantine_reason": "max_attempts_exceeded",
                    "dead_lettered_at": failed_at_iso,
                    "next_retry_at": None,
                    "backoff_seconds": None,
                }
            )
            current_data[LIFECYCLE_RETRY_METADATA_KEY] = metadata
            self._store.update_event(
                event.event_id,
                data=current_data,
                processed=True,
            )
            self._logger.error(
                "Quarantined lifecycle event %s after %s/%s attempts: %s",
                event.event_id,
                attempts,
                self._retry_policy.max_attempts,
                exc,
            )
            return

        backoff_seconds = self._retry_policy.delay_seconds(attempts)
        next_retry_at = failed_at + timedelta(seconds=backoff_seconds)
        metadata.update(
            {
                "status": "retry_scheduled",
                "quarantined": False,
                "next_retry_at": self._to_iso(next_retry_at),
                "backoff_seconds": backoff_seconds,
            }
        )
        current_data[LIFECYCLE_RETRY_METADATA_KEY] = metadata
        self._store.update_event(
            event.event_id,
            data=current_data,
            processed=False,
        )
        self._logger.warning(
            "Scheduled lifecycle event retry %s attempt %s/%s in %.2fs: %s",
            event.event_id,
            attempts,
            self._retry_policy.max_attempts,
            backoff_seconds,
            exc,
        )

    def process_events(self, *, limit: int = 100) -> int:
        events = self._store.get_unprocessed(limit=limit)
        if not events:
            return 0
        processed = 0
        for event in events:
            if not self._should_attempt_now(event, now=self._now_fn()):
                continue
            try:
                self._process_event(event)
                processed += 1
            except (
                Exception
            ) as exc:  # intentional: process_event is a user-provided callback
                self._record_failure(event, exc)
                processed += 1
        return processed


class HubLifecycleWorker:
    """Threaded poller for lifecycle event processing with adaptive backoff."""

    _BACKOFF_GROW_FACTOR = 1.5

    def __init__(
        self,
        *,
        process_once: Callable[[], Optional[bool]],
        poll_interval_seconds: float = 5.0,
        max_poll_interval_seconds: Optional[float] = None,
        join_timeout_seconds: float = 2.0,
        thread_name: str = "lifecycle-event-processor",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._process_once = process_once
        self._base_poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._max_poll_interval_seconds = max(
            self._base_poll_interval_seconds,
            float(
                max_poll_interval_seconds
                if max_poll_interval_seconds is not None
                else self._base_poll_interval_seconds * 6
            ),
        )
        self._join_timeout_seconds = join_timeout_seconds
        self._thread_name = thread_name
        self._logger = logger or logging.getLogger("codex_autorunner.hub")
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._idle_streak = 0

    @property
    def running(self) -> bool:
        with self._thread_lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def _current_interval(self) -> float:
        if self._idle_streak <= 0:
            return self._base_poll_interval_seconds
        grown = self._base_poll_interval_seconds * (
            self._BACKOFF_GROW_FACTOR**self._idle_streak
        )
        return min(grown, self._max_poll_interval_seconds)

    def wake(self) -> None:
        self._idle_streak = 0
        self._wake_event.set()

    def start(self) -> None:
        with self._thread_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._idle_streak = 0

            def _process_loop() -> None:
                current_interval = self._base_poll_interval_seconds
                while not self._stop_event.is_set():
                    deadline = time.monotonic() + current_interval
                    while True:
                        if self._stop_event.is_set():
                            return
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        chunk = min(remaining, 0.5)
                        if self._wake_event.wait(timeout=chunk):
                            self._wake_event.clear()
                            break
                    if self._stop_event.is_set():
                        return
                    try:
                        result = self._process_once()
                        if result:
                            self._idle_streak = 0
                            current_interval = self._base_poll_interval_seconds
                        else:
                            self._idle_streak += 1
                            current_interval = self._current_interval()
                    except (
                        Exception
                    ) as exc:  # intentional: process_once is a user-provided callback
                        if _is_unrecoverable_lifecycle_error(exc):
                            self._logger.exception(
                                "Stopping lifecycle event processor after unrecoverable error"
                            )
                            self._stop_event.set()
                            break
                        self._logger.exception("Error in lifecycle event processor")

            thread = threading.Thread(
                target=_process_loop,
                daemon=True,
                name=self._thread_name,
            )
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        with self._thread_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
        thread.join(timeout=self._join_timeout_seconds)
        with self._thread_lock:
            if self._thread is thread:
                self._thread = None


class HubLifecycleOrchestrator:
    """Consolidates lifecycle worker startup, shutdown, retry orchestration,
    event routing, and outbox wiring into a single ownership boundary."""

    def __init__(
        self,
        hub_config: HubConfig,
        *,
        list_repos_fn: Callable[[], list[RepoSnapshot]],
        ensure_pma_automation_store_fn: Callable[[], PmaAutomationStore],
        ensure_pma_safety_checker_fn: Callable[[], PmaSafetyChecker],
        run_coroutine_fn: Callable[[Any], Any],
        process_scm_polls_fn: Optional[Callable[[], dict[str, int]]] = None,
        process_pma_timers_fn: Optional[Callable[[], int]] = None,
        drain_pma_wakeups_fn: Optional[Callable[[], int]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._hub_config = hub_config
        self._lifecycle_emitter = LifecycleEventEmitter(hub_config.root)
        self._lifecycle_router = LifecycleEventRouter(
            hub_config=hub_config,
            lifecycle_store=self.lifecycle_store,
            list_repos_fn=list_repos_fn,
            ensure_pma_automation_store_fn=ensure_pma_automation_store_fn,
            ensure_pma_safety_checker_fn=ensure_pma_safety_checker_fn,
            run_coroutine_fn=run_coroutine_fn,
            logger=logger,
        )
        self._lifecycle_worker = HubLifecycleWorker(
            process_once=self._process_lifecycle_event_cycle,
            poll_interval_seconds=5.0,
            join_timeout_seconds=2.0,
            thread_name="lifecycle-event-processor",
            logger=logger,
        )
        self._process_scm_polls_fn = process_scm_polls_fn
        self._process_pma_timers_fn = process_pma_timers_fn
        self._drain_pma_wakeups_fn = drain_pma_wakeups_fn
        self._process_event_fn: Callable[[LifecycleEvent], None] = (
            self._process_lifecycle_event
        )
        self._lifecycle_event_processor = LifecycleEventProcessor(
            store=self.lifecycle_store,
            process_event=lambda event: self._process_event_fn(event),
            retry_policy=self._build_lifecycle_retry_policy(),
            logger=logger,
        )
        self._logger = logger or logging.getLogger("codex_autorunner.hub")

    @property
    def lifecycle_emitter(self) -> LifecycleEventEmitter:
        return self._lifecycle_emitter

    @property
    def lifecycle_store(self) -> LifecycleEventStore:
        return self._lifecycle_emitter._store

    def startup(self) -> None:
        self._lifecycle_worker.start()

    def shutdown(self) -> None:
        self._lifecycle_worker.stop()
        from ..tickets.outbox import set_lifecycle_emitter

        set_lifecycle_emitter(None)

    def wire_outbox_lifecycle(self) -> None:
        from ..tickets.outbox import set_lifecycle_emitter

        if not self._hub_config.pma.enabled:
            set_lifecycle_emitter(None)
            return

        def _emit_outbox_event(
            event_type: str,
            repo_id: str,
            run_id: str,
            data: dict[str, Any],
            origin: str,
        ) -> None:
            if event_type == "dispatch_created":
                self._lifecycle_emitter.emit_dispatch_created(
                    repo_id, run_id, data=data, origin=origin
                )
                self._lifecycle_worker.wake()

        set_lifecycle_emitter(_emit_outbox_event)

    def process_lifecycle_events(self) -> int:
        processed = self._lifecycle_event_processor.process_events(limit=100)
        try:
            self._drain_pma_wakeups()
        except Exception:
            self._logger.exception("Failed draining PMA automation wake-ups")
        return processed

    def trigger_pma_from_lifecycle_event(self, event: LifecycleEvent) -> None:
        self._process_lifecycle_event(event)

    def wake_worker(self) -> None:
        self._lifecycle_worker.wake()

    def _process_lifecycle_event(self, event: LifecycleEvent) -> None:
        self._lifecycle_router.route_event(event)

    def _process_lifecycle_event_cycle(self) -> bool:
        productive = False
        if self.process_lifecycle_events() > 0:
            productive = True
        scm_counts = self._process_scm_polls()
        if scm_counts.get("polled", 0) > 0 or scm_counts.get("events_emitted", 0) > 0:
            productive = True
        timer_count = self._process_pma_timers()
        if timer_count > 0:
            productive = True
        wakeup_count = self._drain_pma_wakeups()
        if wakeup_count > 0:
            productive = True
        return productive

    def _process_scm_polls(self) -> dict[str, int]:
        if self._process_scm_polls_fn is not None:
            return self._process_scm_polls_fn()
        return {"polled": 0, "events_emitted": 0}

    def _process_pma_timers(self) -> int:
        if self._process_pma_timers_fn is not None:
            return self._process_pma_timers_fn()
        return 0

    def _drain_pma_wakeups(self) -> int:
        if self._drain_pma_wakeups_fn is not None:
            return self._drain_pma_wakeups_fn()
        return 0

    def _build_lifecycle_retry_policy(self) -> LifecycleRetryPolicy:
        raw = getattr(self._hub_config, "raw", {})
        pma_config = raw.get("pma", {}) if isinstance(raw, dict) else {}
        if not isinstance(pma_config, dict):
            pma_config = {}

        def _read_int(key: str, fallback: int, *, minimum: int = 0) -> int:
            raw_value = pma_config.get(key, fallback)
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                return fallback
            return value if value >= minimum else fallback

        def _read_float(key: str, fallback: float, *, minimum: float = 0.0) -> float:
            raw_value = pma_config.get(key, fallback)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return fallback
            return value if value >= minimum else fallback

        max_attempts = _read_int("lifecycle_retry_max_attempts", 3, minimum=1)
        initial_backoff_seconds = _read_float(
            "lifecycle_retry_initial_backoff_seconds",
            5.0,
            minimum=0.0,
        )
        max_backoff_seconds = _read_float(
            "lifecycle_retry_max_backoff_seconds",
            300.0,
            minimum=0.0,
        )
        if max_backoff_seconds < initial_backoff_seconds:
            max_backoff_seconds = initial_backoff_seconds

        return LifecycleRetryPolicy(
            max_attempts=max_attempts,
            initial_backoff_seconds=initial_backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )


def _is_unrecoverable_lifecycle_error(exc: Exception) -> bool:
    return "schema is newer than this build supports" in str(exc).lower()
