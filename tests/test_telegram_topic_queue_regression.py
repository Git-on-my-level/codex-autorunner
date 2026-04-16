from __future__ import annotations

import asyncio

import pytest

from codex_autorunner.integrations.telegram.topic_queue import (
    TopicQueue,
    TopicRuntime,
)


async def _return(value: object) -> object:
    return value


async def _noop() -> None:
    pass


@pytest.mark.anyio
async def test_enqueue_executes_work() -> None:
    q = TopicQueue()
    result = await q.enqueue(lambda: _return(42))
    assert result == 42
    await q.close()


@pytest.mark.anyio
async def test_enqueue_preserves_order() -> None:
    q = TopicQueue()
    order: list[int] = []

    async def work(n: int) -> int:
        order.append(n)
        return n

    await q.enqueue(lambda: work(1))
    await q.enqueue(lambda: work(2))
    await q.enqueue(lambda: work(3))
    assert order == [1, 2, 3]
    await q.close()


@pytest.mark.anyio
async def test_enqueue_detached_executes_without_future() -> None:
    q = TopicQueue()
    executed: list[bool] = [False]

    async def work() -> None:
        executed[0] = True

    q.enqueue_detached(work)
    await asyncio.sleep(0.05)
    assert executed[0] is True
    await q.close()


@pytest.mark.anyio
async def test_enqueue_detached_with_item_id() -> None:
    q = TopicQueue()
    item_id = q.enqueue_detached(_noop, item_id="item-1")
    assert item_id == "item-1"
    await q.close()


@pytest.mark.anyio
async def test_pending_reports_queue_depth() -> None:
    q = TopicQueue()
    assert q.pending() == 0

    blocker = asyncio.Event()

    async def blocking_work() -> None:
        await blocker.wait()

    q.enqueue_detached(blocking_work)
    await asyncio.sleep(0.05)
    q.enqueue_detached(_noop)
    q.enqueue_detached(_noop)
    await asyncio.sleep(0)
    assert q.pending() == 2

    blocker.set()
    await asyncio.sleep(0.05)
    await q.close()


@pytest.mark.anyio
async def test_cancel_active_cancels_running_task() -> None:
    q = TopicQueue()
    started = asyncio.Event()

    async def long_work() -> None:
        started.set()
        await asyncio.sleep(10)

    q.enqueue_detached(long_work)
    await started.wait()
    cancelled = q.cancel_active()
    assert cancelled is True
    await asyncio.sleep(0.05)
    await q.close()


@pytest.mark.anyio
async def test_cancel_active_returns_false_when_idle() -> None:
    q = TopicQueue()
    assert q.cancel_active() is False
    await q.close()


@pytest.mark.anyio
async def test_cancel_pending_drains_queue() -> None:
    q = TopicQueue()
    blocker = asyncio.Event()

    async def blocking_work() -> None:
        await blocker.wait()

    q.enqueue_detached(blocking_work)
    await asyncio.sleep(0.05)

    q.enqueue_detached(_noop, item_id="a")
    q.enqueue_detached(_noop, item_id="b")
    await asyncio.sleep(0)

    cancelled = q.cancel_pending()
    assert cancelled == 2

    blocker.set()
    await asyncio.sleep(0.05)
    await q.close()


@pytest.mark.anyio
async def test_cancel_pending_item_targets_specific_entry() -> None:
    q = TopicQueue()
    blocker = asyncio.Event()
    results: list[str] = []

    async def blocking_work() -> None:
        await blocker.wait()

    async def tracked_work(label: str) -> None:
        results.append(label)

    q.enqueue_detached(blocking_work)
    await asyncio.sleep(0.05)

    q.enqueue_detached(lambda: tracked_work("a"), item_id="item-a")
    q.enqueue_detached(lambda: tracked_work("b"), item_id="item-b")
    q.enqueue_detached(lambda: tracked_work("c"), item_id="item-c")
    await asyncio.sleep(0)

    cancelled = q.cancel_pending_item("item-b")
    assert cancelled is True

    blocker.set()
    await asyncio.sleep(0.15)

    assert "a" in results
    assert "c" in results
    assert "b" not in results
    await q.close()


@pytest.mark.anyio
async def test_cancel_pending_item_returns_false_for_missing() -> None:
    q = TopicQueue()
    assert q.cancel_pending_item("nonexistent") is False
    await q.close()


@pytest.mark.anyio
async def test_cancel_pending_item_returns_false_for_empty_id() -> None:
    q = TopicQueue()
    assert q.cancel_pending_item("") is False
    await q.close()


@pytest.mark.anyio
async def test_promote_pending_item_moves_to_front() -> None:
    q = TopicQueue()
    blocker = asyncio.Event()
    results: list[str] = []

    async def blocking_work() -> None:
        await blocker.wait()

    async def tracked_work(label: str) -> None:
        results.append(label)

    q.enqueue_detached(blocking_work)
    await asyncio.sleep(0.05)

    q.enqueue_detached(lambda: tracked_work("a"), item_id="item-a")
    q.enqueue_detached(lambda: tracked_work("b"), item_id="item-b")
    q.enqueue_detached(lambda: tracked_work("c"), item_id="item-c")
    await asyncio.sleep(0)

    promoted = q.promote_pending_item("item-c")
    assert promoted is True

    blocker.set()
    await asyncio.sleep(0.15)

    assert results.index("c") < results.index("a")
    assert results.index("c") < results.index("b")
    await q.close()


@pytest.mark.anyio
async def test_promote_pending_item_returns_false_for_missing() -> None:
    q = TopicQueue()
    assert q.promote_pending_item("nonexistent") is False
    await q.close()


@pytest.mark.anyio
async def test_close_stops_worker() -> None:
    q = TopicQueue()
    await q.enqueue(lambda: _return("done"))
    await q.close()

    with pytest.raises(RuntimeError, match="topic queue is closed"):
        q.enqueue_detached(_noop)


@pytest.mark.anyio
async def test_close_is_idempotent() -> None:
    q = TopicQueue()
    await q.close()
    await q.close()


@pytest.mark.anyio
async def test_join_idle_waits_for_work_to_complete() -> None:
    q = TopicQueue()
    executed: list[bool] = [False]

    async def work() -> None:
        executed[0] = True

    await q.enqueue(work)
    await q.join_idle()
    assert executed[0] is True
    await q.close()


class TestTopicRuntime:
    def test_default_values(self) -> None:
        rt = TopicRuntime()
        assert isinstance(rt.queue, TopicQueue)
        assert rt.current_turn_id is None
        assert rt.current_turn_key is None
        assert rt.pending_request_id is None
        assert rt.interrupt_requested is False
        assert rt.interrupt_message_id is None
        assert rt.interrupt_turn_id is None
        assert rt.queued_turn_cancel is None

    def test_custom_values(self) -> None:
        rt = TopicRuntime(
            current_turn_id="turn-1",
            current_turn_key=("thread-1", "turn-1"),
            interrupt_requested=True,
        )
        assert rt.current_turn_id == "turn-1"
        assert rt.current_turn_key == ("thread-1", "turn-1")
        assert rt.interrupt_requested is True
