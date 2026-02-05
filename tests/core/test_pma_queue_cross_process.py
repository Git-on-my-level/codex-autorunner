from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex_autorunner.core.pma_lane_worker import PmaLaneWorker
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState


@pytest.mark.anyio
async def test_enqueue_sync_cross_process_wakes_worker(tmp_path: Path) -> None:
    lane_id = "pma:default"
    worker_queue = PmaQueue(tmp_path)
    producer_queue = PmaQueue(tmp_path)

    processed = asyncio.Event()

    async def executor(_item):
        processed.set()
        return {"status": "ok"}

    worker = PmaLaneWorker(
        lane_id,
        worker_queue,
        executor,
        poll_interval_seconds=0.1,
    )
    await worker.start()

    item, _ = producer_queue.enqueue_sync(
        lane_id,
        "sync-key-1",
        {"message": "hello"},
    )

    await asyncio.wait_for(processed.wait(), timeout=3.0)

    for _ in range(20):
        items = await worker_queue.list_items(lane_id)
        if (
            items
            and items[0].item_id == item.item_id
            and items[0].state
            in (
                QueueItemState.COMPLETED,
                QueueItemState.FAILED,
            )
        ):
            break
        await asyncio.sleep(0.05)

    items = await worker_queue.list_items(lane_id)
    assert items, "queue item should be present"
    assert items[0].item_id == item.item_id
    assert items[0].state in (QueueItemState.COMPLETED, QueueItemState.FAILED)

    await worker.stop()


@pytest.mark.anyio
async def test_refresh_does_not_duplicate_items(tmp_path: Path) -> None:
    lane_id = "pma:default"
    queue = PmaQueue(tmp_path)
    producer_queue = PmaQueue(tmp_path)

    item, _ = producer_queue.enqueue_sync(
        lane_id,
        "sync-key-2",
        {"message": "hello"},
    )

    added = await queue._refresh_lane_from_disk(lane_id)
    assert added == 1

    first = await queue.dequeue(lane_id)
    assert first is not None
    assert first.item_id == item.item_id

    added_again = await queue._refresh_lane_from_disk(lane_id)
    assert added_again == 0

    second = await queue.dequeue(lane_id)
    assert second is None


@pytest.mark.anyio
async def test_enqueue_sync_idempotency_dedupe(tmp_path: Path) -> None:
    lane_id = "pma:default"
    queue = PmaQueue(tmp_path)

    item, reason = queue.enqueue_sync(lane_id, "dupe-key", {"message": "a"})
    assert reason is None

    deduped, reason = queue.enqueue_sync(
        lane_id,
        "dupe-key",
        {"message": "b"},
    )
    assert reason is not None
    assert deduped.state == QueueItemState.DEDUPED
    assert deduped.dedupe_reason == f"duplicate_of_{item.item_id}"

    items = await queue.list_items(lane_id)
    states = [item.state for item in items]
    assert states.count(QueueItemState.PENDING) == 1
    assert states.count(QueueItemState.DEDUPED) == 1
