from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex_autorunner.core.pma_lane_worker import PmaLaneWorker
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState
from tests.support.waits import wait_for_async_event, wait_for_async_predicate


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

    await wait_for_async_event(
        processed,
        timeout_seconds=3.0,
        description="cross-process enqueue to wake the lane worker",
    )

    async def _item_terminal() -> bool:
        items = await worker_queue.list_items(lane_id)
        return (
            items
            and items[0].item_id == item.item_id
            and items[0].state
            in (
                QueueItemState.COMPLETED,
                QueueItemState.FAILED,
            )
        )

    await wait_for_async_predicate(
        _item_terminal,
        timeout_seconds=3.0,
        description="cross-process queue item to reach terminal state",
    )

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
async def test_replay_pending_recovers_running_item_after_restart(
    tmp_path: Path,
) -> None:
    lane_id = "discord:channel-1"
    producer_queue = PmaQueue(tmp_path)

    item, _ = producer_queue.enqueue_sync(
        lane_id,
        "running-key-1",
        {"message": "hello"},
    )
    item.state = QueueItemState.RUNNING
    item.started_at = "2026-04-27T14:10:00Z"
    await producer_queue._update_in_file(item)

    recovered_queue = PmaQueue(tmp_path)
    replayed = await recovered_queue.replay_pending(lane_id)
    assert replayed == 1

    recovered = await recovered_queue.dequeue(lane_id)
    assert recovered is not None
    assert recovered.item_id == item.item_id
    assert recovered.state == QueueItemState.RUNNING
    assert recovered.started_at is not None


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


@pytest.mark.anyio
async def test_async_queue_operations_offload_blocking_store_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane_id = "pma:default"
    queue = PmaQueue(tmp_path)
    calls: list[str] = []
    original_to_thread = asyncio.to_thread

    async def _record_to_thread(func, /, *args, **kwargs):
        calls.append(getattr(func, "__name__", repr(func)))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _record_to_thread)

    item, _ = await queue.enqueue(lane_id, "offload-key", {"message": "hello"})
    items = await queue.list_items(lane_id)
    assert items and items[0].item_id == item.item_id

    dequeued = await queue.dequeue(lane_id)
    assert dequeued is not None
    await queue.complete_item(dequeued, {"status": "ok"})

    lanes = await queue.get_all_lanes()
    assert lane_id in lanes
    assert "_append_to_file_sync" in calls
    assert "_update_in_file_sync" in calls
    assert "_get_all_lanes_sync" in calls


@pytest.mark.anyio
async def test_lane_write_cancellation_waits_for_offloaded_store_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane_id = "pma:default"
    queue = PmaQueue(tmp_path)
    item, _ = await queue.enqueue(lane_id, "cancel-key", {"message": "hello"})
    item.state = QueueItemState.RUNNING

    started = asyncio.Event()
    release = asyncio.Event()
    original_to_thread = asyncio.to_thread

    async def _blocked_to_thread(func, /, *args, **kwargs):
        if getattr(func, "__name__", "") == "_update_in_file_sync":
            started.set()
            await release.wait()
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _blocked_to_thread)

    update_task = asyncio.create_task(queue._update_in_file(item))
    await wait_for_async_event(
        started,
        timeout_seconds=1.0,
        description="offloaded lane write to start",
    )

    update_task.cancel()

    lock_reacquired = asyncio.Event()
    waiter_started = asyncio.Event()

    async def _wait_for_lane_lock() -> None:
        waiter_started.set()
        async with queue._ensure_lane_lock(lane_id):
            lock_reacquired.set()

    waiter = asyncio.create_task(_wait_for_lane_lock())
    await wait_for_async_event(
        waiter_started,
        timeout_seconds=1.0,
        description="lane-lock waiter task to start",
    )
    assert lock_reacquired.is_set() is False

    release.set()

    with pytest.raises(asyncio.CancelledError):
        await update_task
    await asyncio.wait_for(waiter, timeout=1.0)
    assert lock_reacquired.is_set() is True
