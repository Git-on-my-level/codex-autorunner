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
