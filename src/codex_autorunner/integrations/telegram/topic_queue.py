from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TypeVar, cast

logger = logging.getLogger("codex_autorunner.integrations.telegram.topic_queue")

T = TypeVar("T")
_QUEUE_STOP = object()


@dataclass
class _TopicQueueEntry:
    work: Callable[[], Awaitable[Any]]
    future: Optional[asyncio.Future[Any]] = None
    item_id: Optional[str] = None
    item_label: Optional[str] = None


class TopicQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._worker: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._current_task: Optional[asyncio.Task[Any]] = None
        self._cancel_active_requested = False

    def pending(self) -> int:
        return self._queue.qsize()

    def pending_item_ids(self) -> list[str]:
        item_ids: list[str] = []
        for item in list(getattr(self._queue, "_queue", ())):
            if item is _QUEUE_STOP:
                continue
            entry = cast(_TopicQueueEntry, item)
            if isinstance(entry.item_id, str) and entry.item_id:
                item_ids.append(entry.item_id)
        return item_ids

    def pending_items(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for item in list(getattr(self._queue, "_queue", ())):
            if item is _QUEUE_STOP:
                continue
            entry = cast(_TopicQueueEntry, item)
            if not isinstance(entry.item_id, str) or not entry.item_id:
                continue
            payload = {"item_id": entry.item_id}
            if isinstance(entry.item_label, str) and entry.item_label:
                payload["preview"] = entry.item_label
            items.append(payload)
        return items

    async def join_idle(self) -> None:
        """Block until the queue has no backlog and no in-flight work item."""

        while self.pending() > 0 or (
            self._current_task is not None and not self._current_task.done()
        ):
            await asyncio.sleep(0)

    def cancel_active(self) -> bool:
        task = self._current_task
        if task is None or task.done():
            return False
        self._cancel_active_requested = True
        task.cancel()
        return True

    def cancel_pending(self) -> int:
        cancelled = 0
        requeue_stop = False
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if item is _QUEUE_STOP:
                    requeue_stop = True
                    continue
                entry = cast(_TopicQueueEntry, item)
                future = entry.future
                if future is None:
                    cancelled += 1
                    continue
                if not future.done():
                    future.cancel()
                    cancelled += 1
            finally:
                self._queue.task_done()
        if requeue_stop:
            try:
                self._queue.put_nowait(_QUEUE_STOP)
            except asyncio.QueueFull:
                pass
        return cancelled

    def cancel_pending_item(self, item_id: str) -> bool:
        if not isinstance(item_id, str) or not item_id:
            return False
        entries: list[_TopicQueueEntry] = []
        cancelled = False
        requeue_stop = False
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if item is _QUEUE_STOP:
                    requeue_stop = True
                    continue
                entry = cast(_TopicQueueEntry, item)
                if entry.item_id == item_id and not cancelled:
                    future = entry.future
                    if future is not None and not future.done():
                        future.cancel()
                    cancelled = True
                    continue
                entries.append(entry)
            finally:
                self._queue.task_done()
        for entry in entries:
            self._queue.put_nowait(entry)
        if requeue_stop:
            self._queue.put_nowait(_QUEUE_STOP)
        return cancelled

    def promote_pending_item(self, item_id: str) -> bool:
        if not isinstance(item_id, str) or not item_id:
            return False
        entries: list[_TopicQueueEntry] = []
        target: Optional[_TopicQueueEntry] = None
        requeue_stop = False
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if item is _QUEUE_STOP:
                    requeue_stop = True
                    continue
                entry = cast(_TopicQueueEntry, item)
                if target is None and entry.item_id == item_id:
                    target = entry
                    continue
                entries.append(entry)
            finally:
                self._queue.task_done()
        if target is not None:
            self._queue.put_nowait(target)
        for entry in entries:
            self._queue.put_nowait(entry)
        if requeue_stop:
            self._queue.put_nowait(_QUEUE_STOP)
        return target is not None

    def enqueue_detached(
        self,
        work: Callable[[], Awaitable[Any]],
        *,
        item_id: Optional[str] = None,
        item_label: Optional[str] = None,
    ) -> Optional[str]:
        if self._closed:
            raise RuntimeError("topic queue is closed")
        self._queue.put_nowait(
            _TopicQueueEntry(work=work, item_id=item_id, item_label=item_label)
        )
        self._ensure_worker()
        return item_id

    async def enqueue(self, work: Callable[[], Awaitable[T]]) -> T:
        if self._closed:
            raise RuntimeError("topic queue is closed")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        await self._queue.put(_TopicQueueEntry(work=work, future=future))
        self._ensure_worker()
        return await future

    async def close(self) -> None:
        self._closed = True
        if self._worker is None or self._worker.done():
            return
        await self._queue.put(_QUEUE_STOP)
        await self._worker

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _QUEUE_STOP:
                    return
                entry = cast(_TopicQueueEntry, item)
                future = entry.future
                if future is not None and future.cancelled():
                    continue
                try:
                    self._current_task = asyncio.create_task(entry.work())
                    result: Any = await self._current_task
                except asyncio.CancelledError:
                    if self._cancel_active_requested:
                        self._cancel_active_requested = False
                        if future is not None and not future.cancelled():
                            future.cancel()
                    else:
                        if (
                            self._current_task is not None
                            and not self._current_task.done()
                        ):
                            self._current_task.cancel()
                        raise
                except (
                    Exception
                ) as exc:  # intentional: arbitrary async work must not crash worker
                    if future is None:
                        logger.warning(
                            "telegram detached topic queue entry failed", exc_info=exc
                        )
                    elif not future.cancelled():
                        future.set_exception(exc)
                else:
                    if future is not None and not future.cancelled():
                        future.set_result(result)
            finally:
                self._current_task = None
                self._cancel_active_requested = False
                self._queue.task_done()


@dataclass
class TopicRuntime:
    queue: TopicQueue = dataclasses.field(default_factory=TopicQueue)
    current_turn_id: Optional[str] = None
    current_turn_key: Optional[tuple[str, str]] = None
    pending_request_id: Optional[str] = None
    interrupt_requested: bool = False
    interrupt_message_id: Optional[int] = None
    interrupt_turn_id: Optional[str] = None
    queued_turn_cancel: Optional[asyncio.Event] = None
