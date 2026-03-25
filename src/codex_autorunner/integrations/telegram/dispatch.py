from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ...core.logging_utils import log_event
from ...core.request_context import reset_conversation_id, set_conversation_id
from .adapter import (
    ApprovalCallback,
    CancelCallback,
    QuestionCancelCallback,
    QuestionCustomCallback,
    QuestionDoneCallback,
    QuestionOptionCallback,
    TelegramUpdate,
    allowlist_allows,
)
from .chat_callbacks import parse_callback_data
from .state import topic_key


@dataclass(frozen=True)
class DispatchContext:
    chat_id: Optional[int]
    user_id: Optional[int]
    thread_id: Optional[int]
    message_id: Optional[int]
    is_topic: Optional[bool]
    is_edited: Optional[bool]
    topic_key: Optional[str]


DispatchRoute = Callable[[Any, TelegramUpdate, DispatchContext], Awaitable[None]]


async def _run_with_typing_indicator(
    handlers: Any,
    *,
    chat_id: Optional[int],
    thread_id: Optional[int],
    work: Callable[[], Awaitable[None]],
) -> None:
    if chat_id is None:
        await work()
        return
    begin = getattr(handlers, "_begin_typing_indicator", None)
    end = getattr(handlers, "_end_typing_indicator", None)
    began = False
    if callable(begin):
        try:
            await begin(chat_id, thread_id)
            began = True
        except Exception as exc:
            log_event(
                handlers._logger,
                logging.DEBUG,
                "telegram.typing.begin.failed",
                chat_id=chat_id,
                thread_id=thread_id,
                exc=exc,
            )
    try:
        await work()
    finally:
        if began and callable(end):
            try:
                await end(chat_id, thread_id)
            except Exception as exc:
                log_event(
                    handlers._logger,
                    logging.DEBUG,
                    "telegram.typing.end.failed",
                    chat_id=chat_id,
                    thread_id=thread_id,
                    exc=exc,
                )


async def _build_context(handlers: Any, update: TelegramUpdate) -> DispatchContext:
    chat_id = None
    user_id = None
    thread_id = None
    message_id = None
    is_topic = None
    is_edited = None
    key = None
    if update.message:
        chat_id = update.message.chat_id
        user_id = update.message.from_user_id
        thread_id = update.message.thread_id
        message_id = update.message.message_id
        is_topic = update.message.is_topic_message
        is_edited = update.message.is_edited
        key = await handlers._resolve_topic_key(chat_id, thread_id)
    elif update.callback:
        chat_id = update.callback.chat_id
        user_id = update.callback.from_user_id
        thread_id = update.callback.thread_id
        message_id = update.callback.message_id
        if chat_id is not None:
            key = await handlers._resolve_topic_key(chat_id, thread_id)
    return DispatchContext(
        chat_id=chat_id,
        user_id=user_id,
        thread_id=thread_id,
        message_id=message_id,
        is_topic=is_topic,
        is_edited=is_edited,
        topic_key=key,
    )


def _log_denied(handlers: Any, update: TelegramUpdate) -> None:
    chat_id = None
    user_id = None
    thread_id = None
    message_id = None
    update_id = None
    conversation_id = None
    if update.message:
        chat_id = update.message.chat_id
        user_id = update.message.from_user_id
        thread_id = update.message.thread_id
        message_id = update.message.message_id
        update_id = update.message.update_id
    elif update.callback:
        chat_id = update.callback.chat_id
        user_id = update.callback.from_user_id
        thread_id = update.callback.thread_id
        message_id = update.callback.message_id
        update_id = update.callback.update_id
    if chat_id is not None:
        try:
            conversation_id = topic_key(chat_id, thread_id)
        except Exception:
            conversation_id = None
    log_event(
        handlers._logger,
        logging.INFO,
        "telegram.allowlist.denied",
        chat_id=chat_id,
        user_id=user_id,
        thread_id=thread_id,
        message_id=message_id,
        update_id=update_id,
        conversation_id=conversation_id,
    )


async def _dispatch_callback(
    handlers: Any, update: TelegramUpdate, context: DispatchContext
) -> None:
    callback = update.callback
    if callback is None:
        return

    async def _handle() -> None:
        await handlers._handle_callback(callback)

    parsed = parse_callback_data(callback.data)
    should_bypass_queue = isinstance(
        parsed,
        (
            ApprovalCallback,
            QuestionOptionCallback,
            QuestionDoneCallback,
            QuestionCustomCallback,
            QuestionCancelCallback,
        ),
    ) or (
        isinstance(parsed, CancelCallback)
        and (
            parsed.kind == "interrupt"
            or parsed.kind.startswith("queue_cancel:")
            or parsed.kind.startswith("queue_interrupt_send:")
        )
    )
    if context.topic_key:
        if not should_bypass_queue:
            handlers._enqueue_topic_work(
                context.topic_key,
                lambda: _run_with_typing_indicator(
                    handlers,
                    chat_id=callback.chat_id,
                    thread_id=callback.thread_id,
                    work=_handle,
                ),
                force_queue=True,
            )
            return
    await _run_with_typing_indicator(
        handlers,
        chat_id=callback.chat_id,
        thread_id=callback.thread_id,
        work=_handle,
    )


async def _dispatch_message(
    handlers: Any, update: TelegramUpdate, context: DispatchContext
) -> None:
    message = update.message
    if message is None:
        return

    async def _handle() -> None:
        await handlers._handle_message(message)

    if context.topic_key:
        if handlers._should_bypass_topic_queue(message):
            await _run_with_typing_indicator(
                handlers,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                work=_handle,
            )
            return
        await handlers._maybe_send_queued_placeholder(
            message, topic_key=context.topic_key
        )
        handlers._enqueue_topic_work(
            context.topic_key,
            lambda: _run_with_typing_indicator(
                handlers,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                work=_handle,
            ),
            force_queue=True,
            item_id=str(message.message_id),
        )
        return
    await _run_with_typing_indicator(
        handlers,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        work=_handle,
    )


_ROUTES: tuple[tuple[str, DispatchRoute], ...] = (
    ("callback", _dispatch_callback),
    ("message", _dispatch_message),
)


async def dispatch_update(handlers: Any, update: TelegramUpdate) -> None:
    from ...core.state import now_iso

    context = await _build_context(handlers, update)
    conversation_id = None
    if context.chat_id is not None:
        try:
            conversation_id = topic_key(context.chat_id, context.thread_id)
        except Exception:
            conversation_id = None
    token = set_conversation_id(conversation_id)
    try:
        log_event(
            handlers._logger,
            logging.INFO,
            "telegram.update.received",
            update_id=update.update_id,
            chat_id=context.chat_id,
            user_id=context.user_id,
            thread_id=context.thread_id,
            message_id=context.message_id,
            is_topic=context.is_topic,
            is_edited=context.is_edited,
            has_message=bool(update.message),
            has_callback=bool(update.callback),
            update_received_at=now_iso(),
            conversation_id=conversation_id,
        )
        if (
            update.update_id is not None
            and context.topic_key
            and not await handlers._should_process_update(
                context.topic_key, update.update_id
            )
        ):
            log_event(
                handlers._logger,
                logging.INFO,
                "telegram.update.duplicate",
                update_id=update.update_id,
                chat_id=context.chat_id,
                thread_id=context.thread_id,
                message_id=context.message_id,
                conversation_id=conversation_id,
            )
            return
        if not allowlist_allows(update, handlers._allowlist):
            _log_denied(handlers, update)
            return
        for name, route in _ROUTES:
            if name == "callback" and update.callback:
                await route(handlers, update, context)
                return
            if name == "message" and update.message:
                await route(handlers, update, context)
                return
    finally:
        reset_conversation_id(token)
