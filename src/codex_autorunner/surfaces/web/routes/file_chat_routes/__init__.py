from __future__ import annotations

__all__ = [
    "FileChatRoutesState",
    "build_file_chat_runtime_routes",
]

import asyncio
from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class FileChatRoutesState:
    active_chats: Dict[str, asyncio.Event]
    chat_lock: asyncio.Lock
    turn_lock: asyncio.Lock
    current_by_target: Dict[str, Dict[str, Any]]
    current_by_client: Dict[str, Dict[str, Any]]
    last_by_client: Dict[str, Dict[str, Any]]

    def __init__(self) -> None:
        self.active_chats = {}
        self.chat_lock = asyncio.Lock()
        self.turn_lock = asyncio.Lock()
        self.current_by_target = {}
        self.current_by_client = {}
        self.last_by_client = {}
