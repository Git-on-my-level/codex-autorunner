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


@pytest.mark.anyio
async def test_compact_lane_keeps_non_terminal_and_last_terminal_items(
    tmp_path: Path,
) -> None:
    lane_id = "pma:default"
    queue = PmaQueue(tmp_path)
    keep_last = 5
    total_terminal = 12

    terminal_ids: list[str] = []
    for index in range(total_terminal):
        item, _ = await queue.enqueue(
            lane_id,
            f"terminal-{index}",
            {"message": f"terminal-{index}"},
        )
        await queue.complete_item(item, {"index": index})
        terminal_ids.append(item.item_id)

    pending_item, _ = await queue.enqueue(
        lane_id,
        "pending-item",
        {"message": "pending"},
    )
    running_item, _ = await queue.enqueue(
        lane_id,
        "running-item",
        {"message": "running"},
    )
    running_item.state = QueueItemState.RUNNING
    await queue._update_in_file(running_item)

    lane_path = queue._lane_queue_path(lane_id)
    before_lines = len(
        [line for line in lane_path.read_text(encoding="utf-8").splitlines() if line]
    )

    changed = await queue.compact_lane(lane_id, keep_last=keep_last)
    assert changed is True

    after_lines = len(
        [line for line in lane_path.read_text(encoding="utf-8").splitlines() if line]
    )
    assert after_lines < before_lines

    items = await queue.list_items(lane_id)
    assert len(items) == keep_last + 2
    states = [item.state for item in items]
    assert states.count(QueueItemState.PENDING) == 1
    assert states.count(QueueItemState.RUNNING) == 1
    assert states.count(QueueItemState.COMPLETED) == keep_last
    assert any(item.item_id == pending_item.item_id for item in items)
    assert any(item.item_id == running_item.item_id for item in items)

    kept_terminal_ids = [
        item.item_id for item in items if item.state == QueueItemState.COMPLETED
    ]
    assert kept_terminal_ids == terminal_ids[-keep_last:]
