from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from codex_autorunner.integrations.chat.adapter import (
    SendAttachmentRequest,
    SendTextRequest,
)
from codex_autorunner.integrations.chat.errors import ChatAdapterPermanentError
from codex_autorunner.integrations.chat.models import (
    ChatAction,
    ChatInteractionRef,
    ChatMessageRef,
    ChatThreadRef,
)
from codex_autorunner.integrations.telegram.adapter import TelegramBotClient
from codex_autorunner.integrations.telegram.chat_adapter import TelegramChatAdapter


class _DummyPoller:
    async def poll(self, *, timeout: int = 30) -> list[Any]:
        _ = timeout
        return []


@pytest.mark.anyio
async def test_adapter_ops_call_expected_telegram_methods(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "sendDocument":
            payload = {"_multipart": True}
        elif request.content:
            payload = json.loads(request.content.decode("utf-8"))
        else:
            payload = {}
        calls.append((method, payload))
        if method in ("sendMessage", "sendDocument"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 321}})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    file_path = tmp_path / "payload.txt"
    file_path.write_text("hello", encoding="utf-8")
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    bot = TelegramBotClient("test-token", client=http_client)
    adapter = TelegramChatAdapter(bot=bot, poller=_DummyPoller())
    thread = ChatThreadRef(platform="telegram", chat_id="123", thread_id="45")
    reply_ref = ChatMessageRef(thread=thread, message_id="12")

    try:
        sent = await adapter.send_text(
            SendTextRequest(
                thread=thread,
                text="hello world",
                reply_to=reply_ref,
                parse_mode="Markdown",
                actions=(ChatAction(label="Resume", action_id="resume:1"),),
            )
        )
        await adapter.edit_text(sent, "edited")
        await adapter.delete_message(sent)
        await adapter.send_attachment(
            SendAttachmentRequest(
                thread=thread,
                file_path=str(file_path),
                caption="cap",
                reply_to=reply_ref,
            )
        )
        await adapter.ack_interaction(
            ChatInteractionRef(thread=thread, interaction_id="cb-123"),
            text="Ack",
        )
    finally:
        await bot.close()

    methods = [method for method, _ in calls]
    assert methods == [
        "sendMessage",
        "editMessageText",
        "deleteMessage",
        "sendDocument",
        "answerCallbackQuery",
    ]

    send_payload = calls[0][1]
    assert send_payload["chat_id"] == 123
    assert send_payload["message_thread_id"] == 45
    assert send_payload["reply_to_message_id"] == 12
    assert send_payload["parse_mode"] == "Markdown"
    assert send_payload["reply_markup"]["inline_keyboard"][0][0]["text"] == "Resume"
    assert (
        send_payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        == "resume:1"
    )

    edit_payload = calls[1][1]
    assert edit_payload["chat_id"] == 123
    assert edit_payload["message_id"] == 321
    assert edit_payload["message_thread_id"] == 45

    delete_payload = calls[2][1]
    assert delete_payload["chat_id"] == 123
    assert delete_payload["message_id"] == 321

    answer_payload = calls[4][1]
    assert answer_payload["callback_query_id"] == "cb-123"
    assert answer_payload["text"] == "Ack"


@pytest.mark.anyio
async def test_send_text_rejects_non_numeric_message_id_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert method == "sendMessage"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": "bad"}})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    bot = TelegramBotClient("test-token", client=http_client)
    adapter = TelegramChatAdapter(bot=bot, poller=_DummyPoller())
    thread = ChatThreadRef(platform="telegram", chat_id="123", thread_id=None)
    try:
        with pytest.raises(ChatAdapterPermanentError, match="message_id"):
            await adapter.send_text(SendTextRequest(thread=thread, text="hello"))
    finally:
        await bot.close()


@pytest.mark.anyio
async def test_ack_interaction_uses_optional_chat_and_thread_ids() -> None:
    payloads: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    bot = TelegramBotClient("test-token", client=http_client)
    adapter = TelegramChatAdapter(bot=bot, poller=_DummyPoller())
    interaction = ChatInteractionRef(
        thread=ChatThreadRef(platform="telegram", chat_id="not-int", thread_id=None),
        interaction_id="cb-xyz",
    )
    try:
        await adapter.ack_interaction(interaction)
    finally:
        await bot.close()

    assert payloads == [{"callback_query_id": "cb-xyz"}]
