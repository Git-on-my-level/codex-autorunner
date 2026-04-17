from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, TypeVar

WorkResultT = TypeVar("WorkResultT")


class BackgroundRunnerSaturated(RuntimeError):
    def __init__(self, *, max_workers: int, acquire_timeout_seconds: float) -> None:
        super().__init__("background runner saturated")
        self.max_workers = max_workers
        self.acquire_timeout_seconds = acquire_timeout_seconds


class BoundedBackgroundRunner:
    """Bound concurrent background work so timeouts do not leak unbounded threads."""

    def __init__(
        self,
        *,
        max_workers: int,
        saturation_wait_seconds: float = 0.05,
        thread_name_prefix: str,
    ) -> None:
        self._max_workers = max(1, int(max_workers))
        self._saturation_wait_seconds = max(0.0, float(saturation_wait_seconds))
        self._slots = threading.BoundedSemaphore(self._max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix=thread_name_prefix,
        )

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def saturation_wait_seconds(self) -> float:
        return self._saturation_wait_seconds

    def submit(
        self,
        work: Callable[[], WorkResultT],
        *,
        timeout_seconds: float,
    ) -> Future[WorkResultT]:
        acquire_timeout = min(
            max(0.0, float(timeout_seconds)),
            self._saturation_wait_seconds,
        )
        if not self._slots.acquire(timeout=acquire_timeout):
            raise BackgroundRunnerSaturated(
                max_workers=self._max_workers,
                acquire_timeout_seconds=acquire_timeout,
            )

        released = False
        release_lock = threading.Lock()

        def _release_slot(_future: Future[WorkResultT]) -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
            self._slots.release()

        try:
            future = self._executor.submit(work)
        except BaseException:
            _release_slot(Future())
            raise
        future.add_done_callback(_release_slot)
        return future

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)


__all__ = [
    "BackgroundRunnerSaturated",
    "BoundedBackgroundRunner",
]
