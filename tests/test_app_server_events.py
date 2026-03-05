import asyncio

import pytest

from codex_autorunner.integrations.app_server.event_buffer import AppServerEventBuffer


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
    assert "id: 1" in payload
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


@pytest.mark.anyio
async def test_event_buffer_replays_after_specific_event_id() -> None:
    buffer = AppServerEventBuffer(max_events_per_turn=5)
    await buffer.register_turn("thread-3", "turn-3")
    await buffer.handle_notification(
        {
            "method": "item/completed",
            "params": {"turnId": "turn-3", "threadId": "thread-3", "seq": 1},
        }
    )
    await buffer.handle_notification(
        {
            "method": "item/completed",
            "params": {"turnId": "turn-3", "threadId": "thread-3", "seq": 2},
        }
    )

    stream = buffer.stream("thread-3", "turn-3", after_id=1, heartbeat_interval=0.01)
    payload = await asyncio.wait_for(_next_event(stream), timeout=1.0)
    assert "id: 2" in payload
    assert '"seq": 2' in payload
    await stream.aclose()


@pytest.mark.anyio
async def test_event_buffer_lists_events_after_id() -> None:
    buffer = AppServerEventBuffer(max_events_per_turn=5)
    await buffer.register_turn("thread-4", "turn-4")
    await buffer.handle_notification(
        {
            "method": "item/completed",
            "params": {"turnId": "turn-4", "threadId": "thread-4", "seq": 1},
        }
    )
    await buffer.handle_notification(
        {
            "method": "item/completed",
            "params": {"turnId": "turn-4", "threadId": "thread-4", "seq": 2},
        }
    )

    listed = await buffer.list_events("thread-4", "turn-4", after_id=1)
    assert len(listed) == 1
    assert listed[0]["id"] == 2
