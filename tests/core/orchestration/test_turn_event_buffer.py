from __future__ import annotations

import asyncio

import pytest

from codex_autorunner.core.orchestration.turn_event_buffer import TurnEventBuffer


@pytest.mark.asyncio
async def test_turn_event_buffer_append_adds_events() -> None:
    buf = TurnEventBuffer()
    await buf.append({"a": 1})
    await buf.append({"b": 2})
    assert buf.snapshot() == [{"a": 1}, {"b": 2}]


@pytest.mark.asyncio
async def test_turn_event_buffer_close_marks_closed() -> None:
    buf = TurnEventBuffer()
    await buf.close()
    out: list[dict] = []
    async for event in buf.tail():
        out.append(event)
    assert out == []


@pytest.mark.asyncio
async def test_turn_event_buffer_snapshot_returns_copy() -> None:
    buf = TurnEventBuffer()
    await buf.append({"x": 1})
    snap = buf.snapshot()
    snap.append({"y": 2})
    assert buf.snapshot() == [{"x": 1}]


@pytest.mark.asyncio
async def test_turn_event_buffer_snapshot_filters_after_id_and_limit() -> None:
    buf = TurnEventBuffer()
    for idx in range(1, 6):
        await buf.append({"id": idx, "value": idx})
    await buf.append({"id": "not-a-number", "value": "ignored-after-cursor"})

    assert buf.snapshot(after_id=2, limit=2) == [
        {"id": 3, "value": 3},
        {"id": 4, "value": 4},
    ]


@pytest.mark.asyncio
async def test_turn_event_buffer_snapshot_filters_unindexed_events_by_buffer_cursor() -> (
    None
):
    buf = TurnEventBuffer()
    await buf.append({"value": "first"})
    await buf.append({"value": "second"})
    await buf.append({"value": "third"})

    snapshot = buf.snapshot(after_id=1, limit=1)

    assert len(snapshot) == 1
    assert snapshot[0]["value"] == "second"
    assert "_car_buffer_sequence_id" not in snapshot[0]


@pytest.mark.asyncio
async def test_turn_event_buffer_tail_yields_events_as_they_arrive() -> None:
    buf = TurnEventBuffer()
    seen: list[dict] = []

    async def _pump() -> None:
        await asyncio.sleep(0)
        await buf.append({"n": 1})
        await asyncio.sleep(0)
        await buf.append({"n": 2})
        await buf.close()

    pump = asyncio.create_task(_pump())
    async for event in buf.tail():
        seen.append(event)
    await pump
    assert seen == [{"n": 1}, {"n": 2}]


@pytest.mark.asyncio
async def test_turn_event_buffer_tail_stops_when_closed() -> None:
    buf = TurnEventBuffer()
    await buf.append({"done": True})
    await buf.close()
    events = [e async for e in buf.tail()]
    assert events == [{"done": True}]


@pytest.mark.asyncio
async def test_turn_event_buffer_concurrent_tail_and_append() -> None:
    buf = TurnEventBuffer()
    collected: list[dict] = []

    async def _consumer() -> None:
        async for event in buf.tail():
            collected.append(event)

    consumer = asyncio.create_task(_consumer())
    await asyncio.sleep(0)
    for i in range(5):
        await buf.append({"i": i})
        await asyncio.sleep(0)
    await buf.close()
    await consumer
    assert collected == [{"i": i} for i in range(5)]


@pytest.mark.asyncio
async def test_turn_event_buffer_tail_after_close_replays_and_stops() -> None:
    buf = TurnEventBuffer()
    await buf.append({"phase": "a"})
    await buf.append({"phase": "b"})
    await buf.close()
    first = [e async for e in buf.tail()]
    second = [e async for e in buf.tail()]
    assert first == [{"phase": "a"}, {"phase": "b"}]
    assert second == [{"phase": "a"}, {"phase": "b"}]


@pytest.mark.asyncio
async def test_turn_event_buffer_append_copies_event_dict() -> None:
    buf = TurnEventBuffer()
    original = {"k": 1}
    await buf.append(original)
    original["k"] = 99
    assert buf.snapshot() == [{"k": 1}]
