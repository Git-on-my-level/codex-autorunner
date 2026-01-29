import asyncio
import logging
import time
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
        calls = []

        async def send_message(
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> None:
            calls.append(text)

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

        assert len(calls) == 1
        assert calls[0] == "hello world"
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

        await asyncio.sleep(3.5)
        assert task.done()

        assert len(attempt_times) == 2
        # next_attempt_at is stored in whole seconds, allow small slack
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

        await asyncio.sleep(2.5)
        assert task1.done()

        assert len(chat1_times) >= 1
        assert len(chat2_times) >= 1
    finally:
        await store.close()
