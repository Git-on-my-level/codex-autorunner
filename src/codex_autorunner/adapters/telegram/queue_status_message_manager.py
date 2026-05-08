from __future__ import annotations

import time
from typing import Optional


class TelegramQueueStatusMessageManager:
    def __init__(self) -> None:
        self._map: dict[tuple[int, Optional[int]], int] = {}
        self._timestamps: dict[tuple[int, Optional[int]], float] = {}

    def set(self, chat_id: int, thread_id: Optional[int], message_id: int) -> None:
        key = (chat_id, thread_id)
        self._map[key] = message_id
        self._timestamps[key] = time.monotonic()

    def get(self, chat_id: int, thread_id: Optional[int]) -> Optional[int]:
        return self._map.get((chat_id, thread_id))

    def clear(self, chat_id: int, thread_id: Optional[int]) -> None:
        key = (chat_id, thread_id)
        self._map.pop(key, None)
        self._timestamps.pop(key, None)

    @property
    def map(self) -> dict[tuple[int, Optional[int]], int]:
        return self._map

    @property
    def timestamps(self) -> dict[tuple[int, Optional[int]], float]:
        return self._timestamps
