from __future__ import annotations

from typing import Optional

import pytest

from codex_autorunner.integrations.chat.callbacks import decode_logical_callback
from codex_autorunner.integrations.chat.models import (
    ChatInteractionEvent,
    ChatMessageEvent,
)
from codex_autorunner.integrations.telegram.adapter import (
    TelegramAudio,
    TelegramCallbackQuery,
    TelegramDocument,
    TelegramMessage,
    TelegramPhotoSize,
    TelegramUpdate,
    TelegramVoice,
)
from codex_autorunner.integrations.telegram.chat_adapter import TelegramChatAdapter


class _DummyPoller:
    def __init__(self, updates: list[TelegramUpdate]) -> None:
        self.updates = updates
        self.last_timeout: Optional[int] = None

    async def poll(self, *, timeout: int = 30) -> list[TelegramUpdate]:
        self.last_timeout = timeout
        return list(self.updates)


@pytest.mark.anyio
async def test_poll_events_maps_message_with_media_metadata() -> None:
    message = TelegramMessage(
        update_id=11,
        message_id=21,
        chat_id=123456,
        thread_id=42,
        from_user_id=999,
        text=None,
        caption="caption text",
        date=1700000000,
        is_topic_message=True,
        is_edited=True,
        reply_to_message_id=20,
        photos=(
            TelegramPhotoSize(
                file_id="ph-1",
                file_unique_id="uph-1",
                width=640,
                height=480,
                file_size=1024,
            ),
        ),
        document=TelegramDocument(
            file_id="doc-1",
            file_unique_id="udoc-1",
            file_name="report.txt",
            mime_type="text/plain",
            file_size=4096,
        ),
        audio=TelegramAudio(
            file_id="aud-1",
            file_unique_id="uaud-1",
            duration=3,
            file_name="clip.mp3",
            mime_type="audio/mpeg",
            file_size=2048,
        ),
        voice=TelegramVoice(
            file_id="voc-1",
            file_unique_id="uvoc-1",
            duration=2,
            mime_type="audio/ogg",
            file_size=1024,
        ),
    )
    dummy_poller = _DummyPoller(
        updates=[TelegramUpdate(update_id=11, message=message, callback=None)]
    )
    adapter = TelegramChatAdapter(bot=object(), poller=dummy_poller)  # type: ignore[arg-type]

    events = await adapter.poll_events(timeout_seconds=9.1)

    assert dummy_poller.last_timeout == 9
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ChatMessageEvent)
    assert event.update_id == "11"
    assert event.thread.platform == "telegram"
    assert event.thread.chat_id == "123456"
    assert event.thread.thread_id == "42"
    assert event.message.message_id == "21"
    assert event.from_user_id == "999"
    assert event.text == "caption text"
    assert event.is_edited is True
    assert event.reply_to is not None
    assert event.reply_to.message_id == "20"
    assert [item.kind for item in event.attachments] == [
        "photo",
        "document",
        "audio",
        "voice",
    ]
    assert event.attachments[0].file_id == "ph-1"
    assert event.attachments[1].file_name == "report.txt"
    assert event.attachments[2].mime_type == "audio/mpeg"
    assert event.attachments[3].size_bytes == 1024


@pytest.mark.anyio
async def test_poll_events_maps_callback_query_to_interaction_event() -> None:
    callback = TelegramCallbackQuery(
        update_id=12,
        callback_id="cb-1",
        from_user_id=111,
        data="resume:thread-1",
        message_id=22,
        chat_id=123,
        thread_id=77,
    )
    adapter = TelegramChatAdapter(
        bot=object(),  # type: ignore[arg-type]
        poller=_DummyPoller(
            updates=[TelegramUpdate(update_id=12, message=None, callback=callback)]
        ),
    )

    events = await adapter.poll_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ChatInteractionEvent)
    assert event.update_id == "12"
    assert event.interaction.interaction_id == "cb-1"
    assert event.thread.chat_id == "123"
    assert event.thread.thread_id == "77"
    logical = decode_logical_callback(event.payload)
    assert logical is not None
    assert logical.callback_id == "resume"
    assert logical.payload == {"thread_id": "thread-1"}
    assert event.message is not None
    assert event.message.message_id == "22"


@pytest.mark.anyio
async def test_poll_events_skips_callback_without_chat_context() -> None:
    callback = TelegramCallbackQuery(
        update_id=13,
        callback_id="cb-2",
        from_user_id=222,
        data="noop",
        message_id=None,
        chat_id=None,
        thread_id=None,
    )
    adapter = TelegramChatAdapter(
        bot=object(),  # type: ignore[arg-type]
        poller=_DummyPoller(
            updates=[TelegramUpdate(update_id=13, message=None, callback=callback)]
        ),
    )

    events = await adapter.poll_events()

    assert events == ()
