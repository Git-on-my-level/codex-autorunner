from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.chat.handlers.approvals import ChatApprovalHandlers
from codex_autorunner.integrations.chat.handlers.models import ChatContext
from codex_autorunner.integrations.chat.models import (
    ChatInteractionEvent,
    ChatInteractionRef,
    ChatMessageRef,
    ChatThreadRef,
)


class _StoreStub:
    def __init__(self) -> None:
        self.cleared_request_ids: list[str] = []

    async def clear_pending_approval(self, request_id: str) -> None:
        self.cleared_request_ids.append(request_id)


class _RuntimeStub:
    def __init__(self) -> None:
        self.pending_request_id = "req-1"


class _RouterStub:
    def __init__(self, runtime: _RuntimeStub) -> None:
        self._runtime = runtime

    def runtime_for(self, _topic_key: str) -> _RuntimeStub:
        return self._runtime


class _ApprovalHandlersStub(ChatApprovalHandlers):
    def __init__(
        self, *, delete_raises: bool = False, delete_returns: bool = True
    ) -> None:
        self._logger = logging.getLogger("test.chat.approvals")
        self._store = _StoreStub()
        self.runtime = _RuntimeStub()
        self.__dict__["_router"] = _RouterStub(self.runtime)
        self._pending_approvals: dict[str, SimpleNamespace] = {}
        self._context = SimpleNamespace(topic_key="topic-1")
        self.answers: list[str] = []
        self.deleted_messages: list[tuple[str, str | None, str]] = []
        self.edited_messages: list[tuple[str, str | None, str, str, bool]] = []
        self.delete_raises = delete_raises
        self.delete_returns = delete_returns

    @property
    def _router(self) -> _RouterStub:
        return self.__dict__["_router"]

    def _resolve_turn_context(
        self, turn_id: str, thread_id: str | None = None
    ) -> SimpleNamespace | None:
        assert turn_id == "turn-1"
        assert thread_id == "codex-thread-1"
        return self._context

    async def _chat_answer_interaction(
        self, interaction: ChatInteractionEvent, text: str
    ) -> None:
        assert interaction.interaction.interaction_id == "interaction-1"
        self.answers.append(text)

    async def _chat_edit_message(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
        text: str,
        reply_markup: object = None,
        clear_actions: bool = False,
    ) -> None:
        _ = reply_markup
        self.edited_messages.append(
            (chat_id, thread_id, message_id, text, clear_actions)
        )

    async def _chat_delete_message(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
    ) -> bool:
        if self.delete_raises:
            raise RuntimeError("delete failed")
        if self.delete_returns:
            self.deleted_messages.append((chat_id, thread_id, message_id))
        return self.delete_returns

    def _format_approval_decision(self, decision: str) -> str:
        return f"Approval {decision}."


def _build_interaction() -> ChatInteractionEvent:
    thread = ChatThreadRef(platform="telegram", chat_id="chat-1", thread_id="thread-1")
    return ChatInteractionEvent(
        update_id="update-1",
        thread=thread,
        interaction=ChatInteractionRef(
            thread=thread,
            interaction_id="interaction-1",
        ),
        from_user_id="user-1",
        payload="approval payload",
        message=ChatMessageRef(thread=thread, message_id="message-1"),
    )


@pytest.mark.anyio
async def test_handle_approval_interaction_deletes_prompt_message() -> None:
    handlers = _ApprovalHandlersStub()
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    handlers._pending_approvals["req-1"] = SimpleNamespace(
        future=future,
        turn_id="turn-1",
        codex_thread_id="codex-thread-1",
        topic_key="topic-1",
        chat_id="chat-1",
        thread_id="thread-1",
        message_id="message-1",
    )
    context = ChatContext(
        thread=ChatThreadRef(
            platform="telegram", chat_id="chat-1", thread_id="thread-1"
        ),
        topic_key="topic-1",
        user_id="user-1",
    )

    await handlers.handle_approval_interaction(
        context,
        _build_interaction(),
        SimpleNamespace(request_id="req-1", decision="accept"),
    )

    assert handlers._store.cleared_request_ids == ["req-1"]
    assert future.done() and future.result() == "accept"
    assert handlers.answers == ["Decision: accept"]
    assert handlers.deleted_messages == [("chat-1", "thread-1", "message-1")]
    assert handlers.edited_messages == []
    assert handlers.runtime.pending_request_id is None


@pytest.mark.anyio
async def test_handle_approval_interaction_falls_back_to_edit_when_delete_fails() -> (
    None
):
    handlers = _ApprovalHandlersStub(delete_returns=False)
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    handlers._pending_approvals["req-1"] = SimpleNamespace(
        future=future,
        turn_id="turn-1",
        codex_thread_id="codex-thread-1",
        topic_key="topic-1",
        chat_id="chat-1",
        thread_id="thread-1",
        message_id="message-1",
    )
    context = ChatContext(
        thread=ChatThreadRef(
            platform="telegram", chat_id="chat-1", thread_id="thread-1"
        ),
        topic_key="topic-1",
        user_id="user-1",
    )

    await handlers.handle_approval_interaction(
        context,
        _build_interaction(),
        SimpleNamespace(request_id="req-1", decision="decline"),
    )

    assert handlers.deleted_messages == []
    assert handlers.edited_messages == [
        ("chat-1", "thread-1", "message-1", "Approval decline.", True)
    ]


@pytest.mark.anyio
async def test_handle_approval_interaction_falls_back_to_edit_when_delete_raises() -> (
    None
):
    handlers = _ApprovalHandlersStub(delete_raises=True)
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    handlers._pending_approvals["req-1"] = SimpleNamespace(
        future=future,
        turn_id="turn-1",
        codex_thread_id="codex-thread-1",
        topic_key="topic-1",
        chat_id="chat-1",
        thread_id="thread-1",
        message_id="message-1",
    )
    context = ChatContext(
        thread=ChatThreadRef(
            platform="telegram", chat_id="chat-1", thread_id="thread-1"
        ),
        topic_key="topic-1",
        user_id="user-1",
    )

    await handlers.handle_approval_interaction(
        context,
        _build_interaction(),
        SimpleNamespace(request_id="req-1", decision="decline"),
    )

    assert handlers.deleted_messages == []
    assert handlers.edited_messages == [
        ("chat-1", "thread-1", "message-1", "Approval decline.", True)
    ]
