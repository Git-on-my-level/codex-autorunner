from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from codex_autorunner.integrations.chat.outbox_kernel import (
    ChatOutboxKernel,
    OutboxAttemptResult,
)


@dataclass
class _Record:
    record_id: str
    created_at: str
    attempts: int = 0
    next_attempt_at: Optional[str] = None
    operation_id: Optional[str] = None
    outbox_key: Optional[str] = None


class _Store:
    def __init__(self) -> None:
        self.records: dict[str, _Record] = {}

    async def enqueue_outbox(self, record: _Record) -> _Record:
        self.records[record.record_id] = record
        return record

    async def get_outbox(self, record_id: str) -> Optional[_Record]:
        return self.records.get(record_id)

    async def delete_outbox(self, record_id: str) -> None:
        self.records.pop(record_id, None)


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


def _kernel(
    store: _Store,
    *,
    deliver,
    max_attempts: int = 3,
    immediate_retry_delays: tuple[float, ...] = (0.0,),
    clock: Optional[_Clock] = None,
    callbacks: Optional[list[tuple[str, str | None]]] = None,
) -> ChatOutboxKernel[_Record, str]:
    async def cleanup(record: _Record) -> None:
        await store.delete_outbox(record.record_id)

    async def drop(record: _Record) -> None:
        await store.delete_outbox(record.record_id)

    async def on_delivered(record: _Record, delivered_id: Optional[str]) -> None:
        if callbacks is not None:
            callbacks.append((record.record_id, delivered_id))

    kwargs = {}
    if clock is not None:
        kwargs["now_fn"] = clock.now
        kwargs["sleep_fn"] = clock.sleep
    return ChatOutboxKernel(
        store,
        deliver=deliver,
        cleanup_delivered=cleanup,
        drop_exhausted=drop,
        coalesce_key=lambda record: (
            f"op:{record.operation_id}"
            if record.operation_id is not None
            else record.outbox_key
        ),
        inflight_key=lambda record: (
            f"op:{record.operation_id}" if record.operation_id else record.record_id
        ),
        logger=logging.getLogger("test"),
        max_attempts=max_attempts,
        immediate_retry_delays=immediate_retry_delays,
        on_delivered=on_delivered if callbacks is not None else None,
        **kwargs,
    )


@pytest.mark.anyio
async def test_kernel_retries_until_delivery_and_runs_callback() -> None:
    store = _Store()
    clock = _Clock()
    attempts = {"count": 0}
    callbacks: list[tuple[str, str | None]] = []

    async def deliver(record: _Record) -> OutboxAttemptResult[str]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            record.attempts += 1
            return OutboxAttemptResult(delivered=False)
        return OutboxAttemptResult(delivered=True, delivered_id="msg-1")

    kernel = _kernel(
        store,
        deliver=deliver,
        immediate_retry_delays=(2.0,),
        clock=clock,
        callbacks=callbacks,
    )
    kernel.start()

    delivered = await kernel.enqueue_and_retry(
        _Record(record_id="r1", created_at="2026-01-01T00:00:00Z")
    )

    assert delivered is True
    assert attempts["count"] == 2
    assert clock.sleeps == [2.0]
    assert callbacks == [("r1", "msg-1")]
    assert store.records == {}


@pytest.mark.anyio
async def test_kernel_honors_scheduled_retry() -> None:
    store = _Store()
    clock = _Clock()
    calls: list[str] = []
    future = (clock.now() + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def deliver(record: _Record) -> OutboxAttemptResult[str]:
        calls.append(record.record_id)
        return OutboxAttemptResult(delivered=True)

    kernel = _kernel(store, deliver=deliver, clock=clock)
    kernel.start()

    delivered = await kernel.enqueue_and_retry(
        _Record(
            record_id="scheduled",
            created_at="2026-01-01T00:00:00Z",
            next_attempt_at=future,
        )
    )

    assert delivered is True
    assert calls == ["scheduled"]
    assert clock.sleeps == [5.0]


@pytest.mark.anyio
async def test_kernel_flush_coalesces_latest_by_key_and_skips_backoff() -> None:
    store = _Store()
    clock = _Clock()
    delivered: list[str] = []

    async def deliver(record: _Record) -> OutboxAttemptResult[str]:
        delivered.append(record.record_id)
        return OutboxAttemptResult(delivered=True)

    kernel = _kernel(store, deliver=deliver, clock=clock)
    kernel.start()
    future = (clock.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = [
        _Record("old", "2026-01-01T00:00:00Z", operation_id="op-1"),
        _Record("new", "2026-01-01T00:01:00Z", operation_id="op-1"),
        _Record(
            "backed-off",
            "2026-01-01T00:02:00Z",
            outbox_key="edit-1",
            next_attempt_at=future,
        ),
    ]
    for record in records:
        await store.enqueue_outbox(record)

    await kernel.flush(records)

    assert delivered == ["new"]
    assert "old" in store.records
    assert "backed-off" in store.records


@pytest.mark.anyio
async def test_kernel_flush_drops_exhausted_without_delivery() -> None:
    store = _Store()
    calls: list[str] = []
    drops: list[str] = []

    async def deliver(record: _Record) -> OutboxAttemptResult[str]:
        calls.append(record.record_id)
        return OutboxAttemptResult(delivered=True)

    async def cleanup(record: _Record) -> None:
        await store.delete_outbox(record.record_id)

    async def drop(record: _Record) -> None:
        drops.append(record.record_id)
        await store.delete_outbox(record.record_id)

    kernel = ChatOutboxKernel(
        store,
        deliver=deliver,
        cleanup_delivered=cleanup,
        drop_exhausted=drop,
        coalesce_key=lambda _record: None,
        inflight_key=lambda record: record.record_id,
        logger=logging.getLogger("test"),
        max_attempts=2,
        immediate_retry_delays=(),
    )
    kernel.start()
    record = _Record("dead", "2026-01-01T00:00:00Z", attempts=2)
    await store.enqueue_outbox(record)

    await kernel.flush([record])

    assert calls == []
    assert drops == ["dead"]
    assert store.records == {}


@pytest.mark.anyio
async def test_kernel_suppresses_inflight_records_by_key() -> None:
    store = _Store()
    release = asyncio.Event()
    entered = asyncio.Event()
    calls: list[str] = []

    async def deliver(record: _Record) -> OutboxAttemptResult[str]:
        calls.append(record.record_id)
        entered.set()
        await release.wait()
        return OutboxAttemptResult(delivered=True)

    kernel = _kernel(store, deliver=deliver, immediate_retry_delays=())
    kernel.start()
    record = _Record("r1", "2026-01-01T00:00:00Z", operation_id="op-1")
    await store.enqueue_outbox(record)

    first = asyncio.create_task(kernel.attempt_send(record))
    await entered.wait()
    second = await kernel.attempt_send(record)
    release.set()
    first_result = await first

    assert first_result is True
    assert second is False
    assert calls == ["r1"]
