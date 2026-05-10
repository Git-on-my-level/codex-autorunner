from __future__ import annotations

import time
from typing import Optional


class TelegramQueuedPlaceholderManager:
    def __init__(self) -> None:
        self._map: dict[tuple[int, int], int] = {}
        self._timestamps: dict[tuple[int, int], float] = {}

    def set(self, chat_id: int, message_id: int, placeholder_id: int) -> None:
        self._map[(chat_id, message_id)] = placeholder_id
        self._timestamps[(chat_id, message_id)] = time.monotonic()

    def get(self, chat_id: int, message_id: int) -> Optional[int]:
        return self._map.get((chat_id, message_id))

    def claim(self, chat_id: int, message_id: int) -> Optional[int]:
        placeholder_id = self._map.pop((chat_id, message_id), None)
        self._timestamps.pop((chat_id, message_id), None)
        return placeholder_id

    def clear(self, chat_id: int, message_id: int) -> None:
        self._map.pop((chat_id, message_id), None)
        self._timestamps.pop((chat_id, message_id), None)

    @property
    def map(self) -> dict[tuple[int, int], int]:
        return self._map

    @property
    def timestamps(self) -> dict[tuple[int, int], float]:
        return self._timestamps
