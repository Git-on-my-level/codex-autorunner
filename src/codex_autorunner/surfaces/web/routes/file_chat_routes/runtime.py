from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, cast

from fastapi import HTTPException, Request

from . import FileChatRoutesState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def get_or_create_interrupt_event(request: Request, key: str) -> asyncio.Event:
    s = get_state(request)
    async with s.chat_lock:
        if key not in s.active_chats:
            s.active_chats[key] = asyncio.Event()
        return s.active_chats[key]


async def clear_interrupt_event(request: Request, key: str) -> None:
    s = get_state(request)
    async with s.chat_lock:
        s.active_chats.pop(key, None)


async def begin_turn_state(
    request: Request, target: Any, client_turn_id: Optional[str]
) -> None:
    s = get_state(request)
    async with s.turn_lock:
        state: Dict[str, Any] = {
            "client_turn_id": client_turn_id or "",
            "target": target.target,
            "status": "starting",
            "agent": None,
            "thread_id": None,
            "turn_id": None,
        }
        s.current_by_target[target.state_key] = state
        if client_turn_id:
            s.current_by_client[client_turn_id] = state


async def update_turn_state(request: Request, target: Any, **updates: Any) -> None:
    s = get_state(request)
    async with s.turn_lock:
        state = s.current_by_target.get(target.state_key)
        if not state:
            return
        for key, value in updates.items():
            if value is None:
                continue
            state[key] = value
        cid = state.get("client_turn_id") or ""
        if cid:
            s.current_by_client[cid] = state


async def finalize_turn_state(
    request: Request, target: Any, result: Dict[str, Any]
) -> None:
    s = get_state(request)
    async with s.turn_lock:
        state = s.current_by_target.pop(target.state_key, None)
        cid = ""
        if state:
            cid = state.get("client_turn_id", "") or ""
        if cid:
            s.current_by_client.pop(cid, None)
            s.last_by_client[cid] = dict(result or {})


async def active_for_client(
    request: Request, client_turn_id: Optional[str]
) -> Dict[str, Any]:
    if not client_turn_id:
        return {}
    s = get_state(request)
    async with s.turn_lock:
        return dict(s.current_by_client.get(client_turn_id, {}))


async def last_for_client(
    request: Request, client_turn_id: Optional[str]
) -> Dict[str, Any]:
    if not client_turn_id:
        return {}
    s = get_state(request)
    async with s.turn_lock:
        return dict(s.last_by_client.get(client_turn_id, {}))


def get_state(request: Request) -> FileChatRoutesState:
    if not hasattr(request.app.state, "file_chat_routes_state"):
        request.app.state.file_chat_routes_state = FileChatRoutesState()
    return cast(FileChatRoutesState, request.app.state.file_chat_routes_state)
