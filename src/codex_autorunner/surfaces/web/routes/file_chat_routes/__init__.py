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


def build_file_chat_runtime_routes():
    from .runtime import (
        active_for_client,
        begin_turn_state,
        clear_interrupt_event,
        finalize_turn_state,
        get_or_create_interrupt_event,
        get_state,
        last_for_client,
        update_turn_state,
    )
    from . import execution, targets
    from .. import shared

    return {
        "get_state": get_state,
        "get_or_create_interrupt_event": get_or_create_interrupt_event,
        "clear_interrupt_event": clear_interrupt_event,
        "begin_turn_state": begin_turn_state,
        "update_turn_state": update_turn_state,
        "finalize_turn_state": finalize_turn_state,
        "active_for_client": active_for_client,
        "last_for_client": last_for_client,
        "targets": targets,
        "execution": execution,
    }
