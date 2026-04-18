from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Optional

from ...core.logging_utils import log_event
from .adapter import TelegramAPIError

TYPING_HEARTBEAT_INTERVAL_SECONDS = 4.0


class TelegramTypingManager:
    def __init__(
        self,
        *,
        owner: Any,
        logger: logging.Logger,
    ) -> None:
        self._owner = owner
        self._logger = logger
        self._sessions: dict[tuple[int, Optional[int]], int] = {}
        self._tasks: dict[tuple[int, Optional[int]], asyncio.Task[None]] = {}
        self._lock: Optional[asyncio.Lock] = None

    def _ensure_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._lock
        lock_loop = getattr(lock, "_loop", None) if lock else None
        if (
            lock is None
            or lock_loop is None
            or lock_loop is not loop
            or lock_loop.is_closed()
        ):
            lock = asyncio.Lock()
            self._lock = lock
        return lock

    async def is_active(self, key: tuple[int, Optional[int]]) -> bool:
        lock = self._ensure_lock()
        async with lock:
            return self._sessions.get(key, 0) > 0

    async def _indicator_loop(self, chat_id: int, thread_id: Optional[int]) -> None:
        key = (chat_id, thread_id)
        bot = getattr(self._owner, "_bot", None)
        send_chat_action = getattr(bot, "send_chat_action", None) if bot else None
        if not callable(send_chat_action):
            return
        try:
            while True:
                try:
                    await send_chat_action(
                        chat_id,
                        action="typing",
                        message_thread_id=thread_id,
                    )
                except TelegramAPIError as exc:
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "telegram.typing.send.failed",
                        chat_id=chat_id,
                        thread_id=thread_id,
                        exc=exc,
                    )
                await asyncio.sleep(TYPING_HEARTBEAT_INTERVAL_SECONDS)
                if not await self.is_active(key):
                    return
        finally:
            lock = self._ensure_lock()
            async with lock:
                task = self._tasks.get(key)
                if task is asyncio.current_task():
                    self._tasks.pop(key, None)

    async def begin(self, chat_id: int, thread_id: Optional[int]) -> None:
        key = (chat_id, thread_id)
        lock = self._ensure_lock()
        async with lock:
            self._sessions[key] = self._sessions.get(key, 0) + 1
            task = self._tasks.get(key)
            if task is not None and not task.done():
                return
            coro = self._indicator_loop(chat_id, thread_id)
            try:
                spawn_task = getattr(self._owner, "_spawn_task", None)
                if callable(spawn_task):
                    self._tasks[key] = spawn_task(coro)
                else:
                    self._tasks[key] = asyncio.create_task(coro)
            except (OSError, RuntimeError, ValueError):
                coro.close()
                count = self._sessions.get(key, 0)
                if count <= 1:
                    self._sessions.pop(key, None)
                else:
                    self._sessions[key] = count - 1
                raise

    async def end(self, chat_id: int, thread_id: Optional[int]) -> None:
        key = (chat_id, thread_id)
        task_to_cancel: Optional[asyncio.Task[None]] = None
        lock = self._ensure_lock()
        async with lock:
            count = self._sessions.get(key)
            if count is None:
                return
            if count > 1:
                self._sessions[key] = count - 1
                return
            self._sessions.pop(key, None)
            task_to_cancel = self._tasks.pop(key, None)
        if task_to_cancel is not None and not task_to_cancel.done():
            task_to_cancel.cancel()
            with suppress(asyncio.CancelledError):
                await task_to_cancel
