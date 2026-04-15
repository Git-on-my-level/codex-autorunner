from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex_autorunner.core.pma_lane_worker import PmaLaneWorker
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState
from tests.support.waits import wait_for_async_event, wait_for_async_predicate


@pytest.mark.anyio
async def test_pma_lane_worker_processes_item(tmp_path: Path) -> None:
    queue = PmaQueue(tmp_path)
    lane_id = "pma:default"
    terminal_states = (QueueItemState.COMPLETED, QueueItemState.FAILED)

    item, _ = await queue.enqueue(lane_id, "key-1", {"message": "hi"})
    processed = asyncio.Event()

    async def executor(_item):
        processed.set()
        return {"status": "ok", "message": "done"}

    worker = PmaLaneWorker(lane_id, queue, executor)
    await worker.start()

    await wait_for_async_event(
        processed,
        timeout_seconds=2.0,
        description="lane worker executor to process the queued item",
    )

    terminal_item_state: QueueItemState | None = None

    async def _item_reached_terminal_state() -> bool:
        nonlocal terminal_item_state
        items = await queue.list_items(lane_id)
        target = next((entry for entry in items if entry.item_id == item.item_id), None)
        if target is None:
            return False
        if target.state in terminal_states:
            terminal_item_state = target.state
            return True
        return False

    await wait_for_async_predicate(
        _item_reached_terminal_state,
        timeout_seconds=5.0,
        description="specific queue item to reach a terminal state",
    )

    items = await asyncio.to_thread(queue._read_items_from_sqlite, lane_id)
    assert items, "queue item should be present"
    assert items[0].item_id == item.item_id
    assert terminal_item_state in terminal_states

    await worker.stop()


@pytest.mark.anyio
async def test_pma_lane_worker_marks_running_item_failed_on_cancellation(
    tmp_path: Path,
) -> None:
    queue = PmaQueue(tmp_path)
    lane_id = "pma:default"

    item, _ = await queue.enqueue(lane_id, "key-2", {"message": "hi"})
    entered = asyncio.Event()
    release = asyncio.Event()

    async def executor(_item):
        entered.set()
        await release.wait()
        return {"status": "ok", "message": "done"}

    worker = PmaLaneWorker(lane_id, queue, executor)
    await worker.start()
    await wait_for_async_event(
        entered,
        timeout_seconds=2.0,
        description="lane worker executor to begin processing",
    )

    task = worker._task
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    items = await queue.list_items(lane_id)
    assert len(items) == 1
    assert items[0].item_id == item.item_id
    assert items[0].state == QueueItemState.FAILED
