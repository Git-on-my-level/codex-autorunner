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
from .orchestration.migrate_legacy_state import backfill_legacy_queue_state
from .orchestration.sqlite import open_orchestration_sqlite
from .time_utils import now_iso
from .utils import atomic_write

PMA_QUEUE_DIR = ".codex-autorunner/pma/queue"
QUEUE_FILE_SUFFIX = ".jsonl"
DEFAULT_COMPACTION_KEEP_LAST = 200
COMPACTION_MIN_SIZE_BYTES = 256 * 1024

logger = logging.getLogger(__name__)


class QueueItemState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEDUPED = "deduped"


TERMINAL_STATES = {
    QueueItemState.COMPLETED.value,
    QueueItemState.FAILED.value,
    QueueItemState.CANCELLED.value,
    QueueItemState.DEDUPED.value,
}


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
    """PMA queue backed by orchestration SQLite with JSONL compatibility mirrors."""

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root
        self._queue_dir = hub_root / PMA_QUEUE_DIR
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        self._lane_queues: dict[str, asyncio.Queue[PmaQueueItem]] = {}
        self._lane_locks: dict[str, asyncio.Lock] = {}
        self._lane_events: dict[str, asyncio.Event] = {}
        self._lane_known_ids: dict[str, set[str]] = {}
        self._replayed_lanes: set[str] = set()
        self._lock: Optional[asyncio.Lock] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._initialize_canonical_state()

    def _initialize_canonical_state(self) -> None:
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            backfill_legacy_queue_state(self._hub_root, conn)

    def _lane_queue_path(self, lane_id: str) -> Path:
        safe_lane_id = lane_id.replace(":", "__COLON__").replace("/", "__SLASH__")
        return self._queue_dir / f"{safe_lane_id}{QUEUE_FILE_SUFFIX}"

    def _lane_queue_lock_path(self, lane_id: str) -> Path:
        path = self._lane_queue_path(lane_id)
        return path.with_suffix(path.suffix + ".lock")

    def _ensure_lock(self) -> asyncio.Lock:
        lock = self._lock
        if lock is None:
            lock = asyncio.Lock()
            self._lock = lock
        return lock

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

    def _ensure_lane_known_ids(self, lane_id: str) -> set[str]:
        known = self._lane_known_ids.get(lane_id)
        if known is None:
            known = set()
            self._lane_known_ids[lane_id] = known
        return known

    def _record_loop(self) -> None:
        if self._loop is not None:
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            return

    async def enqueue(
        self,
        lane_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[PmaQueueItem, Optional[str]]:
        async with self._ensure_lock():
            self._record_loop()
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
            self._ensure_lane_known_ids(lane_id).add(item.item_id)
            self._ensure_lane_event(lane_id).set()
            return item, None

    def enqueue_sync(
        self,
        lane_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[PmaQueueItem, Optional[str]]:
        existing = self._find_by_idempotency_key_sync(lane_id, idempotency_key)
        if existing:
            if existing.state in (QueueItemState.PENDING, QueueItemState.RUNNING):
                dedupe_item = PmaQueueItem.create(
                    lane_id=lane_id,
                    idempotency_key=idempotency_key,
                    payload=payload,
                )
                dedupe_item.state = QueueItemState.DEDUPED
                dedupe_item.dedupe_reason = f"duplicate_of_{existing.item_id}"
                self._append_to_file_sync(dedupe_item)
                return dedupe_item, f"duplicate of {existing.item_id}"

        item = PmaQueueItem.create(lane_id, idempotency_key, payload)
        self._append_to_file_sync(item)
        self._notify_in_memory_enqueue(item)
        return item, None

    async def dequeue(self, lane_id: str) -> Optional[PmaQueueItem]:
        self._record_loop()
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
        await self._maybe_compact_lane(item.lane_id)

    async def fail_item(self, item: PmaQueueItem, error: str) -> None:
        item.state = QueueItemState.FAILED
        item.finished_at = now_iso()
        item.error = error
        await self._update_in_file(item)
        await self._maybe_compact_lane(item.lane_id)

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
        self._record_loop()
        if lane_id in self._replayed_lanes:
            return 0
        self._replayed_lanes.add(lane_id)

        items = await self.list_items(lane_id)
        known = self._ensure_lane_known_ids(lane_id)
        for item in items:
            known.add(item.item_id)
        pending = [item for item in items if item.state == QueueItemState.PENDING]
        if not pending:
            return 0

        queue = self._ensure_lane_queue(lane_id)
        for item in pending:
            await queue.put(item)
        self._ensure_lane_event(lane_id).set()
        return len(pending)

    async def wait_for_lane_item(
        self,
        lane_id: str,
        cancel_event: Optional[asyncio.Event] = None,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> bool:
        self._record_loop()
        event = self._ensure_lane_event(lane_id)
        if event.is_set():
            event.clear()
            return True

        poll_interval = max(0.1, poll_interval_seconds)
        while True:
            wait_tasks = [asyncio.create_task(event.wait())]
            if cancel_event is not None:
                wait_tasks.append(asyncio.create_task(cancel_event.wait()))

            try:
                done, pending = await asyncio.wait(
                    wait_tasks,
                    timeout=poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in wait_tasks:
                    if not task.done():
                        task.cancel()

            if cancel_event is not None and cancel_event.is_set():
                return False

            if event.is_set():
                event.clear()
                return True

            added = await self._refresh_lane_from_disk(lane_id)
            if added:
                return True

    async def list_items(self, lane_id: str) -> list[PmaQueueItem]:
        async with self._ensure_lane_lock(lane_id):
            return self._read_items_from_sqlite(lane_id)

    async def _refresh_lane_from_disk(self, lane_id: str) -> int:
        items = await self.list_items(lane_id)
        if not items:
            return 0

        known = self._ensure_lane_known_ids(lane_id)
        new_pending: list[PmaQueueItem] = []
        for item in items:
            if item.item_id in known:
                continue
            known.add(item.item_id)
            if item.state == QueueItemState.PENDING:
                new_pending.append(item)

        if not new_pending:
            return 0

        queue = self._ensure_lane_queue(lane_id)
        for item in new_pending:
            await queue.put(item)
        self._ensure_lane_event(lane_id).set()
        return len(new_pending)

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
        async with self._ensure_lane_lock(item.lane_id):
            with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO orch_queue_items (
                            queue_item_id,
                            lane_id,
                            source_kind,
                            source_key,
                            dedupe_key,
                            state,
                            visible_at,
                            claimed_at,
                            completed_at,
                            payload_json,
                            created_at,
                            updated_at,
                            idempotency_key,
                            error_text,
                            dedupe_reason,
                            result_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(queue_item_id) DO UPDATE SET
                            lane_id = excluded.lane_id,
                            source_kind = excluded.source_kind,
                            source_key = excluded.source_key,
                            dedupe_key = excluded.dedupe_key,
                            state = excluded.state,
                            visible_at = excluded.visible_at,
                            claimed_at = excluded.claimed_at,
                            completed_at = excluded.completed_at,
                            payload_json = excluded.payload_json,
                            created_at = excluded.created_at,
                            updated_at = excluded.updated_at,
                            idempotency_key = excluded.idempotency_key,
                            error_text = excluded.error_text,
                            dedupe_reason = excluded.dedupe_reason,
                            result_json = excluded.result_json
                        """,
                        self._item_db_tuple(item),
                    )
            self._sync_lane_mirror_sync(item.lane_id)

    async def _update_in_file(self, item: PmaQueueItem) -> None:
        async with self._ensure_lane_lock(item.lane_id):
            with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
                with conn:
                    conn.execute(
                        """
                        UPDATE orch_queue_items
                           SET lane_id = ?,
                               source_kind = ?,
                               source_key = ?,
                               dedupe_key = ?,
                               state = ?,
                               visible_at = ?,
                               claimed_at = ?,
                               completed_at = ?,
                               payload_json = ?,
                               created_at = ?,
                               updated_at = ?,
                               idempotency_key = ?,
                               error_text = ?,
                               dedupe_reason = ?,
                               result_json = ?
                         WHERE queue_item_id = ?
                        """,
                        (
                            item.lane_id,
                            "pma_lane",
                            item.item_id,
                            item.idempotency_key,
                            item.state.value,
                            item.enqueued_at,
                            item.started_at,
                            item.finished_at,
                            json.dumps(item.payload, separators=(",", ":")),
                            item.enqueued_at,
                            item.finished_at or item.started_at or item.enqueued_at,
                            item.idempotency_key,
                            item.error,
                            item.dedupe_reason,
                            json.dumps(item.result or {}, separators=(",", ":")),
                            item.item_id,
                        ),
                    )
            self._sync_lane_mirror_sync(item.lane_id)

    async def compact_lane(
        self,
        lane_id: str,
        *,
        keep_last: int = DEFAULT_COMPACTION_KEEP_LAST,
    ) -> bool:
        keep_last = max(0, keep_last)
        async with self._ensure_lane_lock(lane_id):
            items = self._read_items_from_sqlite(lane_id)
            if not items:
                return False
            terminal_indexes = [
                idx
                for idx, item in enumerate(items)
                if item.state.value in TERMINAL_STATES
            ]
            if len(terminal_indexes) <= keep_last:
                return False
            keep_terminal_indexes = (
                set(terminal_indexes[-keep_last:]) if keep_last > 0 else set()
            )
            delete_ids = [
                item.item_id
                for idx, item in enumerate(items)
                if item.state.value in TERMINAL_STATES
                and idx not in keep_terminal_indexes
            ]
            if not delete_ids:
                return False
            with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
                with conn:
                    conn.executemany(
                        "DELETE FROM orch_queue_items WHERE queue_item_id = ?",
                        [(item_id,) for item_id in delete_ids],
                    )
            self._sync_lane_mirror_sync(lane_id)
            return True

    async def _maybe_compact_lane(self, lane_id: str) -> None:
        path = self._lane_queue_path(lane_id)
        try:
            if path.stat().st_size < COMPACTION_MIN_SIZE_BYTES:
                return
        except OSError:
            return
        await self.compact_lane(lane_id)

    def _append_to_file_sync(self, item: PmaQueueItem) -> None:
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_queue_items (
                        queue_item_id,
                        lane_id,
                        source_kind,
                        source_key,
                        dedupe_key,
                        state,
                        visible_at,
                        claimed_at,
                        completed_at,
                        payload_json,
                        created_at,
                        updated_at,
                        idempotency_key,
                        error_text,
                        dedupe_reason,
                        result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(queue_item_id) DO UPDATE SET
                        lane_id = excluded.lane_id,
                        source_kind = excluded.source_kind,
                        source_key = excluded.source_key,
                        dedupe_key = excluded.dedupe_key,
                        state = excluded.state,
                        visible_at = excluded.visible_at,
                        claimed_at = excluded.claimed_at,
                        completed_at = excluded.completed_at,
                        payload_json = excluded.payload_json,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at,
                        idempotency_key = excluded.idempotency_key,
                        error_text = excluded.error_text,
                        dedupe_reason = excluded.dedupe_reason,
                        result_json = excluded.result_json
                    """,
                    self._item_db_tuple(item),
                )
        self._sync_lane_mirror_sync(item.lane_id)

    def _read_items_sync(self, lane_id: str) -> list[PmaQueueItem]:
        return self._read_items_from_sqlite(lane_id)

    def _find_by_idempotency_key_sync(
        self, lane_id: str, idempotency_key: str
    ) -> Optional[PmaQueueItem]:
        items = self._read_items_sync(lane_id)
        for item in items:
            if item.idempotency_key == idempotency_key and item.state in (
                QueueItemState.PENDING,
                QueueItemState.RUNNING,
            ):
                return item
        return None

    def _notify_in_memory_enqueue(self, item: PmaQueueItem) -> None:
        self._ensure_lane_known_ids(item.lane_id).add(item.item_id)
        queue = self._lane_queues.get(item.lane_id)
        event = self._lane_events.get(item.lane_id)
        loop = self._loop
        if loop is None or loop.is_closed() or queue is None or event is None:
            return

        def _enqueue() -> None:
            queue.put_nowait(item)
            event.set()

        try:
            loop.call_soon_threadsafe(_enqueue)
        except RuntimeError:
            return

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
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT lane_id
                  FROM orch_queue_items
                 ORDER BY lane_id ASC
                """
            ).fetchall()
        return [str(row["lane_id"]) for row in rows if row["lane_id"]]

    async def get_queue_summary(self) -> dict[str, Any]:
        lanes = await self.get_all_lanes()
        summary: dict[str, Any] = {"lanes": {}}
        for lane in lanes:
            summary["lanes"][lane] = await self.get_lane_stats(lane)
        summary["total_lanes"] = len(lanes)
        return summary

    def _item_db_tuple(self, item: PmaQueueItem) -> tuple[Any, ...]:
        return (
            item.item_id,
            item.lane_id,
            "pma_lane",
            item.item_id,
            item.idempotency_key,
            item.state.value,
            item.enqueued_at,
            item.started_at,
            item.finished_at,
            json.dumps(item.payload, separators=(",", ":")),
            item.enqueued_at,
            item.finished_at or item.started_at or item.enqueued_at,
            item.idempotency_key,
            item.error,
            item.dedupe_reason,
            json.dumps(item.result or {}, separators=(",", ":")),
        )

    def _row_to_item(self, row: Any) -> PmaQueueItem:
        payload = self._json_loads_object(row["payload_json"])
        result = self._json_loads_object(row["result_json"])
        state_raw = str(row["state"] or QueueItemState.PENDING.value)
        try:
            state = QueueItemState(state_raw)
        except ValueError:
            state = QueueItemState.PENDING
        return PmaQueueItem(
            item_id=str(row["queue_item_id"]),
            lane_id=str(row["lane_id"]),
            enqueued_at=str(row["created_at"]),
            idempotency_key=str(row["idempotency_key"] or row["dedupe_key"] or ""),
            payload=payload,
            state=state,
            started_at=row["claimed_at"],
            finished_at=row["completed_at"],
            error=row["error_text"],
            dedupe_reason=row["dedupe_reason"],
            result=result or None,
        )

    @staticmethod
    def _json_loads_object(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _read_items_from_sqlite(self, lane_id: str) -> list[PmaQueueItem]:
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_queue_items
                 WHERE lane_id = ?
                 ORDER BY rowid ASC
                """,
                (lane_id,),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def _sync_lane_mirror_sync(self, lane_id: str) -> None:
        path = self._lane_queue_path(lane_id)
        items = self._read_items_from_sqlite(lane_id)
        with file_lock(self._lane_queue_lock_path(lane_id)):
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                json.dumps(item.to_dict(), separators=(",", ":")) for item in items
            ]
            content = "\n".join(lines)
            atomic_write(path, (content + "\n") if content else "")


__all__ = [
    "QueueItemState",
    "PmaQueueItem",
    "PmaQueue",
]
