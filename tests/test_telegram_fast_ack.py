import asyncio
import logging
from types import SimpleNamespace
from typing import Optional

import pytest

from codex_autorunner.integrations.chat.immediate_feedback import (
    QUEUED_NOTICE_TEXT,
    ImmediateAckResult,
    QueuedNoticeResult,
    WorkingAnchorResult,
    create_or_reuse_working_anchor,
    immediate_ack,
    publish_queued_notice,
)
from codex_autorunner.integrations.chat.models import (
    ChatAction,
    ChatInteractionRef,
    ChatMessageRef,
    ChatThreadRef,
)
from codex_autorunner.integrations.telegram.adapter import (
    TelegramCallbackQuery,
    TelegramMessage,
    encode_cancel_callback,
)
from codex_autorunner.integrations.telegram.dispatch import (
    _dispatch_callback,
    _dispatch_message,
)
from codex_autorunner.integrations.telegram.state import (
    TelegramTopicRecord,
    TopicRouter,
)


class _BotStub:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.answered_callbacks: list[dict] = []

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
                "reply_markup": reply_markup,
            }
        )
        return message

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        chat_id: Optional[int] = None,
        thread_id: Optional[int] = None,
        message_id: Optional[int] = None,
        text: Optional[str] = None,
    ) -> None:
        self.answered_callbacks.append(
            {
                "callback_query_id": callback_query_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "message_id": message_id,
                "text": text,
            }
        )


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
        self.timeline: list[str] = []

    def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    def _should_bypass_topic_queue(self, _message: TelegramMessage) -> bool:
        return False

    def _spawn_task(self, coro) -> asyncio.Task:
        return asyncio.create_task(coro)

    async def _answer_callback(self, callback, text: str) -> None:
        self._bot.answered_callbacks.append(
            {
                "callback_query_id": getattr(callback, "callback_id", None),
                "text": text,
            }
        )

    def _get_queued_placeholder(self, chat_id: int, message_id: int) -> Optional[int]:
        return self._queued_placeholder_map.get((chat_id, message_id))

    def _set_queued_placeholder(
        self, chat_id: int, message_id: int, placeholder_id: int
    ) -> None:
        self._queued_placeholder_map[(chat_id, message_id)] = placeholder_id

    def _clear_queued_placeholder(self, chat_id: int, message_id: int) -> None:
        self._queued_placeholder_map.pop((chat_id, message_id), None)

    async def _begin_typing_indicator(
        self, _chat_id: int, _thread_id: Optional[int]
    ) -> None:
        self.timeline.append("typing_begin")

    async def _end_typing_indicator(
        self, _chat_id: int, _thread_id: Optional[int]
    ) -> None:
        self.timeline.append("typing_end")

    async def _handle_message(self, _message: TelegramMessage) -> None:
        pass

    async def _handle_callback(self, _callback: TelegramCallbackQuery) -> None:
        pass

    def _enqueue_topic_work(
        self,
        key: str,
        work,
        *,
        force_queue: bool = False,
        item_id: Optional[str] = None,
    ) -> Optional[str]:
        runtime = self._router.runtime_for(key)
        wrapped = self._wrap_topic_work(key, work)
        if force_queue:
            return runtime.queue.enqueue_detached(wrapped, item_id=item_id)
        self._spawn_task(wrapped())
        return None

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
    assert sent["text"] == QUEUED_NOTICE_TEXT
    assert sent["chat_id"] == 10
    assert sent["reply_to"] == 1
    assert sent["reply_markup"] is not None

    assert handler._queued_placeholder_map.get((10, 1)) is not None


@pytest.mark.anyio
async def test_fast_ack_sent_when_topic_queue_has_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topics = {}
    store = SimpleNamespace(_topics=topics)

    router = TopicRouter(store)
    handler = _ServiceStub(router)

    runtime = router.runtime_for("10:11")
    monkeypatch.setattr(runtime.queue, "pending", lambda: 1)

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
    assert sent["text"] == QUEUED_NOTICE_TEXT
    assert sent["chat_id"] == 10
    assert sent["reply_to"] == 1
    assert sent["reply_markup"] is not None

    assert handler._queued_placeholder_map.get((10, 1)) is not None


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


@pytest.mark.anyio
async def test_dispatch_message_wraps_handler_with_typing_indicator() -> None:
    topics = {}
    store = SimpleNamespace(_topics=topics)
    router = TopicRouter(store)
    handler = _ServiceStub(router)

    async def _recording_handle(_message: TelegramMessage) -> None:
        handler.timeline.append("handle_start")
        await asyncio.sleep(0)
        handler.timeline.append("handle_end")

    handler._handle_message = _recording_handle  # type: ignore[assignment]
    message = TelegramMessage(
        update_id=1,
        message_id=1,
        chat_id=10,
        thread_id=None,
        from_user_id=2,
        text="hello",
        date=None,
        is_topic_message=False,
    )
    update = SimpleNamespace(update_id=1, message=message, callback=None)
    context = SimpleNamespace(
        chat_id=10,
        user_id=2,
        thread_id=None,
        message_id=1,
        is_topic=False,
        is_edited=False,
        topic_key=None,
    )

    await _dispatch_message(handler, update, context)

    assert handler.timeline == [
        "typing_begin",
        "handle_start",
        "handle_end",
        "typing_end",
    ]


@pytest.mark.anyio
async def test_callback_answer_before_queue_admission() -> None:
    topics = {}
    store = SimpleNamespace(_topics=topics)
    router = TopicRouter(store)
    handler = _ServiceStub(router)

    runtime = router.runtime_for("10:11")
    runtime.current_turn_id = "active-turn"

    events: list[str] = []

    async def _recording_handle_callback(
        _callback: TelegramCallbackQuery,
    ) -> None:
        events.append("callback_handled")

    handler._handle_callback = _recording_handle_callback  # type: ignore[assignment]

    callback = TelegramCallbackQuery(
        update_id=1,
        callback_id="cb-123",
        chat_id=10,
        from_user_id=2,
        thread_id=11,
        message_id=50,
        data="resume:abc",
    )
    update = SimpleNamespace(update_id=1, message=None, callback=callback)
    context = SimpleNamespace(
        chat_id=10,
        user_id=2,
        thread_id=11,
        message_id=50,
        is_topic=True,
        is_edited=False,
        topic_key="10:11",
    )

    await _dispatch_callback(handler, update, context)

    assert len(handler._bot.answered_callbacks) == 1
    ack = handler._bot.answered_callbacks[0]
    assert ack["callback_query_id"] == "cb-123"


@pytest.mark.anyio
async def test_callback_answer_happens_for_bypass_callbacks() -> None:
    topics = {}
    store = SimpleNamespace(_topics=topics)
    router = TopicRouter(store)
    handler = _ServiceStub(router)

    events: list[str] = []

    async def _recording_handle_callback(
        _callback: TelegramCallbackQuery,
    ) -> None:
        events.append("callback_handled")

    handler._handle_callback = _recording_handle_callback  # type: ignore[assignment]

    callback = TelegramCallbackQuery(
        update_id=1,
        callback_id="cb-approve",
        chat_id=10,
        from_user_id=2,
        thread_id=11,
        message_id=50,
        data="appr:accept:123",
    )
    update = SimpleNamespace(update_id=1, message=None, callback=callback)
    context = SimpleNamespace(
        chat_id=10,
        user_id=2,
        thread_id=11,
        message_id=50,
        is_topic=True,
        is_edited=False,
        topic_key="10:11",
    )

    await _dispatch_callback(handler, update, context)

    assert len(handler._bot.answered_callbacks) == 1
    assert events == ["callback_handled"]


@pytest.mark.anyio
async def test_callback_answer_before_queue_non_bypass() -> None:
    topics = {}
    store = SimpleNamespace(_topics=topics)
    router = TopicRouter(store)
    handler = _ServiceStub(router)

    runtime = router.runtime_for("10:11")
    runtime.current_turn_id = "active-turn"

    queue_order: list[str] = []

    async def _recording_handle_callback(
        _callback: TelegramCallbackQuery,
    ) -> None:
        queue_order.append("handled")

    handler._handle_callback = _recording_handle_callback  # type: ignore[assignment]

    original_enqueue = handler._enqueue_topic_work

    def _tracking_enqueue(key, work, *, force_queue=False, item_id=None):
        queue_order.append("enqueued")
        return original_enqueue(key, work, force_queue=force_queue, item_id=item_id)

    handler._enqueue_topic_work = _tracking_enqueue

    callback = TelegramCallbackQuery(
        update_id=1,
        callback_id="cb-resume",
        chat_id=10,
        from_user_id=2,
        thread_id=11,
        message_id=50,
        data="resume:abc",
    )
    update = SimpleNamespace(update_id=1, message=None, callback=callback)
    context = SimpleNamespace(
        chat_id=10,
        user_id=2,
        thread_id=11,
        message_id=50,
        is_topic=True,
        is_edited=False,
        topic_key="10:11",
    )

    await _dispatch_callback(handler, update, context)

    ack_idx = queue_order.index("enqueued") if "enqueued" in queue_order else -1
    assert len(handler._bot.answered_callbacks) >= 1
    assert ack_idx >= 0


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, ChatThreadRef, Optional[ChatMessageRef]]] = []
        self.acked: list[Optional[ChatInteractionRef]] = []
        self._next_id = 100

    async def send_text(
        self,
        thread: ChatThreadRef,
        text: str,
        *,
        reply_to: Optional[ChatMessageRef] = None,
        parse_mode: Optional[str] = None,
    ) -> ChatMessageRef:
        self.sent.append((text, thread, reply_to))
        msg_id = str(self._next_id)
        self._next_id += 1
        return ChatMessageRef(thread=thread, message_id=msg_id)

    async def present_actions(
        self,
        thread: ChatThreadRef,
        text: str,
        *,
        actions: tuple[ChatAction, ...] = (),
        reply_to: Optional[ChatMessageRef] = None,
        parse_mode: Optional[str] = None,
    ) -> ChatMessageRef:
        self.sent.append((text, thread, reply_to))
        msg_id = str(self._next_id)
        self._next_id += 1
        return ChatMessageRef(thread=thread, message_id=msg_id)

    async def ack_interaction(
        self,
        interaction: Optional[ChatInteractionRef],
        *,
        text: Optional[str] = None,
    ) -> None:
        self.acked.append(interaction)


@pytest.mark.anyio
async def test_immediate_ack_success() -> None:
    transport = _FakeTransport()
    interaction = ChatInteractionRef(
        thread=ChatThreadRef(platform="test", chat_id="10"),
        interaction_id="cb-1",
    )
    result = await immediate_ack(
        transport,
        interaction,
        ack_class="callback_answer",
    )
    assert isinstance(result, ImmediateAckResult)
    assert result.acknowledged is True
    assert result.ack_class == "callback_answer"
    assert len(transport.acked) == 1


@pytest.mark.anyio
async def test_immediate_ack_no_interaction() -> None:
    transport = _FakeTransport()
    result = await immediate_ack(
        transport,
        None,
        ack_class="callback_answer",
    )
    assert result.acknowledged is False


@pytest.mark.anyio
async def test_immediate_ack_with_state_writer() -> None:
    transport = _FakeTransport()
    interaction = ChatInteractionRef(
        thread=ChatThreadRef(platform="test", chat_id="10"),
        interaction_id="cb-1",
    )
    state_calls: list[dict] = []

    async def state_writer(operation_id, *, state, **changes):
        state_calls.append({"id": operation_id, "state": state, **changes})

    result = await immediate_ack(
        transport,
        interaction,
        ack_class="callback_answer",
        operation_id="op-1",
        state_writer=state_writer,
    )
    assert result.acknowledged is True
    assert len(state_calls) == 1
    assert state_calls[0]["state"].value == "acknowledged"


@pytest.mark.anyio
async def test_publish_queued_notice() -> None:
    transport = _FakeTransport()
    thread = ChatThreadRef(platform="test", chat_id="10")
    reply_to = ChatMessageRef(thread=thread, message_id="5")

    result = await publish_queued_notice(
        transport,
        thread,
        reply_to=reply_to,
    )
    assert isinstance(result, QueuedNoticeResult)
    assert result.published is True
    assert result.anchor_ref is not None
    assert len(transport.sent) == 1
    sent_text, sent_thread, sent_reply = transport.sent[0]
    assert sent_text == QUEUED_NOTICE_TEXT
    assert sent_reply == reply_to


@pytest.mark.anyio
async def test_publish_queued_notice_includes_cancel_button() -> None:
    transport = _FakeTransport()
    thread = ChatThreadRef(platform="test", chat_id="10")

    result = await publish_queued_notice(transport, thread)
    assert result.published is True
    assert result.message_ref is not None


@pytest.mark.anyio
async def test_fast_ack_queued_notice_uses_source_specific_queue_actions() -> None:
    topics = {}
    store = SimpleNamespace(_topics=topics)
    router = TopicRouter(store)
    handler = _ServiceStub(router)

    runtime = router.runtime_for("10:11")
    runtime.current_turn_id = "active-turn"

    message = _message(message_id=17, thread_id=11)
    update = SimpleNamespace(update_id=1, message=message, callback=None)
    context = SimpleNamespace(
        chat_id=10,
        user_id=2,
        thread_id=11,
        message_id=17,
        is_topic=True,
        is_edited=False,
        topic_key="10:11",
    )

    await _dispatch_message(handler, update, context)

    assert len(handler._bot.sent_messages) == 1
    keyboard = handler._bot.sent_messages[0]["reply_markup"]
    assert keyboard is not None
    rows = keyboard["inline_keyboard"]
    assert rows[0][0]["callback_data"] == encode_cancel_callback("queue_cancel:17")
    assert rows[1][0]["callback_data"] == encode_cancel_callback(
        "queue_interrupt_send:17"
    )


@pytest.mark.anyio
async def test_create_working_anchor_new() -> None:
    transport = _FakeTransport()
    thread = ChatThreadRef(platform="test", chat_id="10")
    reply_to = ChatMessageRef(thread=thread, message_id="5")

    result = await create_or_reuse_working_anchor(
        transport,
        thread,
        reply_to=reply_to,
    )
    assert isinstance(result, WorkingAnchorResult)
    assert result.created is True
    assert result.reused is False
    assert result.anchor_ref is not None
    assert result.message_ref is not None


@pytest.mark.anyio
async def test_create_working_anchor_reuse_existing() -> None:
    transport = _FakeTransport()
    thread = ChatThreadRef(platform="test", chat_id="10")
    existing = ChatMessageRef(thread=thread, message_id="99")

    result = await create_or_reuse_working_anchor(
        transport,
        thread,
        anchor_reuse="prefer",
        existing_anchor=existing,
    )
    assert result.reused is True
    assert result.created is False
    assert result.anchor_ref == "99"
    assert len(transport.sent) == 0


@pytest.mark.anyio
async def test_create_working_anchor_require_without_existing() -> None:
    transport = _FakeTransport()
    thread = ChatThreadRef(platform="test", chat_id="10")

    result = await create_or_reuse_working_anchor(
        transport,
        thread,
        anchor_reuse="require",
    )
    assert result.created is False
    assert result.reused is False
    assert result.anchor_ref is None
    assert len(transport.sent) == 0


@pytest.mark.anyio
async def test_anchor_reuse_on_queued_to_running_transition() -> None:
    transport = _FakeTransport()
    thread = ChatThreadRef(platform="test", chat_id="10")
    reply_to = ChatMessageRef(thread=thread, message_id="5")

    queued_result = await publish_queued_notice(
        transport,
        thread,
        reply_to=reply_to,
    )
    assert queued_result.anchor_ref is not None
    queued_anchor_ref = queued_result.anchor_ref

    existing_anchor = ChatMessageRef(thread=thread, message_id=queued_anchor_ref)

    running_result = await create_or_reuse_working_anchor(
        transport,
        thread,
        anchor_reuse="prefer",
        existing_anchor=existing_anchor,
    )
    assert running_result.reused is True
    assert running_result.anchor_ref == queued_anchor_ref
    assert len(transport.sent) == 1


@pytest.mark.anyio
async def test_anchor_new_on_queued_to_running_without_reuse() -> None:
    transport = _FakeTransport()
    thread = ChatThreadRef(platform="test", chat_id="10")

    queued_result = await publish_queued_notice(transport, thread)
    assert queued_result.anchor_ref is not None

    running_result = await create_or_reuse_working_anchor(
        transport,
        thread,
        anchor_reuse="never",
    )
    assert running_result.created is True
    assert running_result.reused is False
    assert running_result.anchor_ref is not None
    assert len(transport.sent) == 2
