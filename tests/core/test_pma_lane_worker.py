from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex_autorunner.core.pma_lane_worker import PmaLaneWorker
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState


@pytest.mark.anyio
async def test_pma_lane_worker_processes_item(tmp_path: Path) -> None:
    queue = PmaQueue(tmp_path)
    lane_id = "pma:default"

    item, _ = await queue.enqueue(lane_id, "key-1", {"message": "hi"})
    processed = asyncio.Event()

    async def executor(_item):
        processed.set()
        return {"status": "ok", "message": "done"}

    worker = PmaLaneWorker(lane_id, queue, executor)
    await worker.start()

    await asyncio.wait_for(processed.wait(), timeout=2.0)

    for _ in range(20):
        items = await queue.list_items(lane_id)
        if items and items[0].state in (
            QueueItemState.COMPLETED,
            QueueItemState.FAILED,
        ):
            break
        await asyncio.sleep(0.05)

    items = await queue.list_items(lane_id)
    assert items, "queue item should be present"
    assert items[0].item_id == item.item_id
    assert items[0].state in (QueueItemState.COMPLETED, QueueItemState.FAILED)

    await worker.stop()
