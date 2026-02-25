from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_autorunner.core.state import now_iso
from codex_autorunner.integrations.discord.outbox import DiscordOutboxManager
from codex_autorunner.integrations.discord.state import DiscordStateStore, OutboxRecord


class _RetryAfterError(Exception):
    def __init__(self, seconds: float) -> None:
        super().__init__("rate limited")
        self.retry_after_seconds = seconds


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current = self.current + timedelta(seconds=seconds)


@pytest.mark.anyio
async def test_outbox_retry_uses_retry_after_and_eventually_delivers(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls = {"count": 0}

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        calls["count"] += 1
        if calls["count"] == 1:
            raise _RetryAfterError(2.0)
        return {"id": "msg-1"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        immediate_retry_delays=(0.0,),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="r1",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "hello"},
                created_at=now_iso(),
            )
        )
        assert delivered is True
        assert calls["count"] == 2
        assert any(delay >= 1.9 for delay in clock.sleeps)
        assert await store.get_outbox("r1") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flush_skips_future_next_attempt_records(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls = {"count": 0}

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        calls["count"] += 1
        return {"id": "msg-1"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        record = OutboxRecord(
            record_id="r1",
            channel_id="chan-1",
            message_id=None,
            operation="send",
            payload_json={"content": "hello"},
            created_at=now_iso(),
        )
        await store.enqueue_outbox(record)
        await store.record_outbox_failure("r1", error="later", retry_after_seconds=10.0)

        records = await store.list_outbox()
        await manager._flush(records)
        assert calls["count"] == 0
        assert await store.get_outbox("r1") is not None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_delete_operation_is_supported(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls: list[tuple[str, str]] = []

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        return {"id": "msg-1"}

    async def delete_message(channel_id: str, message_id: str) -> None:
        calls.append((channel_id, message_id))

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        delete_message=delete_message,
        logger=logging.getLogger("test"),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="del-1",
                channel_id="chan-1",
                message_id="msg-123",
                operation="delete",
                payload_json={},
                created_at=now_iso(),
            )
        )
        assert delivered is True
        assert calls == [("chan-1", "msg-123")]
        assert await store.get_outbox("del-1") is None
    finally:
        await store.close()
