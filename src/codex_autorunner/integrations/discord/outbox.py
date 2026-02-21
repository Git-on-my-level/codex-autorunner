from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

from .state import DiscordStateStore, OutboxRecord

OUTBOX_RETRY_INTERVAL_SECONDS = 5.0
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_IMMEDIATE_RETRY_DELAYS = (0.0, 1.0, 2.0)

SendMessageFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _parse_next_attempt_at(next_at_str: Optional[str]) -> Optional[datetime]:
    if not isinstance(next_at_str, str) or not next_at_str:
        return None
    try:
        return datetime.strptime(next_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _extract_retry_after_seconds(exc: Exception) -> Optional[float]:
    current: Optional[BaseException] = exc
    while current is not None:
        retry_attr = getattr(current, "retry_after_seconds", None)
        if isinstance(retry_attr, (int, float)):
            return max(float(retry_attr), 0.0)
        if isinstance(current, httpx.HTTPStatusError):
            header = current.response.headers.get("Retry-After")
            if header:
                try:
                    return max(float(header), 0.0)
                except ValueError:
                    pass
        current = current.__cause__ or current.__context__
    return None


class DiscordOutboxManager:
    def __init__(
        self,
        store: DiscordStateStore,
        *,
        send_message: SendMessageFn,
        logger: logging.Logger,
        retry_interval_seconds: float = OUTBOX_RETRY_INTERVAL_SECONDS,
        max_attempts: int = OUTBOX_MAX_ATTEMPTS,
        immediate_retry_delays: tuple[float, ...] = OUTBOX_IMMEDIATE_RETRY_DELAYS,
        now_fn: Optional[Callable[[], datetime]] = None,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._send_message = send_message
        self._logger = logger
        self._retry_interval_seconds = max(retry_interval_seconds, 0.1)
        self._max_attempts = max(max_attempts, 1)
        self._immediate_retry_delays = immediate_retry_delays
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_fn
        self._inflight: set[str] = set()
        self._lock: Optional[asyncio.Lock] = None

    def start(self) -> None:
        self._inflight = set()
        self._lock = asyncio.Lock()

    async def run_loop(self) -> None:
        while True:
            await self._sleep(self._retry_interval_seconds)
            try:
                records = await self._store.list_outbox()
                if records:
                    await self._flush(records)
            except Exception as exc:
                self._logger.warning("discord.outbox.flush_failed: %s", exc)

    async def send_with_outbox(self, record: OutboxRecord) -> bool:
        await self._store.enqueue_outbox(record)
        immediate_delays_iter = iter(self._immediate_retry_delays)
        while True:
            current = await self._store.get_outbox(record.record_id)
            if current is None:
                return False
            if current.attempts >= self._max_attempts:
                return False

            next_at = _parse_next_attempt_at(current.next_attempt_at)
            if next_at is not None:
                sleep_duration = (next_at - self._now()).total_seconds()
                if sleep_duration > 0.01:
                    await self._sleep(sleep_duration)

            if await self._attempt_send(current):
                return True

            current = await self._store.get_outbox(record.record_id)
            if current is None:
                return False
            if current.attempts >= self._max_attempts:
                return False

            next_at = _parse_next_attempt_at(current.next_attempt_at)
            if next_at is not None:
                continue

            try:
                delay = next(immediate_delays_iter)
            except StopIteration:
                return False
            if delay > 0:
                await self._sleep(delay)

    async def _flush(self, records: list[OutboxRecord]) -> None:
        now = self._now()
        for record in records:
            next_at = _parse_next_attempt_at(record.next_attempt_at)
            if next_at is not None and now < next_at:
                continue
            await self._attempt_send(record)

    async def _attempt_send(self, record: OutboxRecord) -> bool:
        current = await self._store.get_outbox(record.record_id)
        if current is None:
            return False
        if current.attempts >= self._max_attempts:
            return False
        if not await self._mark_inflight(current.record_id):
            return False
        try:
            if current.operation != "send":
                await self._store.record_outbox_failure(
                    current.record_id,
                    error=f"Unsupported Discord outbox operation: {current.operation}",
                    retry_after_seconds=None,
                )
                return False
            await self._send_message(current.channel_id, current.payload_json)
        except Exception as exc:
            retry_after = _extract_retry_after_seconds(exc)
            await self._store.record_outbox_failure(
                current.record_id,
                error=str(exc),
                retry_after_seconds=retry_after,
            )
            self._logger.warning(
                "discord.outbox.send_failed record_id=%s attempts=%s retry_after=%s error=%s",
                current.record_id,
                current.attempts + 1,
                retry_after,
                exc,
            )
            return False
        finally:
            await self._clear_inflight(current.record_id)

        await self._store.mark_outbox_delivered(current.record_id)
        self._logger.info("discord.outbox.delivered record_id=%s", current.record_id)
        return True

    async def _mark_inflight(self, key: str) -> bool:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    async def _clear_inflight(self, key: str) -> None:
        if self._lock is None:
            return
        async with self._lock:
            self._inflight.discard(key)
