from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.telegram.handlers.approvals import (
    TelegramApprovalHandlers,
)


class _StoreStub:
    def __init__(self) -> None:
        self.upserted_records: list[object] = []
        self.cleared_request_ids: list[str] = []

    async def upsert_pending_approval(self, record: object) -> object:
        self.upserted_records.append(record)
        return record

    async def clear_pending_approval(self, request_id: str) -> None:
        self.cleared_request_ids.append(request_id)


class _RouterStub:
    def __init__(self, runtime: object) -> None:
        self._runtime = runtime

    def runtime_for(self, _topic_key: str) -> object:
        return self._runtime


class _BotStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: object = None,
        parse_mode: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": message_thread_id,
                "reply_to": reply_to_message_id,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )
        return {"message_id": 321}


class _TelegramApprovalHarness(TelegramApprovalHandlers):
    def __init__(self) -> None:
        self._logger = logging.getLogger("test.telegram.approvals")
        self._store = _StoreStub()
        self._bot = _BotStub()
        self._pending_approvals: dict[str, object] = {}
        self._runtime = SimpleNamespace(pending_request_id=None)
        self.__dict__["_router"] = _RouterStub(self._runtime)
        self._context = SimpleNamespace(
            chat_id=123,
            thread_id=456,
            topic_key="topic-1",
            reply_to_message_id=789,
        )

    @property
    def _router(self) -> _RouterStub:
        return self.__dict__["_router"]

    def _resolve_turn_context(
        self, turn_id: str, thread_id: str | None = None
    ) -> object | None:
        assert turn_id == "turn-1"
        assert thread_id == "codex-thread-1"
        return self._context

    def _prepare_outgoing_text(
        self,
        text: str,
        *,
        chat_id: int,
        thread_id: int | None,
        reply_to: int | None,
        topic_key: str,
        codex_thread_id: str | None,
    ) -> tuple[str, str]:
        _ = chat_id, thread_id, reply_to, topic_key, codex_thread_id
        return text, "HTML"

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
    ) -> None:
        _ = chat_id, text, thread_id, reply_to

    async def _edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: object = None,
    ) -> bool:
        _ = chat_id, message_id, text, reply_markup
        return True

    def _touch_cache_timestamp(self, bucket: str, key: str) -> None:
        _ = bucket, key


@pytest.mark.anyio
async def test_telegram_approval_request_accepts_zero_request_id() -> None:
    handlers = _TelegramApprovalHarness()

    request = {
        "id": 0,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "turnId": "turn-1",
            "threadId": "codex-thread-1",
            "command": ["/bin/zsh", "-c", "pwd"],
        },
    }

    task = asyncio.create_task(handlers._handle_approval_request(request))
    await asyncio.sleep(0)

    assert handlers._bot.calls
    assert handlers._store.upserted_records
    assert "0" in handlers._pending_approvals
    assert handlers._runtime.pending_request_id == "0"

    pending = handlers._pending_approvals.pop("0")
    pending.future.set_result("accept")

    assert await task == "accept"
