from __future__ import annotations

import asyncio

from codex_autorunner.integrations.chat.models import ChatMessageEvent
from codex_autorunner.integrations.discord.adapter import DiscordChatAdapter


class _UnusedRestClient:
    pass


def _message_payload(*, message_id: str, content: str) -> dict[str, object]:
    return {
        "id": message_id,
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "content": content,
        "author": {"id": "user-1", "bot": False},
        "attachments": [],
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
