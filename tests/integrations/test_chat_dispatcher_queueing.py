from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from codex_autorunner.integrations.chat.callbacks import (
    LogicalCallback,
    encode_logical_callback,
)
from codex_autorunner.integrations.chat.dispatcher import (
    ChatDispatcher,
    is_bypass_event,
)
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
    assert accepted_result.queued_pending == 1
    assert accepted_result.queued_while_busy is False
    assert seen == ["ok"]


@pytest.mark.anyio
async def test_dispatcher_treats_logical_callback_ids_case_insensitively() -> None:
    payload = encode_logical_callback(
        LogicalCallback(callback_id="QUESTION_DONE", payload={"request_id": "req-1"})
    )
    event = _interaction_event("u-1", payload=payload)

    dispatcher = ChatDispatcher()

    async def _noop_handler(_event, _context) -> None:
        return

    result = await dispatcher.dispatch(event, _noop_handler)
    assert result.bypassed is True
    assert result.status == "dispatched"


@pytest.mark.anyio
async def test_dispatcher_supports_custom_bypass_rules() -> None:
    dispatcher = ChatDispatcher(
        bypass_interaction_prefixes=(),
        bypass_callback_ids=(),
        bypass_message_texts=("!stop",),
    )
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
    queued = await dispatcher.dispatch(
        _interaction_event("queued", payload="qopt:0:0:req-1"), handler
    )
    bypassed = await dispatcher.dispatch(
        _message_event("bypass", message_id="m2", text="!stop"), handler
    )
    release_first.set()
    await dispatcher.wait_idle()

    assert queued.status == "queued"
    assert queued.bypassed is False
    assert queued.queued_while_busy is True
    assert queued.queued_pending == 1
    assert bypassed.status == "dispatched"
    assert bypassed.bypassed is True
    assert observed[:4] == ["normal-1-start", "bypass", "normal-1-end", "queued"]


@pytest.mark.parametrize(
    ("kwargs", "expected_param"),
    [
        ({"bypass_interaction_prefixes": "qopt:"}, "bypass_interaction_prefixes"),
        ({"bypass_callback_ids": "question_done"}, "bypass_callback_ids"),
        ({"bypass_message_texts": "!stop"}, "bypass_message_texts"),
    ],
)
def test_dispatcher_rejects_scalar_string_bypass_iterables(
    kwargs: dict[str, str], expected_param: str
) -> None:
    with pytest.raises(TypeError, match=expected_param):
        ChatDispatcher(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "expected_param"),
    [
        ({"interaction_prefixes": "qopt:"}, "interaction_prefixes"),
        ({"callback_ids": "question_done"}, "callback_ids"),
        ({"message_texts": "!stop"}, "message_texts"),
    ],
)
def test_is_bypass_event_rejects_scalar_string_iterables(
    kwargs: dict[str, str], expected_param: str
) -> None:
    with pytest.raises(TypeError, match=expected_param):
        is_bypass_event(_interaction_event("u-1"), **kwargs)
