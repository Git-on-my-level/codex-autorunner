from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


def _coerce_event_id(value: Any) -> int:
    try:
        event_id = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, event_id)


def _event_cursor(event: tuple[int, dict[str, Any]]) -> int:
    sequence_id, payload = event
    event_id = _coerce_event_id(payload.get("id"))
    if event_id > 0:
        return event_id
    if sequence_id > 0:
        return sequence_id
    return 0


class TurnEventBuffer:
    """Condition-backed buffer for streaming dict-shaped turn events."""

    def __init__(self) -> None:
        self._events: list[tuple[int, dict[str, Any]]] = []
        self._condition = asyncio.Condition()
        self._closed = False
        self._next_sequence_id = 1

    async def append(self, event: dict[str, Any]) -> None:
        async with self._condition:
            buffered = dict(event)
            sequence_id = self._next_sequence_id
            self._next_sequence_id += 1
            self._events.append((sequence_id, buffered))
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    def snapshot(
        self, *, after_id: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        min_id = max(0, int(after_id or 0))
        events = self._events
        if min_id > 0:
            events = [event for event in events if _event_cursor(event) > min_id]
        if limit is not None:
            events = events[: max(0, int(limit))]
        return [dict(event) for _sequence_id, event in events]

    async def tail(self) -> AsyncIterator[dict[str, Any]]:
        next_index = 0
        while True:
            async with self._condition:
                while next_index >= len(self._events) and not self._closed:
                    await self._condition.wait()
                batch = list(self._events[next_index:])
                next_index += len(batch)
                should_stop = self._closed and next_index >= len(self._events)
            for _sequence_id, event in batch:
                yield dict(event)
            if should_stop:
                break


__all__ = ["TurnEventBuffer"]
