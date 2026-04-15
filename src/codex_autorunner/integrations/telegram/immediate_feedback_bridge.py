"""Telegram bridge for shared immediate-feedback primitives.

This module adapts the duck-typed Telegram service handlers object to the
shared feedback primitives' interfaces so that dispatch code can call
platform-agnostic feedback helpers without sacrificing Telegram's native
callback-answer and send_message semantics.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from ...core.orchestration.chat_operation_state import ChatOperationState
from ..chat.immediate_feedback import (
    QUEUED_NOTICE_TEXT,
    ChatOperationStateWriter,
    ImmediateAckResult,
    QueuedNoticeResult,
    immediate_ack,
    publish_queued_notice,
)
from ..chat.models import (
    ChatAction,
    ChatInteractionRef,
    ChatMessageRef,
    ChatThreadRef,
)
from .adapter import encode_cancel_callback


def _thread_ref(chat_id: Optional[int], thread_id: Optional[int]) -> ChatThreadRef:
    return ChatThreadRef(
        platform="telegram",
        chat_id=str(chat_id or 0),
        thread_id=str(thread_id) if thread_id is not None else None,
    )


def _message_ref(
    chat_id: Optional[int],
    message_id: Optional[int],
    thread_id: Optional[int] = None,
) -> Optional[ChatMessageRef]:
    if message_id is None:
        return None
    return ChatMessageRef(
        thread=_thread_ref(chat_id, thread_id),
        message_id=str(message_id),
    )


def _interaction_ref(
    callback_id: Optional[str],
    chat_id: Optional[int],
    thread_id: Optional[int] = None,
) -> Optional[ChatInteractionRef]:
    if not callback_id:
        return None
    return ChatInteractionRef(
        thread=_thread_ref(chat_id, thread_id),
        interaction_id=callback_id,
    )


class _TelegramFeedbackTransport:
    def __init__(self, handlers: Any) -> None:
        self._handlers = handlers

    async def ack_interaction(
        self,
        interaction: ChatInteractionRef,
        *,
        text: Optional[str] = None,
    ) -> None:
        answer_fn = getattr(self._handlers, "_answer_callback", None)
        if not callable(answer_fn):
            bot = getattr(self._handlers, "_bot", None)
            if bot is None:
                return
            answer_fn_direct = getattr(bot, "answer_callback_query", None)
            if not callable(answer_fn_direct):
                return
            chat_id = _optional_int(interaction.thread.chat_id)
            thread_id = _optional_int(interaction.thread.thread_id)
            await answer_fn_direct(
                interaction.interaction_id,
                chat_id=chat_id,
                thread_id=thread_id,
                text=text or "",
            )
            return
        callback = _CallbackStub(
            callback_id=interaction.interaction_id,
            chat_id=_optional_int(interaction.thread.chat_id),
            thread_id=_optional_int(interaction.thread.thread_id),
        )
        await answer_fn(callback, text or "")

    async def present_actions(
        self,
        thread: ChatThreadRef,
        text: str,
        *,
        actions: Sequence[ChatAction] = (),
        reply_to: Optional[ChatMessageRef] = None,
        parse_mode: Optional[str] = None,
    ) -> ChatMessageRef:
        bot = getattr(self._handlers, "_bot", None)
        if bot is None:
            raise RuntimeError("no bot available for send")
        chat_id = _require_int(thread.chat_id)
        thread_id = _optional_int(thread.thread_id)
        reply_to_id = _optional_int(reply_to.message_id) if reply_to else None
        reply_markup = _build_reply_markup(actions) if actions else None
        response = await bot.send_message(
            chat_id,
            text,
            message_thread_id=thread_id,
            reply_to_message_id=reply_to_id,
            reply_markup=reply_markup,
        )
        msg_id = response.get("message_id") if isinstance(response, dict) else None
        return ChatMessageRef(
            thread=thread,
            message_id=str(msg_id) if msg_id is not None else "0",
        )

    async def send_text(
        self,
        thread: ChatThreadRef,
        text: str,
        *,
        reply_to: Optional[ChatMessageRef] = None,
        parse_mode: Optional[str] = None,
    ) -> ChatMessageRef:
        bot = getattr(self._handlers, "_bot", None)
        if bot is None:
            raise RuntimeError("no bot available for send")
        chat_id = _require_int(thread.chat_id)
        thread_id = _optional_int(thread.thread_id)
        reply_to_id = _optional_int(reply_to.message_id) if reply_to else None
        response = await bot.send_message(
            chat_id,
            text,
            message_thread_id=thread_id,
            reply_to_message_id=reply_to_id,
            parse_mode=parse_mode,
        )
        msg_id = response.get("message_id") if isinstance(response, dict) else None
        return ChatMessageRef(
            thread=thread,
            message_id=str(msg_id) if msg_id is not None else "0",
        )


class _CallbackStub:
    def __init__(
        self,
        *,
        callback_id: str,
        chat_id: Optional[int] = None,
        thread_id: Optional[int] = None,
    ) -> None:
        self.callback_id = callback_id
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.message_id: Optional[int] = None


def _build_reply_markup(actions: Sequence[ChatAction]) -> Optional[dict[str, Any]]:
    if not actions:
        return None
    keyboard = []
    for action in actions:
        callback_data = (
            action.payload if action.payload is not None else action.action_id
        )
        keyboard.append([{"text": action.label, "callback_data": callback_data}])
    return {"inline_keyboard": keyboard}


def _require_int(raw: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"id must be numeric: {raw!r}") from exc


def _optional_int(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _make_state_writer(
    handlers: Any,
) -> ChatOperationStateWriter:
    async def _write(
        operation_id: Optional[str],
        *,
        state: ChatOperationState,
        **changes: Any,
    ) -> None:
        marker = getattr(handlers, "_mark_chat_operation_state", None)
        if callable(marker):
            await marker(operation_id, state=state, **changes)

    return _write


async def telegram_immediate_callback_ack(
    handlers: Any,
    *,
    callback_id: Optional[str],
    chat_id: Optional[int],
    thread_id: Optional[int] = None,
    message_id: Optional[int] = None,
    operation_id: Optional[str] = None,
    ack_text: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> ImmediateAckResult:
    transport = _TelegramFeedbackTransport(handlers)
    interaction = _interaction_ref(callback_id, chat_id, thread_id)
    state_writer = _make_state_writer(handlers)
    return await immediate_ack(
        transport,
        interaction,
        ack_class="callback_answer",
        operation_id=operation_id,
        state_writer=state_writer,
        anchor_message_reuse="never",
        existing_anchor=_message_ref(chat_id, message_id, thread_id),
        logger=logger or getattr(handlers, "_logger", None),
    )


async def telegram_publish_queued_notice(
    handlers: Any,
    *,
    chat_id: Optional[int],
    thread_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
    text: str = QUEUED_NOTICE_TEXT,
    cancel_action_id: str = "cancel:queue_cancel",
    cancel_label: str = "Cancel",
    operation_id: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> QueuedNoticeResult:
    transport = _TelegramFeedbackTransport(handlers)
    thread = _thread_ref(chat_id, thread_id)
    reply_to = _message_ref(chat_id, reply_to_message_id, thread_id)
    state_writer = _make_state_writer(handlers)
    actions: tuple[ChatAction, ...] = ()
    if reply_to_message_id is not None:
        source = str(reply_to_message_id)
        actions = (
            ChatAction(
                label=cancel_label,
                action_id=encode_cancel_callback(f"queue_cancel:{source}"),
            ),
            ChatAction(
                label="Interrupt + Send",
                action_id=encode_cancel_callback(f"queue_interrupt_send:{source}"),
            ),
        )
    return await publish_queued_notice(
        transport,
        thread,
        reply_to=reply_to,
        text=text,
        actions=actions,
        cancel_action_id=cancel_action_id,
        cancel_label=cancel_label,
        operation_id=operation_id,
        state_writer=state_writer,
        logger=logger or getattr(handlers, "_logger", None),
    )


__all__ = [
    "telegram_immediate_callback_ack",
    "telegram_publish_queued_notice",
]
