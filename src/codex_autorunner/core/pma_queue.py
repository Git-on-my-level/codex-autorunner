from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
from .time_utils import now_iso

PMA_QUEUE_DIR = ".codex-autorunner/pma/queue"
QUEUE_FILE_SUFFIX = ".jsonl"

logger = logging.getLogger(__name__)


class QueueItemState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEDUPED = "deduped"


@dataclass
class PmaQueueItem:
    item_id: str
    lane_id: str
    enqueued_at: str
    idempotency_key: str
    payload: dict[str, Any]
    state: QueueItemState = QueueItemState.PENDING
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    dedupe_reason: Optional[str] = None
    result: Optional[dict[str, Any]] = None

    @classmethod
    def create(
        cls,
        lane_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> "PmaQueueItem":
        return cls(
            item_id=str(uuid.uuid4()),
            lane_id=lane_id,
            enqueued_at=now_iso(),
            idempotency_key=idempotency_key,
            payload=payload,
            state=QueueItemState.PENDING,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PmaQueueItem":
        data = dict(data)
        if isinstance(data.get("state"), str):
            try:
                data["state"] = QueueItemState(data["state"])
            except ValueError:
                data["state"] = QueueItemState.PENDING
        return cls(**data)


class PmaQueue:
    """PMA queue backed by JSONL state; pending items are replayed into memory."""

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root
        self._queue_dir = hub_root / PMA_QUEUE_DIR
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        self._lane_queues: dict[str, asyncio.Queue[PmaQueueItem]] = {}
        self._lane_locks: dict[str, asyncio.Lock] = {}
        self._lane_events: dict[str, asyncio.Event] = {}
        self._replayed_lanes: set[str] = set()
        self._lock = asyncio.Lock()

    def _lane_queue_path(self, lane_id: str) -> Path:
        safe_lane_id = lane_id.replace(":", "__COLON__").replace("/", "__SLASH__")
        return self._queue_dir / f"{safe_lane_id}{QUEUE_FILE_SUFFIX}"

    def _lane_queue_lock_path(self, lane_id: str) -> Path:
        path = self._lane_queue_path(lane_id)
        return path.with_suffix(path.suffix + ".lock")

    def _ensure_lane_lock(self, lane_id: str) -> asyncio.Lock:
        lock = self._lane_locks.get(lane_id)
        if lock is None:
            lock = asyncio.Lock()
            self._lane_locks[lane_id] = lock
        return lock

    def _ensure_lane_event(self, lane_id: str) -> asyncio.Event:
        event = self._lane_events.get(lane_id)
        if event is None:
            event = asyncio.Event()
            self._lane_events[lane_id] = event
        return event

    def _ensure_lane_queue(self, lane_id: str) -> asyncio.Queue[PmaQueueItem]:
        queue = self._lane_queues.get(lane_id)
        if queue is None:
            queue = asyncio.Queue()
            self._lane_queues[lane_id] = queue
        return queue

    async def enqueue(
        self,
        lane_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[PmaQueueItem, Optional[str]]:
        async with self._lock:
            existing = await self._find_by_idempotency_key(lane_id, idempotency_key)
            if existing:
                if existing.state in (QueueItemState.PENDING, QueueItemState.RUNNING):
                    dedupe_item = PmaQueueItem.create(
                        lane_id=lane_id,
                        idempotency_key=idempotency_key,
                        payload=payload,
                    )
                    dedupe_item.state = QueueItemState.DEDUPED
                    dedupe_item.dedupe_reason = f"duplicate_of_{existing.item_id}"
                    await self._append_to_file(dedupe_item)
                    return dedupe_item, f"duplicate of {existing.item_id}"

            item = PmaQueueItem.create(lane_id, idempotency_key, payload)
            await self._append_to_file(item)
            queue = self._ensure_lane_queue(lane_id)
            await queue.put(item)
            self._ensure_lane_event(lane_id).set()
            return item, None

    async def dequeue(self, lane_id: str) -> Optional[PmaQueueItem]:
        queue = self._lane_queues.get(lane_id)
        if queue is None or queue.empty():
            return None
        try:
            item = queue.get_nowait()
            item.state = QueueItemState.RUNNING
            item.started_at = now_iso()
            await self._update_in_file(item)
            return item
        except asyncio.QueueEmpty:
            return None

    async def complete_item(
        self, item: PmaQueueItem, result: Optional[dict[str, Any]] = None
    ) -> None:
        item.state = QueueItemState.COMPLETED
        item.finished_at = now_iso()
        if result is not None:
            item.result = result
        await self._update_in_file(item)

    async def fail_item(self, item: PmaQueueItem, error: str) -> None:
        item.state = QueueItemState.FAILED
        item.finished_at = now_iso()
        item.error = error
        await self._update_in_file(item)

    async def cancel_lane(self, lane_id: str) -> int:
        cancelled = 0
        cancelled_ids: set[str] = set()
        items = await self.list_items(lane_id)
        for item in items:
            if item.state == QueueItemState.PENDING:
                item.state = QueueItemState.CANCELLED
                item.finished_at = now_iso()
                await self._update_in_file(item)
                cancelled += 1
                cancelled_ids.add(item.item_id)

        queue = self._lane_queues.get(lane_id)
        if queue is not None:
            while not queue.empty():
                try:
                    queued_item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if queued_item.item_id in cancelled_ids:
                    continue
                if queued_item.state != QueueItemState.PENDING:
                    continue
                queued_item.state = QueueItemState.CANCELLED
                queued_item.finished_at = now_iso()
                await self._update_in_file(queued_item)
                cancelled += 1
                cancelled_ids.add(queued_item.item_id)

        event = self._lane_events.get(lane_id)
        if event is not None:
            event.set()

        return cancelled

    async def replay_pending(self, lane_id: str) -> int:
        if lane_id in self._replayed_lanes:
            return 0
        self._replayed_lanes.add(lane_id)

        items = await self.list_items(lane_id)
        pending = [item for item in items if item.state == QueueItemState.PENDING]
        if not pending:
            return 0

        queue = self._ensure_lane_queue(lane_id)
        for item in pending:
            await queue.put(item)
        self._ensure_lane_event(lane_id).set()
        return len(pending)

    async def wait_for_lane_item(
        self, lane_id: str, cancel_event: Optional[asyncio.Event] = None
    ) -> bool:
        event = self._ensure_lane_event(lane_id)
        if event.is_set():
            event.clear()
            return True

        wait_tasks = [asyncio.create_task(event.wait())]
        if cancel_event is not None:
            wait_tasks.append(asyncio.create_task(cancel_event.wait()))

        done, pending = await asyncio.wait(
            wait_tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

        if cancel_event is not None and cancel_event.is_set():
            return False

        if event.is_set():
            event.clear()
        return True

    async def list_items(self, lane_id: str) -> list[PmaQueueItem]:
        path = self._lane_queue_path(lane_id)
        if not path.exists():
            return []

        items: list[PmaQueueItem] = []
        async with self._ensure_lane_lock(lane_id):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                return []

            for line in content.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    items.append(PmaQueueItem.from_dict(data))
                except (json.JSONDecodeError, ValueError):
                    continue

        return items

    async def _find_by_idempotency_key(
        self, lane_id: str, idempotency_key: str
    ) -> Optional[PmaQueueItem]:
        items = await self.list_items(lane_id)
        for item in items:
            if item.idempotency_key == idempotency_key and item.state in (
                QueueItemState.PENDING,
                QueueItemState.RUNNING,
            ):
                return item
        return None

    async def _append_to_file(self, item: PmaQueueItem) -> None:
        path = self._lane_queue_path(item.lane_id)
        async with self._ensure_lane_lock(item.lane_id):
            with file_lock(self._lane_queue_lock_path(item.lane_id)):
                path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(item.to_dict(), separators=(",", ":")) + "\n"
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)

    async def _update_in_file(self, item: PmaQueueItem) -> None:
        path = self._lane_queue_path(item.lane_id)
        async with self._ensure_lane_lock(item.lane_id):
            with file_lock(self._lane_queue_lock_path(item.lane_id)):
                if not path.exists():
                    return

                try:
                    content = path.read_text(encoding="utf-8")
                except OSError:
                    return

                lines: list[str] = []
                updated = False
                for line in content.strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if data.get("item_id") == item.item_id:
                            lines.append(
                                json.dumps(item.to_dict(), separators=(",", ":"))
                            )
                            updated = True
                        else:
                            lines.append(line)
                    except (json.JSONDecodeError, ValueError):
                        lines.append(line)

                if updated:
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    async def get_lane_stats(self, lane_id: str) -> dict[str, Any]:
        items = await self.list_items(lane_id)
        by_state: dict[str, int] = {}
        for item in items:
            state = item.state.value
            by_state[state] = by_state.get(state, 0) + 1

        return {
            "lane_id": lane_id,
            "total_items": len(items),
            "by_state": by_state,
        }

    async def get_all_lanes(self) -> list[str]:
        lanes: set[str] = set()
        if not self._queue_dir.exists():
            return []

        for path in self._queue_dir.iterdir():
            if path.is_file() and path.suffix == QUEUE_FILE_SUFFIX:
                lane_name = path.stem.replace("__SLASH__", "/").replace(
                    "__COLON__", ":"
                )
                lanes.add(lane_name)

        return sorted(lanes)

    async def get_queue_summary(self) -> dict[str, Any]:
        lanes = await self.get_all_lanes()
        summary: dict[str, Any] = {"lanes": {}}
        for lane in lanes:
            summary["lanes"][lane] = await self.get_lane_stats(lane)
        summary["total_lanes"] = len(lanes)
        return summary


__all__ = [
    "QueueItemState",
    "PmaQueueItem",
    "PmaQueue",
]
