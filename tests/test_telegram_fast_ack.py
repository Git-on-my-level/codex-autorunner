import asyncio
import logging
from types import SimpleNamespace
from typing import Optional

import pytest

from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.constants import (
    PLACEHOLDER_TEXT,
    QUEUED_PLACEHOLDER_TEXT,
)
from codex_autorunner.integrations.telegram.dispatch import _dispatch_message
from codex_autorunner.integrations.telegram.state import (
    TelegramTopicRecord,
    TopicRouter,
)


class _BotStub:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[dict] = None,
    ) -> dict:
        message = {
            "message_id": 1000 + len(self.sent_messages),
            "text": text,
        }
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": message_thread_id,
                "reply_to": reply_to_message_id,
            }
        )
        return message


class _ServiceStub:
    def __init__(self, router: TopicRouter) -> None:
        self._logger = logging.getLogger("test")
        self._router = router
        self._bot = _BotStub()
        self._allowlist = None
        self._queued_placeholder_map: dict[tuple[int, int], int] = {}
        self._coalesced_buffers: dict = {}
        self._coalesce_locks: dict = {}
        self._media_batch_buffers: dict = {}
        self._media_batch_locks: dict = {}
        self._resume_options: dict = {}
        self._bind_options: dict = {}
        self._agent_options: dict = {}
        self._model_options: dict = {}
        self._model_pending: dict = {}
        self._review_commit_options: dict = {}
        self._review_commit_subjects: dict = {}
        self._pending_review_custom: dict = {}
        self._pending_approvals: dict = {}
        self._turn_contexts: dict = {}

    def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    def _should_bypass_topic_queue(self, _message: TelegramMessage) -> bool:
        return False

    def _spawn_task(self, coro) -> asyncio.Task:
        return asyncio.create_task(coro)

    def _get_queued_placeholder(self, chat_id: int, message_id: int) -> Optional[int]:
        return self._queued_placeholder_map.get((chat_id, message_id))

    def _set_queued_placeholder(
        self, chat_id: int, message_id: int, placeholder_id: int
    ) -> None:
        self._queued_placeholder_map[(chat_id, message_id)] = placeholder_id

    def _clear_queued_placeholder(self, chat_id: int, message_id: int) -> None:
        self._queued_placeholder_map.pop((chat_id, message_id), None)

    async def _send_placeholder(
        self,
        chat_id: int,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        text: str = PLACEHOLDER_TEXT,
        reply_markup: Optional[dict] = None,
    ) -> int:
        response = await self._bot.send_message(
            chat_id,
            text,
            message_thread_id=thread_id,
            reply_to_message_id=reply_to,
        )
        return response["message_id"]

    async def _handle_message(self, _message: TelegramMessage) -> None:
        pass

    def _enqueue_topic_work(self, key: str, work, *, force_queue: bool = False) -> None:
        runtime = self._router.runtime_for(key)
        wrapped = self._wrap_topic_work(key, work)
        if force_queue:
            self._spawn_task(runtime.queue.enqueue(wrapped))
        else:
            self._spawn_task(wrapped())

    def _wrap_topic_work(self, key: str, work):
        async def wrapped():
            return await work()

        return wrapped


def _message(*, message_id: int, thread_id: int) -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=message_id,
        chat_id=10,
        thread_id=thread_id,
        from_user_id=2,
        text="hello",
        date=None,
        is_topic_message=True,
    )


def _record(thread_id: str) -> TelegramTopicRecord:
    return TelegramTopicRecord(
        workspace_path="/tmp",
        active_thread_id=thread_id,
        thread_ids=[thread_id],
    )


@pytest.mark.anyio
async def test_fast_ack_sent_when_topic_has_current_turn() -> None:
    topics = {}
    store = SimpleNamespace(_topics=topics)

    router = TopicRouter(store)
    handler = _ServiceStub(router)

    runtime = router.runtime_for("10:11")
    runtime.current_turn_id = "active-turn"

    message = _message(message_id=1, thread_id=11)
    update = SimpleNamespace(update_id=1, message=message, callback=None)
    context = SimpleNamespace(
        chat_id=10,
        user_id=2,
        thread_id=11,
        message_id=1,
        is_topic=True,
        is_edited=False,
        topic_key="10:11",
    )

    await _dispatch_message(handler, update, context)

    assert len(handler._bot.sent_messages) == 1
    sent = handler._bot.sent_messages[0]
    assert sent["text"] == QUEUED_PLACEHOLDER_TEXT
    assert sent["chat_id"] == 10
    assert sent["reply_to"] == 1

    assert handler._queued_placeholder_map.get((10, 1)) == 1000


@pytest.mark.anyio
async def test_fast_ack_sent_when_topic_queue_has_depth() -> None:
    topics = {}
    store = SimpleNamespace(_topics=topics)

    router = TopicRouter(store)
    handler = _ServiceStub(router)

    runtime = router.runtime_for("10:11")
    await runtime.queue._queue.put(("work", None))

    message = _message(message_id=1, thread_id=11)
    update = SimpleNamespace(update_id=1, message=message, callback=None)
    context = SimpleNamespace(
        chat_id=10,
        user_id=2,
        thread_id=11,
        message_id=1,
        is_topic=True,
        is_edited=False,
        topic_key="10:11",
    )

    await _dispatch_message(handler, update, context)

    assert len(handler._bot.sent_messages) == 1
    sent = handler._bot.sent_messages[0]
    assert sent["text"] == QUEUED_PLACEHOLDER_TEXT
    assert sent["chat_id"] == 10
    assert sent["reply_to"] == 1

    assert handler._queued_placeholder_map.get((10, 1)) == 1000


@pytest.mark.anyio
async def test_fast_ack_not_sent_when_topic_is_idle() -> None:
    topics = {}
    store = SimpleNamespace(_topics=topics)

    router = TopicRouter(store)
    handler = _ServiceStub(router)

    message = _message(message_id=1, thread_id=11)
    update = SimpleNamespace(update_id=1, message=message, callback=None)
    context = SimpleNamespace(
        chat_id=10,
        user_id=2,
        thread_id=11,
        message_id=1,
        is_topic=True,
        is_edited=False,
        topic_key="10:11",
    )

    await _dispatch_message(handler, update, context)

    assert len(handler._bot.sent_messages) == 0
    assert (10, 1) not in handler._queued_placeholder_map
