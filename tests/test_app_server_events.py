import asyncio

import pytest

from codex_autorunner.core.app_server_events import AppServerEventBuffer


async def _next_event(stream):
    return await stream.__anext__()


@pytest.mark.anyio
async def test_event_buffer_streams_existing_events() -> None:
    buffer = AppServerEventBuffer(max_events_per_turn=5)
    await buffer.register_turn("thread-1", "turn-1")
    await buffer.handle_notification(
        {
            "method": "item/completed",
            "params": {"turnId": "turn-1", "threadId": "thread-1"},
        }
    )

    stream = buffer.stream("thread-1", "turn-1", heartbeat_interval=0.01)
    payload = await asyncio.wait_for(_next_event(stream), timeout=1.0)
    assert "event: app-server" in payload
    assert '"turnId": "turn-1"' in payload
    await stream.aclose()


@pytest.mark.anyio
async def test_event_buffer_maps_turn_to_thread() -> None:
    buffer = AppServerEventBuffer(max_events_per_turn=5)
    await buffer.register_turn("thread-2", "turn-2")
    await buffer.handle_notification(
        {
            "method": "item/completed",
            "params": {"turnId": "turn-2"},
        }
    )

    stream = buffer.stream("thread-2", "turn-2", heartbeat_interval=0.01)
    payload = await asyncio.wait_for(_next_event(stream), timeout=1.0)
    assert '"turnId": "turn-2"' in payload
    await stream.aclose()
