from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex_autorunner.core.diagnostics.loop_attribution import (
    reset_loop_attribution,
    snapshot_loop_attribution,
)
from codex_autorunner.core.pma_lane_worker import PmaLaneWorker
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState
from tests.support.waits import wait_for_async_event, wait_for_async_predicate


@pytest.fixture(autouse=True)
def _reset_loop_attribution():
    reset_loop_attribution()
    yield
    reset_loop_attribution()


@pytest.mark.anyio
async def test_pma_idle_wait_skips_disk_read_when_mirror_unchanged(
    tmp_path: Path,
) -> None:
    lane_id = "pma:default"
    queue = PmaQueue(tmp_path)
    cancel = asyncio.Event()
    wait_task = asyncio.create_task(
        queue.wait_for_lane_item(
            lane_id,
            cancel,
            poll_interval_seconds=0.05,
        )
    )
    await asyncio.sleep(0.3)
    cancel.set()
    result = await wait_task
    assert result is False

    snap = snapshot_loop_attribution()
    assert "loops" not in snap or lane_id not in snap.get("loops", {})


@pytest.mark.anyio
async def test_pma_idle_wait_grows_backoff_on_consecutive_timeouts(
    tmp_path: Path,
) -> None:
    lane_id = "pma:backoff-test"
    queue = PmaQueue(tmp_path)
    cancel = asyncio.Event()

    poll_interval = 0.05
    wait_task = asyncio.create_task(
        queue.wait_for_lane_item(
            lane_id,
            cancel,
            poll_interval_seconds=poll_interval,
        )
    )

    await asyncio.sleep(0.15)
    assert not wait_task.done(), "should still be waiting"

    cancel.set()
    result = await wait_task
    assert result is False


@pytest.mark.anyio
async def test_pma_idle_wait_resets_backoff_on_enqueue(tmp_path: Path) -> None:
    lane_id = "pma:streak-reset"
    queue = PmaQueue(tmp_path)
    processed = asyncio.Event()

    async def executor(_item):
        processed.set()
        return {"status": "ok"}

    worker = PmaLaneWorker(
        lane_id,
        queue,
        executor,
        poll_interval_seconds=0.05,
    )
    await worker.start()

    _, _ = await queue.enqueue(lane_id, "key-a", {"msg": "first"})
    await wait_for_async_event(
        processed,
        timeout_seconds=2.0,
        description="first item processed",
    )

    terminal_item_state = None

    async def _item_terminal():
        nonlocal terminal_item_state
        items = await queue.list_items(lane_id)
        target = next((i for i in items if i.idempotency_key == "key-a"), None)
        if target is None:
            return False
        if target.state in (QueueItemState.COMPLETED, QueueItemState.FAILED):
            terminal_item_state = target.state
            return True
        return False

    await wait_for_async_predicate(
        _item_terminal,
        timeout_seconds=2.0,
        description="first item terminal",
    )

    assert queue._lane_idle_streaks.get(lane_id, 0) == 0

    processed.clear()
    _, _ = await queue.enqueue(lane_id, "key-b", {"msg": "second"})
    await wait_for_async_event(
        processed,
        timeout_seconds=2.0,
        description="second item processed",
    )

    await worker.stop()


@pytest.mark.anyio
async def test_pma_idle_wait_does_not_hit_disk_when_event_set(tmp_path: Path) -> None:
    lane_id = "pma:event-fast"
    queue = PmaQueue(tmp_path)
    _, _ = await queue.enqueue(lane_id, "pre-key", {"msg": "pre"})

    _ = await queue.dequeue(lane_id)

    event = queue._ensure_lane_event(lane_id)
    event.clear()

    _, _ = await queue.enqueue(lane_id, "trigger-key", {"msg": "trigger"})

    result = await asyncio.wait_for(
        queue.wait_for_lane_item(lane_id, poll_interval_seconds=10.0),
        timeout=0.5,
    )
    assert result is True


@pytest.mark.anyio
async def test_pma_lane_worker_idle_does_not_count_db_reads(
    tmp_path: Path,
) -> None:
    lane_id = "pma:worker-idle"
    queue = PmaQueue(tmp_path)
    cancel = asyncio.Event()
    processed = asyncio.Event()

    async def executor(_item):
        processed.set()
        return {"status": "ok"}

    worker = PmaLaneWorker(
        lane_id,
        queue,
        executor,
        poll_interval_seconds=0.05,
    )
    await worker.start()

    await asyncio.sleep(0.3)

    snap = snapshot_loop_attribution()
    loop_key = f"pma.lane_worker.{lane_id}"
    loop_stats = snap.get("loops", {}).get(loop_key, {})
    assert (
        loop_stats.get("db_reads", 0) == 0
    ), f"idle worker should not accumulate db_reads, got {loop_stats.get('db_reads')}"

    cancel.set()
    await worker.stop()
