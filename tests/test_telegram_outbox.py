import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import pytest

from codex_autorunner.core.state import now_iso
from codex_autorunner.integrations.telegram import outbox as outbox_module
from codex_autorunner.integrations.telegram.outbox import TelegramOutboxManager
from codex_autorunner.integrations.telegram.state import (
    OutboxRecord,
    TelegramStateStore,
)


@pytest.mark.anyio
async def test_outbox_immediate_retry_respects_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(outbox_module, "OUTBOX_IMMEDIATE_RETRY_DELAYS", [0, 0, 0])
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        calls = {"count": 0}

        async def send_message(
            _chat_id: int,
            _text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> None:
            calls["count"] += 1
            raise RuntimeError("fail")

        async def edit_message_text(*_args, **_kwargs) -> bool:
            return False

        async def delete_message(*_args, **_kwargs) -> bool:
            return False

        manager = TelegramOutboxManager(
            store,
            send_message=send_message,
            edit_message_text=edit_message_text,
            delete_message=delete_message,
            logger=logging.getLogger("test"),
        )
        manager.start()
        record = OutboxRecord(
            record_id="r1",
            chat_id=123,
            thread_id=None,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="hello",
            created_at=now_iso(),
        )
        delivered = await manager.send_message_with_outbox(record)

        assert delivered is False
        assert calls["count"] == 2
        stored = await store.get_outbox("r1")
        assert stored is not None
        assert stored.attempts == 2
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_coalescing_collapses_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_IMMEDIATE_RETRY_DELAYS", [])
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        send_calls: list[str] = []
        edit_calls: list[tuple[int, str]] = []

        async def send_message(
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> None:
            send_calls.append(text)

        async def edit_message_text(
            _chat_id: int,
            message_id: int,
            text: str,
            *,
            message_thread_id: Optional[int] = None,
        ) -> bool:
            _ = message_thread_id
            edit_calls.append((message_id, text))
            return True

        async def delete_message(*_args, **_kwargs) -> bool:
            return False

        manager = TelegramOutboxManager(
            store,
            send_message=send_message,
            edit_message_text=edit_message_text,
            delete_message=delete_message,
            logger=logging.getLogger("test"),
        )
        manager.start()

        from codex_autorunner.integrations.telegram.outbox import _outbox_key

        outbox_key = _outbox_key(123, 456, 789, "edit")

        record1 = OutboxRecord(
            record_id="r1",
            chat_id=123,
            thread_id=456,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="hello",
            created_at=now_iso(),
            operation="edit",
            message_id=789,
            outbox_key=outbox_key,
        )
        await store.enqueue_outbox(record1)

        record2 = OutboxRecord(
            record_id="r2",
            chat_id=123,
            thread_id=456,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="hello world",
            created_at=now_iso(),
            operation="edit",
            message_id=789,
            outbox_key=outbox_key,
        )
        await store.enqueue_outbox(record2)

        records = await store.list_outbox()
        assert len(records) == 2

        await manager._flush(records)

        assert send_calls == []
        assert edit_calls == [(789, "hello world")]
        records = await store.list_outbox()
        assert len(records) == 0
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.anyio
async def test_outbox_retry_after_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_IMMEDIATE_RETRY_DELAYS", [0])
    monkeypatch.setattr(outbox_module, "OUTBOX_RETRY_INTERVAL_SECONDS", 0.1)
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        attempt_times = []

        class RetryAfterError(Exception):
            def __init__(self) -> None:
                response = httpx.Response(
                    429,
                    headers={"Retry-After": "2"},
                    request=httpx.Request("POST", "https://api.telegram.org/"),
                )
                super().__init__("Too many requests")
                self.__cause__ = httpx.HTTPStatusError(
                    "Too many requests", request=response.request, response=response
                )

        async def send_message(
            _chat_id: int,
            _text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> None:
            attempt_times.append(time.time())
            if len(attempt_times) == 1:
                raise RetryAfterError()

        async def edit_message_text(*_args, **_kwargs) -> bool:
            return False

        async def delete_message(*_args, **_kwargs) -> bool:
            return False

        manager = TelegramOutboxManager(
            store,
            send_message=send_message,
            edit_message_text=edit_message_text,
            delete_message=delete_message,
            logger=logging.getLogger("test"),
        )
        manager.start()

        record = OutboxRecord(
            record_id="r1",
            chat_id=123,
            thread_id=None,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="hello",
            created_at=now_iso(),
        )
        task = asyncio.create_task(manager.send_message_with_outbox(record))

        await asyncio.wait_for(task, timeout=3.0)
        assert task.done()

        assert len(attempt_times) == 2
        # next_attempt_at is stored in whole seconds, allow small slack.
        assert attempt_times[1] - attempt_times[0] >= 1.0
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.anyio
async def test_outbox_per_chat_scheduling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_IMMEDIATE_RETRY_DELAYS", [0])
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:

        class RetryAfterError(Exception):
            def __init__(self) -> None:
                response = httpx.Response(
                    429,
                    headers={"Retry-After": "1"},
                    request=httpx.Request("POST", "https://api.telegram.org/"),
                )
                super().__init__("Too many requests")
                self.__cause__ = httpx.HTTPStatusError(
                    "Too many requests", request=response.request, response=response
                )

        chat1_times = []
        chat2_times = []

        async def send_message(
            chat_id: int,
            _text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> None:
            if chat_id == 123:
                chat1_times.append(time.time())
                if len(chat1_times) == 1:
                    raise RetryAfterError()
            elif chat_id == 456:
                chat2_times.append(time.time())

        async def edit_message_text(*_args, **_kwargs) -> bool:
            return False

        async def delete_message(*_args, **_kwargs) -> bool:
            return False

        manager = TelegramOutboxManager(
            store,
            send_message=send_message,
            edit_message_text=edit_message_text,
            delete_message=delete_message,
            logger=logging.getLogger("test"),
        )
        manager.start()

        record1 = OutboxRecord(
            record_id="r1",
            chat_id=123,
            thread_id=None,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="hello 123",
            created_at=now_iso(),
        )
        record2 = OutboxRecord(
            record_id="r2",
            chat_id=456,
            thread_id=None,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="hello 456",
            created_at=now_iso(),
        )

        task1 = asyncio.create_task(manager.send_message_with_outbox(record1))
        await asyncio.sleep(0.2)
        await manager.send_message_with_outbox(record2)

        await asyncio.wait_for(task1, timeout=2.0)
        assert task1.done()

        assert len(chat1_times) >= 1
        assert len(chat2_times) >= 1
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_coalescing_by_operation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_IMMEDIATE_RETRY_DELAYS", [])
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        sent: list[str] = []

        async def send_message(
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> int:
            sent.append(text)
            return len(sent)

        async def edit_message_text(*_args, **_kwargs) -> bool:
            return False

        async def delete_message(*_args, **_kwargs) -> bool:
            return False

        manager = TelegramOutboxManager(
            store,
            send_message=send_message,
            edit_message_text=edit_message_text,
            delete_message=delete_message,
            logger=logging.getLogger("test"),
        )
        manager.start()

        ts = now_iso()
        record1 = OutboxRecord(
            record_id="r1",
            chat_id=123,
            thread_id=None,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="stale",
            created_at=ts,
            operation_id="op-100",
        )
        record2 = OutboxRecord(
            record_id="r2",
            chat_id=123,
            thread_id=None,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="fresh",
            created_at=ts,
            operation_id="op-100",
        )
        record3 = OutboxRecord(
            record_id="r3",
            chat_id=123,
            thread_id=None,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="other",
            created_at=ts,
            operation_id="op-200",
        )
        await store.enqueue_outbox(record1)
        await store.enqueue_outbox(record2)
        await store.enqueue_outbox(record3)

        records = await store.list_outbox()
        assert len(records) == 3

        await manager._flush(records)
        assert len(sent) == 2
        assert "fresh" in sent
        assert "other" in sent
        assert "stale" not in sent

        records = await store.list_outbox()
        assert len(records) == 0
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_operation_id_delivery_cleans_all_same_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_IMMEDIATE_RETRY_DELAYS", [])
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        sent: list[str] = []

        async def send_message(
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> int:
            sent.append(text)
            return len(sent)

        async def edit_message_text(*_args, **_kwargs) -> bool:
            return False

        async def delete_message(*_args, **_kwargs) -> bool:
            return False

        manager = TelegramOutboxManager(
            store,
            send_message=send_message,
            edit_message_text=edit_message_text,
            delete_message=delete_message,
            logger=logging.getLogger("test"),
        )
        manager.start()

        ts_early = "2026-01-01T00:00:00Z"
        ts_late = "2026-01-01T00:01:00Z"
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="r1",
                chat_id=123,
                thread_id=None,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="old",
                created_at=ts_early,
                operation_id="op-clean",
            )
        )
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="r2",
                chat_id=123,
                thread_id=None,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="new",
                created_at=ts_late,
                operation_id="op-clean",
            )
        )

        records = await store.list_outbox()
        await manager._flush(records)

        assert sent == ["new"]
        records = await store.list_outbox()
        assert len(records) == 0
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_skips_ready_older_record_when_newer_same_op_is_backed_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_IMMEDIATE_RETRY_DELAYS", [])
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        sent: list[str] = []

        async def send_message(
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> int:
            sent.append(text)
            return len(sent)

        async def edit_message_text(*_args, **_kwargs) -> bool:
            return False

        async def delete_message(*_args, **_kwargs) -> bool:
            return False

        manager = TelegramOutboxManager(
            store,
            send_message=send_message,
            edit_message_text=edit_message_text,
            delete_message=delete_message,
            logger=logging.getLogger("test"),
        )
        manager.start()

        await store.enqueue_outbox(
            OutboxRecord(
                record_id="old-ready",
                chat_id=123,
                thread_id=None,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="old",
                created_at="2026-01-01T00:00:00Z",
                operation_id="op-backoff",
            )
        )
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="new-backoff",
                chat_id=123,
                thread_id=None,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="new",
                created_at="2026-01-01T00:01:00Z",
                next_attempt_at=(
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                operation_id="op-backoff",
            )
        )

        await manager._flush(await store.list_outbox())
        assert sent == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_inflight_dedup_by_operation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(outbox_module, "OUTBOX_IMMEDIATE_RETRY_DELAYS", [0])
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        send_count = {"n": 0}

        async def send_message(
            _chat_id: int,
            _text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> int:
            send_count["n"] += 1
            return send_count["n"]

        async def edit_message_text(*_args, **_kwargs) -> bool:
            return False

        async def delete_message(*_args, **_kwargs) -> bool:
            return False

        manager = TelegramOutboxManager(
            store,
            send_message=send_message,
            edit_message_text=edit_message_text,
            delete_message=delete_message,
            logger=logging.getLogger("test"),
        )
        manager.start()

        record = OutboxRecord(
            record_id="r-dedup",
            chat_id=123,
            thread_id=None,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="hello",
            created_at=now_iso(),
            operation_id="op-dedup-test",
        )
        delivered = await manager.send_message_with_outbox(record)
        assert delivered is True
        assert send_count["n"] == 1
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_replay_after_send_failure_with_operation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(outbox_module, "OUTBOX_IMMEDIATE_RETRY_DELAYS", [0])
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        attempts = {"n": 0}

        async def send_message(
            _chat_id: int,
            _text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> int:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient failure")
            return 999

        async def edit_message_text(*_args, **_kwargs) -> bool:
            return False

        async def delete_message(*_args, **_kwargs) -> bool:
            return False

        manager = TelegramOutboxManager(
            store,
            send_message=send_message,
            edit_message_text=edit_message_text,
            delete_message=delete_message,
            logger=logging.getLogger("test"),
        )
        manager.start()

        ts = now_iso()
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="r-replay",
                chat_id=123,
                thread_id=None,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="replay-me",
                created_at=ts,
                operation_id="op-replay-test",
            )
        )

        records = await store.list_outbox()
        assert len(records) == 1
        assert records[0].operation_id == "op-replay-test"

        await manager._flush(records)
        assert attempts["n"] == 1

        failed = await store.get_outbox("r-replay")
        assert failed is not None
        assert failed.operation_id == "op-replay-test"
        assert failed.attempts == 1

        records2 = await store.list_outbox()
        await manager._flush(records2)
        assert attempts["n"] == 2

        remaining = await store.list_outbox()
        assert len(remaining) == 0
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_give_up_invokes_delivery_callback_with_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_MAX_ATTEMPTS", 1)
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    callbacks: list[tuple[str, int | None]] = []
    try:

        async def send_message(
            _chat_id: int,
            _text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> int:
            _ = thread_id, reply_to
            raise RuntimeError("boom")

        async def edit_message_text(*_args, **_kwargs) -> bool:
            return False

        async def delete_message(*_args, **_kwargs) -> bool:
            return False

        async def on_delivered(
            record: OutboxRecord,
            delivered_message_id: Optional[int],
        ) -> None:
            callbacks.append((record.record_id, delivered_message_id))

        manager = TelegramOutboxManager(
            store,
            send_message=send_message,
            edit_message_text=edit_message_text,
            delete_message=delete_message,
            logger=logging.getLogger("test"),
            on_delivered=on_delivered,
        )
        manager.start()

        await store.enqueue_outbox(
            OutboxRecord(
                record_id="give-up",
                chat_id=123,
                thread_id=None,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="hello",
                created_at=now_iso(),
                operation="send",
                message_id=None,
            )
        )

        await manager._flush(await store.list_outbox())
        await manager._flush(await store.list_outbox())

        assert callbacks == [("give-up", None)]
        assert await store.list_outbox() == []
    finally:
        await store.close()
