from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol

from .lifecycle_events import LifecycleEvent

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
    def _parse_iso_datetime(value: Any) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

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
        next_retry_at = self._parse_iso_datetime(metadata.get("next_retry_at"))
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

    def process_events(self, *, limit: int = 100) -> None:
        events = self._store.get_unprocessed(limit=limit)
        if not events:
            return
        for event in events:
            if not self._should_attempt_now(event, now=self._now_fn()):
                continue
            try:
                self._process_event(event)
            except (
                Exception
            ) as exc:  # intentional: process_event is a user-provided callback
                self._record_failure(event, exc)


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
        self._thread_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        with self._thread_lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._thread_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            self._stop_event.clear()

            def _process_loop() -> None:
                while not self._stop_event.wait(self._poll_interval_seconds):
                    try:
                        self._process_once()
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


def _is_unrecoverable_lifecycle_error(exc: Exception) -> bool:
    return "schema is newer than this build supports" in str(exc).lower()
