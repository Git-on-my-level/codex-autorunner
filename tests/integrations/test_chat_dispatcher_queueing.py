from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from codex_autorunner.integrations.chat.dispatcher import ChatDispatcher
from codex_autorunner.integrations.chat.models import (
    ChatInteractionEvent,
    ChatInteractionRef,
    ChatMessageEvent,
    ChatMessageRef,
    ChatThreadRef,
)


def _message_event(
    update_id: str,
    *,
    chat_id: str = "chat-1",
    thread_id: Optional[str] = "thread-1",
    message_id: str = "msg-1",
    text: str = "hello",
) -> ChatMessageEvent:
    thread = ChatThreadRef(platform="fake", chat_id=chat_id, thread_id=thread_id)
    return ChatMessageEvent(
        update_id=update_id,
        thread=thread,
        message=ChatMessageRef(thread=thread, message_id=message_id),
        from_user_id="user-1",
        text=text,
    )


def _interaction_event(
    update_id: str,
    *,
    chat_id: str = "chat-1",
    thread_id: Optional[str] = "thread-1",
    payload: str = "qdone:req-1",
) -> ChatInteractionEvent:
    thread = ChatThreadRef(platform="fake", chat_id=chat_id, thread_id=thread_id)
    return ChatInteractionEvent(
        update_id=update_id,
        thread=thread,
        interaction=ChatInteractionRef(thread=thread, interaction_id=f"cb-{update_id}"),
        from_user_id="user-1",
        payload=payload,
    )


@pytest.mark.anyio
async def test_dispatcher_orders_normal_events_per_conversation() -> None:
    dispatcher = ChatDispatcher()
    observed: list[str] = []

    async def handler(event, _context) -> None:
        await asyncio.sleep(0.01)
        observed.append(event.update_id)

    await asyncio.gather(
        dispatcher.dispatch(_message_event("u1", message_id="m1"), handler),
        dispatcher.dispatch(_message_event("u2", message_id="m2"), handler),
        dispatcher.dispatch(_message_event("u3", message_id="m3"), handler),
    )
    await dispatcher.wait_idle()

    assert observed == ["u1", "u2", "u3"]


@pytest.mark.anyio
async def test_dispatcher_bypass_events_run_immediately() -> None:
    dispatcher = ChatDispatcher()
    release_first = asyncio.Event()
    entered_first = asyncio.Event()
    observed: list[str] = []

    async def handler(event, _context) -> None:
        if event.update_id == "normal-1":
            observed.append("normal-1-start")
            entered_first.set()
            await release_first.wait()
            observed.append("normal-1-end")
            return
        observed.append(event.update_id)

    await dispatcher.dispatch(
        _message_event("normal-1", message_id="m1", text="normal"), handler
    )
    await entered_first.wait()
    await dispatcher.dispatch(
        _interaction_event("bypass", payload="qopt:0:0:req-1"), handler
    )
    release_first.set()
    await dispatcher.wait_idle()

    assert observed[:3] == ["normal-1-start", "bypass", "normal-1-end"]


@pytest.mark.anyio
async def test_dispatcher_dedupe_hook_short_circuits_processing() -> None:
    seen: list[str] = []

    def dedupe_predicate(event, _context) -> bool:
        return event.update_id != "dup"

    dispatcher = ChatDispatcher(dedupe_predicate=dedupe_predicate)

    async def handler(event, _context) -> None:
        seen.append(event.update_id)

    duplicate_result = await dispatcher.dispatch(_message_event("dup"), handler)
    accepted_result = await dispatcher.dispatch(_message_event("ok"), handler)
    await dispatcher.wait_idle()

    assert duplicate_result.status == "duplicate"
    assert accepted_result.status == "queued"
    assert seen == ["ok"]
