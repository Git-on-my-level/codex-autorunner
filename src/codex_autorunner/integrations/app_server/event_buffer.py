import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional

from ...core.app_server_ids import (
    extract_thread_id,
    extract_thread_id_for_turn,
    extract_turn_id,
)

TurnKey = tuple[str, str]
LOGGER = logging.getLogger("codex_autorunner.app_server")


def format_sse(event: str, data: object) -> str:
    payload = data if isinstance(data, str) else json.dumps(data)
    lines = payload.splitlines() or [""]
    parts = [f"event: {event}"]
    for line in lines:
        parts.append(f"data: {line}")
    return "\n".join(parts) + "\n\n"


@dataclass
class TurnEventEntry:
    thread_id: str
    turn_id: str
    events: list[dict] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    created_at: float = field(default_factory=time.monotonic)
    last_event_at: float = field(default_factory=time.monotonic)
    next_id: int = 1
    active_streams: int = 0
    context: dict[str, Any] = field(default_factory=dict)


class AppServerEventBuffer:
    def __init__(
        self,
        *,
        max_events_per_turn: int = 200,
        max_turns: int = 200,
        turn_ttl_seconds: float = 1800.0,
    ) -> None:
        self._entries: dict[TurnKey, TurnEventEntry] = {}
        self._turn_index: dict[str, str] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._lock_init = threading.Lock()
        self._max_events_per_turn = max_events_per_turn
        self._max_turns = max_turns
        self._turn_ttl_seconds = turn_ttl_seconds

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            with self._lock_init:
                if self._lock is None:
                    self._lock = asyncio.Lock()
        return self._lock

    async def register_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        if not thread_id or not turn_id:
            return
        entry = await self._ensure_entry(thread_id, turn_id)
        async with self._ensure_lock():
            self._turn_index[turn_id] = thread_id
        if context:
            entry.context.update(context)

    async def handle_notification(self, message: Dict[str, Any]) -> None:
        thread_id, turn_id = self._extract_turn_ids(message)
        if not thread_id or not turn_id:
            return
        entry = await self._ensure_entry(thread_id, turn_id)
        event = {
            "id": entry.next_id,
            "received_at": int(time.time() * 1000),
            "message": message,
        }
        entry.next_id += 1
        async with entry.condition:
            entry.events.append(event)
            if len(entry.events) > self._max_events_per_turn:
                entry.events = entry.events[-self._max_events_per_turn :]
            entry.last_event_at = time.monotonic()
            entry.condition.notify_all()
        context = dict(entry.context) if entry.context else {}
        async with self._ensure_lock():
            self._turn_index[turn_id] = thread_id
            self._prune_locked()
        self._emit_log_lines(context, message)

    async def stream(
        self,
        thread_id: str,
        turn_id: str,
        *,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[str]:
        entry = await self._ensure_entry(thread_id, turn_id)
        async with self._ensure_lock():
            entry.active_streams += 1
            self._turn_index[turn_id] = thread_id
        last_id = 0
        try:
            while True:
                async with entry.condition:
                    events = [e for e in entry.events if e["id"] > last_id]
                    if not events:
                        try:
                            await asyncio.wait_for(
                                entry.condition.wait(), timeout=heartbeat_interval
                            )
                        except asyncio.TimeoutError:
                            yield ": ping\n\n"
                        continue
                for event in events:
                    last_id = event["id"]
                    yield format_sse("app-server", event)
        finally:
            async with self._ensure_lock():
                entry.active_streams = max(0, entry.active_streams - 1)

    async def _ensure_entry(self, thread_id: str, turn_id: str) -> TurnEventEntry:
        key = (thread_id, turn_id)
        async with self._ensure_lock():
            entry = self._entries.get(key)
            if entry is None:
                entry = TurnEventEntry(thread_id=thread_id, turn_id=turn_id)
                self._entries[key] = entry
                self._turn_index[turn_id] = thread_id
            return entry

    def _extract_turn_ids(
        self, message: Dict[str, Any]
    ) -> tuple[Optional[str], Optional[str]]:
        params_raw = message.get("params")
        params: Dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}
        turn_id = extract_turn_id(params) or extract_turn_id(message)
        thread_id = (
            extract_thread_id_for_turn(params)
            or extract_thread_id(params)
            or extract_thread_id(message)
        )
        if not thread_id and turn_id:
            thread_id = self._turn_index.get(turn_id)
        return thread_id, turn_id

    def _prune_locked(self) -> None:
        if not self._entries:
            return
        now = time.monotonic()
        if self._turn_ttl_seconds > 0:
            for key, entry in list(self._entries.items()):
                if entry.active_streams:
                    continue
                if (now - entry.last_event_at) > self._turn_ttl_seconds:
                    self._entries.pop(key, None)
                    self._turn_index.pop(entry.turn_id, None)
        if self._max_turns > 0 and len(self._entries) > self._max_turns:
            inactive = [
                (key, entry)
                for key, entry in self._entries.items()
                if not entry.active_streams
            ]
            inactive.sort(key=lambda item: item[1].last_event_at)
            while len(self._entries) > self._max_turns and inactive:
                key, entry = inactive.pop(0)
                self._entries.pop(key, None)
                self._turn_index.pop(entry.turn_id, None)

    def _emit_log_lines(self, context: dict[str, Any], message: Dict[str, Any]) -> None:
        emit = context.get("emit")
        formatter = context.get("formatter")
        if emit is None or formatter is None:
            return
        try:
            lines = formatter.format_event(message)
        except Exception:
            LOGGER.warning("Failed to format app server event log line.", exc_info=True)
            return
        for line in lines:
            try:
                emit(line)
            except Exception:
                LOGGER.warning(
                    "Failed to emit app server event log line.",
                    exc_info=True,
                )
                continue
