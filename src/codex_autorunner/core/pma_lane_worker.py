from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from .pma_queue import PmaQueue, PmaQueueItem

logger = logging.getLogger(__name__)

PmaLaneExecutor = Callable[[PmaQueueItem], Awaitable[dict[str, Any]]]
PmaLaneResultHook = Callable[
    [PmaQueueItem, dict[str, Any]],
    Optional[Awaitable[None]],
]


class PmaLaneWorker:
    """Background worker that drains a PMA lane queue."""

    def __init__(
        self,
        lane_id: str,
        queue: PmaQueue,
        executor: PmaLaneExecutor,
        *,
        log: Optional[logging.Logger] = None,
        on_result: Optional[PmaLaneResultHook] = None,
    ) -> None:
        self.lane_id = lane_id
        self._queue = queue
        self._executor = executor
        self._log = log or logger
        self._on_result = on_result
        self._task: Optional[asyncio.Task[None]] = None
        self._cancel_event = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        async with self._lock:
            if self.is_running:
                return False
            self._cancel_event = asyncio.Event()
            self._task = asyncio.create_task(self._run())
            self._log.info("PMA lane worker started (lane_id=%s)", self.lane_id)
            return True

    async def stop(self) -> None:
        async with self._lock:
            task = self._task
            if task is None:
                return
            self._cancel_event.set()
        if task is not None:
            try:
                await task
            finally:
                self._log.info("PMA lane worker stopped (lane_id=%s)", self.lane_id)

    async def _run(self) -> None:
        await self._queue.replay_pending(self.lane_id)
        while not self._cancel_event.is_set():
            item = await self._queue.dequeue(self.lane_id)
            if item is None:
                await self._queue.wait_for_lane_item(self.lane_id, self._cancel_event)
                continue

            if self._cancel_event.is_set():
                await self._queue.fail_item(item, "cancelled by lane stop")
                await self._notify(item, {"status": "error", "detail": "lane stopped"})
                continue

            self._log.info(
                "PMA lane item started (lane_id=%s item_id=%s)",
                self.lane_id,
                item.item_id,
            )
            try:
                result = await self._executor(item)
                await self._queue.complete_item(item, result)
                self._log.info(
                    "PMA lane item completed (lane_id=%s item_id=%s status=%s)",
                    self.lane_id,
                    item.item_id,
                    result.get("status") if isinstance(result, dict) else None,
                )
                await self._notify(item, result)
            except Exception as exc:
                self._log.exception("Failed to process PMA queue item %s", item.item_id)
                error_result = {"status": "error", "detail": str(exc)}
                await self._queue.fail_item(item, str(exc))
                self._log.info(
                    "PMA lane item failed (lane_id=%s item_id=%s error=%s)",
                    self.lane_id,
                    item.item_id,
                    str(exc),
                )
                await self._notify(item, error_result)

    async def _notify(self, item: PmaQueueItem, result: dict[str, Any]) -> None:
        if self._on_result is None:
            return
        try:
            maybe = self._on_result(item, result)
            if asyncio.iscoroutine(maybe):
                await maybe
        except Exception:
            self._log.exception(
                "PMA lane result hook failed (lane_id=%s item_id=%s)",
                self.lane_id,
                item.item_id,
            )
