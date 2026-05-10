from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Generic, Optional, Protocol, TypeVar

__all__ = [
    "ChatOutboxKernel",
    "CoalesceKeyFn",
    "DeliveredCallback",
    "DeliveryFn",
    "InflightKeyFn",
    "NowFn",
    "OutboxAttemptResult",
    "OutboxRecordLike",
    "OutboxStoreLike",
    "RecordBoolFn",
    "RecordFn",
    "SleepFn",
    "coalesce_latest_records",
    "parse_next_attempt_at",
]

RecordT = TypeVar("RecordT", bound="OutboxRecordLike")
DeliveredIdT = TypeVar("DeliveredIdT")


class OutboxRecordLike(Protocol):
    @property
    def record_id(self) -> str: ...

    @property
    def attempts(self) -> int: ...

    @property
    def next_attempt_at(self) -> Optional[str]: ...

    @property
    def created_at(self) -> str: ...


class OutboxStoreLike(Protocol[RecordT]):
    async def enqueue_outbox(self, record: RecordT) -> RecordT: ...

    async def get_outbox(self, record_id: str) -> Optional[RecordT]: ...


@dataclass(frozen=True)
class OutboxAttemptResult(Generic[DeliveredIdT]):
    delivered: bool
    delivered_id: Optional[DeliveredIdT] = None


DeliveryFn = Callable[[RecordT], Awaitable[OutboxAttemptResult[DeliveredIdT]]]
RecordFn = Callable[[RecordT], Awaitable[None]]
RecordBoolFn = Callable[[RecordT], Awaitable[bool]]
DeliveredCallback = Callable[[RecordT, Optional[DeliveredIdT]], Awaitable[None]]
CoalesceKeyFn = Callable[[RecordT], Optional[str]]
InflightKeyFn = Callable[[RecordT], str]
NowFn = Callable[[], datetime]
SleepFn = Callable[[float], Awaitable[None]]


def parse_next_attempt_at(next_at_str: Optional[str]) -> Optional[datetime]:
    if not isinstance(next_at_str, str) or not next_at_str:
        return None
    try:
        return datetime.strptime(next_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def coalesce_latest_records(
    records: list[RecordT],
    *,
    coalesce_key: CoalesceKeyFn[RecordT],
) -> list[RecordT]:
    keyed: dict[str, RecordT] = {}
    unkeyed: list[RecordT] = []
    for record in records:
        key = coalesce_key(record)
        if key is None:
            unkeyed.append(record)
            continue
        existing = keyed.get(key)
        if existing is None or record.created_at >= existing.created_at:
            keyed[key] = record

    coalesced: list[RecordT] = []
    seen_ids: set[str] = set()
    for record in keyed.values():
        if record.record_id not in seen_ids:
            coalesced.append(record)
            seen_ids.add(record.record_id)
    for record in unkeyed:
        if record.record_id not in seen_ids:
            coalesced.append(record)
            seen_ids.add(record.record_id)
    return coalesced


class ChatOutboxKernel(Generic[RecordT, DeliveredIdT]):
    """Transport-independent retry/coalescing orchestration for chat outboxes."""

    def __init__(
        self,
        store: OutboxStoreLike[RecordT],
        *,
        deliver: DeliveryFn[RecordT, DeliveredIdT],
        cleanup_delivered: RecordFn[RecordT],
        drop_exhausted: RecordFn[RecordT],
        coalesce_key: CoalesceKeyFn[RecordT],
        inflight_key: InflightKeyFn[RecordT],
        logger: logging.Logger,
        max_attempts: int,
        immediate_retry_delays: tuple[float, ...],
        now_fn: Optional[NowFn] = None,
        sleep_fn: SleepFn = asyncio.sleep,
        on_delivered: Optional[DeliveredCallback[RecordT, DeliveredIdT]] = None,
        before_attempt: Optional[RecordBoolFn[RecordT]] = None,
        drop_direct_exhausted: bool = True,
        drop_all_flush_exhausted_before_coalesce: bool = True,
        callback_failed_event: str = "chat.outbox.delivery_callback_failed",
    ) -> None:
        self._store = store
        self._deliver = deliver
        self._cleanup_delivered = cleanup_delivered
        self._drop_exhausted = drop_exhausted
        self._coalesce_key = coalesce_key
        self._inflight_key = inflight_key
        self._logger = logger
        self._max_attempts = max(max_attempts, 1)
        self._immediate_retry_delays = immediate_retry_delays
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_fn
        self._on_delivered = on_delivered
        self._before_attempt = before_attempt
        self._drop_direct_exhausted = drop_direct_exhausted
        self._drop_all_flush_exhausted_before_coalesce = (
            drop_all_flush_exhausted_before_coalesce
        )
        self._callback_failed_event = callback_failed_event
        self._inflight: set[str] = set()
        self._lock: Optional[asyncio.Lock] = None

    def start(self) -> None:
        self._inflight = set()
        self._lock = asyncio.Lock()

    async def enqueue_and_retry(self, record: RecordT) -> bool:
        await self._store.enqueue_outbox(record)
        immediate_delays_iter = iter(self._immediate_retry_delays)
        while True:
            current = await self._store.get_outbox(record.record_id)
            if current is None:
                return False
            if current.attempts >= self._max_attempts:
                if self._drop_direct_exhausted:
                    await self._drop_exhausted(current)
                return False

            next_at = parse_next_attempt_at(current.next_attempt_at)
            if next_at is not None:
                sleep_duration = (next_at - self._now()).total_seconds()
                if sleep_duration > 0.01:
                    await self._sleep(sleep_duration)

            if await self.attempt_send(current):
                return True

            current = await self._store.get_outbox(record.record_id)
            if current is None:
                return False
            if current.attempts >= self._max_attempts:
                if self._drop_direct_exhausted:
                    await self._drop_exhausted(current)
                return False

            next_at = parse_next_attempt_at(current.next_attempt_at)
            if next_at is not None:
                continue

            try:
                delay = next(immediate_delays_iter)
            except StopIteration:
                return False
            if delay > 0:
                await self._sleep(delay)

    async def flush(self, records: list[RecordT]) -> None:
        now = self._now()
        pending = records
        if self._drop_all_flush_exhausted_before_coalesce:
            pending = []
            for record in records:
                if record.attempts >= self._max_attempts:
                    await self._drop_exhausted(record)
                else:
                    pending.append(record)

        for record in coalesce_latest_records(pending, coalesce_key=self._coalesce_key):
            if record.attempts >= self._max_attempts:
                await self._drop_exhausted(record)
                continue
            next_at = parse_next_attempt_at(record.next_attempt_at)
            if next_at is not None and now < next_at:
                continue
            await self.attempt_send(record)

    async def attempt_send(self, record: RecordT) -> bool:
        current = await self._store.get_outbox(record.record_id)
        if current is None:
            return False
        if current.attempts >= self._max_attempts:
            await self._drop_exhausted(current)
            return False
        if self._before_attempt is not None and await self._before_attempt(current):
            return False

        key = self._inflight_key(current)
        if not await self._mark_inflight(key):
            return False
        try:
            result = await self._deliver(current)
        finally:
            await self._clear_inflight(key)

        if not result.delivered:
            return False
        if self._on_delivered is not None:
            try:
                await self._on_delivered(current, result.delivered_id)
            except Exception:
                self._logger.warning(
                    "%s record_id=%s",
                    self._callback_failed_event,
                    current.record_id,
                    exc_info=True,
                )
        await self._cleanup_delivered(current)
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
