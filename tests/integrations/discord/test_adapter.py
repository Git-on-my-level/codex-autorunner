from __future__ import annotations

import asyncio

from codex_autorunner.integrations.chat.models import ChatMessageEvent
from codex_autorunner.integrations.chat.renderer import RenderedText
from codex_autorunner.integrations.discord import adapter as discord_adapter_module
from codex_autorunner.integrations.discord.adapter import (
    DiscordChatAdapter,
    DiscordTextRenderer,
)


class _UnusedRestClient:
    pass


class _FollowupRestClient:
    def __init__(self) -> None:
        self.calls = 0

    async def create_followup_message(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        _ = (application_id, interaction_token, payload)
        self.calls += 1
        return {"id": f"message-{self.calls}", "channel_id": "channel-1"}


def _message_payload(
    *,
    message_id: str,
    content: str,
    attachments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": message_id,
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "content": content,
        "author": {"id": "user-1", "bot": False},
        "attachments": attachments or [],
    }


def test_adapter_poll_events_when_constructed_outside_loop() -> None:
    adapter = DiscordChatAdapter(
        rest_client=_UnusedRestClient(),  # type: ignore[arg-type]
        application_id="app-1",
    )

    async def _poll_once() -> tuple[object, ...]:
        return tuple(await adapter.poll_events(timeout_seconds=0.01))

    assert asyncio.run(_poll_once()) == ()


def test_adapter_enqueues_before_loop_and_delivers_event() -> None:
    adapter = DiscordChatAdapter(
        rest_client=_UnusedRestClient(),  # type: ignore[arg-type]
        application_id="app-1",
    )
    enqueued = adapter.enqueue_message_event(
        _message_payload(message_id="m-1", content="hello from discord")
    )
    assert isinstance(enqueued, ChatMessageEvent)

    async def _poll_once() -> tuple[object, ...]:
        return tuple(await adapter.poll_events(timeout_seconds=0.01))

    events = asyncio.run(_poll_once())
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ChatMessageEvent)
    assert event.update_id == "discord:message:m-1"
    assert event.text == "hello from discord"


def test_adapter_enqueues_off_loop_after_queue_init_and_delivers_event() -> None:
    adapter = DiscordChatAdapter(
        rest_client=_UnusedRestClient(),  # type: ignore[arg-type]
        application_id="app-1",
    )

    async def _run() -> None:
        # Initialize the queue on the current loop.
        assert await adapter.poll_events(timeout_seconds=0.01) == ()

        # Enqueue from a worker thread (off-loop) and ensure it is still delivered.
        def _enqueue() -> None:
            adapter.enqueue_message_event(
                _message_payload(message_id="m-2", content="hello off-loop")
            )

        await asyncio.to_thread(_enqueue)
        events = await adapter.poll_events(timeout_seconds=0.05)
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, ChatMessageEvent)
        assert event.update_id == "discord:message:m-2"
        assert event.text == "hello off-loop"

    asyncio.run(_run())


def test_adapter_parses_attachment_source_url_metadata() -> None:
    adapter = DiscordChatAdapter(
        rest_client=_UnusedRestClient(),  # type: ignore[arg-type]
        application_id="app-1",
    )
    event = adapter.parse_message_event(
        _message_payload(
            message_id="m-3",
            content="",
            attachments=[
                {
                    "id": "att-1",
                    "filename": "photo.png",
                    "content_type": "image/png",
                    "size": 1234,
                    "url": "https://cdn.discordapp.com/attachments/photo.png",
                },
                {
                    "id": "att-2",
                    "filename": "alt.png",
                    "content_type": "image/png",
                    "size": 42,
                    "proxy_url": "https://media.discordapp.net/attachments/alt.png",
                },
            ],
        )
    )

    assert isinstance(event, ChatMessageEvent)
    assert len(event.attachments) == 2
    attachment = event.attachments[0]
    assert attachment.file_id == "att-1"
    assert attachment.file_name == "photo.png"
    assert attachment.kind == "image"
    assert attachment.source_url == "https://cdn.discordapp.com/attachments/photo.png"
    proxy_attachment = event.attachments[1]
    assert proxy_attachment.file_id == "att-2"
    assert proxy_attachment.source_url == (
        "https://media.discordapp.net/attachments/alt.png"
    )


def test_adapter_infers_audio_kind_without_content_type() -> None:
    adapter = DiscordChatAdapter(
        rest_client=_UnusedRestClient(),  # type: ignore[arg-type]
        application_id="app-1",
    )
    event = adapter.parse_message_event(
        _message_payload(
            message_id="m-4",
            content="",
            attachments=[
                {
                    "id": "att-audio-1",
                    "filename": "voice-message.ogg",
                    "size": 1024,
                    "url": "https://cdn.discordapp.com/attachments/voice-message.ogg",
                },
                {
                    "id": "att-audio-2",
                    "size": 2048,
                    "duration_secs": 4,
                    "proxy_url": "https://media.discordapp.net/attachments/audio.opus",
                },
            ],
        )
    )

    assert isinstance(event, ChatMessageEvent)
    assert len(event.attachments) == 2
    assert event.attachments[0].kind == "audio"
    assert event.attachments[1].kind == "audio"


def test_adapter_infers_audio_kind_with_generic_content_type_and_duration() -> None:
    adapter = DiscordChatAdapter(
        rest_client=_UnusedRestClient(),  # type: ignore[arg-type]
        application_id="app-1",
    )
    event = adapter.parse_message_event(
        _message_payload(
            message_id="m-5",
            content="",
            attachments=[
                {
                    "id": "att-audio-generic",
                    "filename": "voice-message",
                    "content_type": "application/octet-stream",
                    "duration_secs": 6,
                    "size": 1024,
                    "url": "https://cdn.discordapp.com/attachments/no-ext",
                }
            ],
        )
    )

    assert isinstance(event, ChatMessageEvent)
    assert len(event.attachments) == 1
    assert event.attachments[0].kind == "audio"


def test_discord_text_renderer_split_text_has_no_part_prefix() -> None:
    renderer = DiscordTextRenderer()
    rendered = RenderedText(text=("alpha " * 500), parse_mode=None)

    chunks = renderer.split_text(rendered, max_length=120)

    assert len(chunks) > 1
    assert all(not chunk.text.startswith("Part ") for chunk in chunks)


def test_adapter_prunes_interaction_token_cache(monkeypatch) -> None:
    monkeypatch.setattr(discord_adapter_module, "_MAX_INTERACTION_TOKEN_CACHE", 2)
    adapter = DiscordChatAdapter(
        rest_client=_UnusedRestClient(),  # type: ignore[arg-type]
        application_id="app-1",
    )

    for idx in range(3):
        payload = {
            "id": f"interaction-{idx}",
            "token": f"token-{idx}",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "member": {"user": {"id": "user-1"}},
            "data": {"name": "car", "options": []},
        }
        adapter.parse_interaction_event(payload)

    assert len(adapter._interaction_tokens) == 2
    assert "interaction-0" not in adapter._interaction_tokens
    assert adapter._interaction_tokens["interaction-1"] == "token-1"
    assert adapter._interaction_tokens["interaction-2"] == "token-2"


def test_adapter_prunes_message_interaction_token_cache(monkeypatch) -> None:
    monkeypatch.setattr(
        discord_adapter_module, "_MAX_MESSAGE_INTERACTION_TOKEN_CACHE", 2
    )
    rest = _FollowupRestClient()
    adapter = DiscordChatAdapter(
        rest_client=rest,  # type: ignore[arg-type]
        application_id="app-1",
    )
    adapter._interaction_tokens["interaction-1"] = "token-1"

    async def _send_followups() -> None:
        for idx in range(3):
            result = await adapter.send_interaction_followup(
                "interaction-1", f"message {idx}"
            )
            assert result is not None

    asyncio.run(_send_followups())

    assert len(adapter._message_interaction_tokens) == 2
    assert "message-1" not in adapter._message_interaction_tokens
    assert adapter._message_interaction_tokens["message-2"] == "token-1"
    assert adapter._message_interaction_tokens["message-3"] == "token-1"
