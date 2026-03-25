import asyncio

import pytest

from codex_autorunner.integrations.telegram.state import TopicQueue


@pytest.mark.anyio
async def test_topic_queue_cancel_active_allows_next() -> None:
    queue = TopicQueue()
    started = asyncio.Event()
    unblock = asyncio.Event()

    async def work() -> str:
        started.set()
        await unblock.wait()
        return "done"

    task = asyncio.create_task(queue.enqueue(work))
    await started.wait()
    assert queue.cancel_active() is True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    async def follow_up() -> str:
        return "ok"

    result = await queue.enqueue(follow_up)
    assert result == "ok"
    await queue.close()


@pytest.mark.anyio
async def test_topic_queue_cancel_pending_item_removes_selected_entry() -> None:
    queue = TopicQueue()
    started = asyncio.Event()
    release = asyncio.Event()
    observed: list[str] = []

    async def work(label: str) -> str:
        observed.append(label)
        if label == "first":
            started.set()
            await release.wait()
        return label

    first_task = asyncio.create_task(queue.enqueue(lambda: work("first")))
    await started.wait()
    queue.enqueue_detached(lambda: work("second"), item_id="m-2")
    queue.enqueue_detached(lambda: work("third"), item_id="m-3")

    assert queue.cancel_pending_item("m-2") is True

    release.set()
    assert await first_task == "first"
    await asyncio.sleep(0)

    assert observed == ["first", "third"]
    await queue.close()


@pytest.mark.anyio
async def test_topic_queue_promote_pending_item_moves_selected_entry_to_front() -> None:
    queue = TopicQueue()
    started = asyncio.Event()
    release = asyncio.Event()
    observed: list[str] = []

    async def work(label: str) -> str:
        observed.append(label)
        if label == "first":
            started.set()
            await release.wait()
        return label

    first_task = asyncio.create_task(queue.enqueue(lambda: work("first")))
    await started.wait()
    queue.enqueue_detached(lambda: work("second"), item_id="m-2")
    queue.enqueue_detached(lambda: work("third"), item_id="m-3")

    assert queue.promote_pending_item("m-3") is True

    release.set()
    assert await first_task == "first"
    for _ in range(20):
        if observed == ["first", "third", "second"]:
            break
        await asyncio.sleep(0.01)

    assert observed == ["first", "third", "second"]
    await queue.close()
