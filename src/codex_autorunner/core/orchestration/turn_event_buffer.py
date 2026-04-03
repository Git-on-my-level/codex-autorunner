from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class TurnEventBuffer:
    """Condition-backed buffer for streaming dict-shaped turn events."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._condition = asyncio.Condition()
        self._closed = False

    async def append(self, event: dict[str, Any]) -> None:
        async with self._condition:
            self._events.append(dict(event))
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._events)

    async def tail(self) -> AsyncIterator[dict[str, Any]]:
        next_index = 0
        while True:
            async with self._condition:
                while next_index >= len(self._events) and not self._closed:
                    await self._condition.wait()
                batch = list(self._events[next_index:])
                next_index += len(batch)
                should_stop = self._closed and next_index >= len(self._events)
            for event in batch:
                yield dict(event)
            if should_stop:
                break


__all__ = ["TurnEventBuffer"]
