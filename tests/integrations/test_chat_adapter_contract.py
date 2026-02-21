from __future__ import annotations

import pytest

from codex_autorunner.integrations.chat.adapter import SendTextRequest
from codex_autorunner.integrations.chat.capabilities import ChatCapabilities
from codex_autorunner.integrations.chat.models import (
    ChatAttachment,
    ChatInteractionEvent,
    ChatMessageEvent,
    ChatThreadRef,
)
from codex_autorunner.integrations.chat.testing import (
    FakeChatAdapter,
    deserialize_chat_event,
    serialize_chat_event,
)


@pytest.mark.asyncio
async def test_message_event_round_trip_without_required_field_mutation() -> None:
    adapter = FakeChatAdapter()
    thread = ChatThreadRef(platform="fake", chat_id="c-1", thread_id="t-1")
    event = adapter.emit_message_event(
        update_id="u-1",
        thread=thread,
        message_id="m-1",
        from_user_id="user-1",
        text="hello",
        is_edited=True,
        attachments=(
            ChatAttachment(
                kind="photo",
                file_id="f-1",
                file_name="one.png",
                mime_type="image/png",
                size_bytes=321,
            ),
        ),
    )

    polled = await adapter.poll_events()
    assert len(polled) == 1
    assert isinstance(polled[0], ChatMessageEvent)
    assert polled[0] == event

    payload = serialize_chat_event(polled[0])
    restored = deserialize_chat_event(payload)
    assert isinstance(restored, ChatMessageEvent)
    assert restored == polled[0]
    assert restored.update_id == "u-1"
    assert restored.thread.chat_id == "c-1"
    assert restored.message.message_id == "m-1"
    assert restored.attachments[0].file_id == "f-1"


@pytest.mark.asyncio
async def test_interaction_event_round_trip_without_required_field_mutation() -> None:
    adapter = FakeChatAdapter()
    thread = ChatThreadRef(platform="fake", chat_id="c-2", thread_id="t-2")
    event = adapter.emit_interaction_event(
        update_id="u-2",
        thread=thread,
        interaction_id="i-2",
        from_user_id="user-2",
        payload="approve:req-1",
        message_id="m-2",
    )

    polled = await adapter.poll_events()
    assert len(polled) == 1
    assert isinstance(polled[0], ChatInteractionEvent)
    assert polled[0] == event

    payload = serialize_chat_event(polled[0])
    restored = deserialize_chat_event(payload)
    assert isinstance(restored, ChatInteractionEvent)
    assert restored == polled[0]
    assert restored.update_id == "u-2"
    assert restored.thread.thread_id == "t-2"
    assert restored.interaction.interaction_id == "i-2"
    assert restored.message is not None
    assert restored.message.message_id == "m-2"


@pytest.mark.asyncio
async def test_capability_max_text_length_enforced_by_fake_adapter() -> None:
    adapter = FakeChatAdapter(capabilities=ChatCapabilities(max_text_length=5))
    thread = ChatThreadRef(platform="fake", chat_id="c-3")

    with pytest.raises(ValueError, match="max_text_length"):
        await adapter.send_text(SendTextRequest(thread=thread, text="toolong"))

    sent = await adapter.send_text(SendTextRequest(thread=thread, text="short"))
    assert sent.message_id == "1"


def test_capability_max_callback_payload_bytes_enforced_by_fake_adapter() -> None:
    adapter = FakeChatAdapter(
        capabilities=ChatCapabilities(max_callback_payload_bytes=6)
    )
    thread = ChatThreadRef(platform="fake", chat_id="c-4")

    with pytest.raises(ValueError, match="max_callback_payload_bytes"):
        adapter.emit_interaction_event(
            update_id="u-4",
            thread=thread,
            interaction_id="i-4",
            from_user_id="user-4",
            payload="1234567",
        )

    event = adapter.emit_interaction_event(
        update_id="u-5",
        thread=thread,
        interaction_id="i-5",
        from_user_id="user-5",
        payload="123456",
    )
    assert event.payload == "123456"
