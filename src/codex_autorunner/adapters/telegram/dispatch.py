from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Optional

from ...core.logging_utils import log_event
from ...core.orchestration import ChatOperationState
from ...core.request_context import reset_conversation_id, set_conversation_id
from ...core.state import now_iso
from ..chat.action_ux_contract import (
    callback_entry_bypasses_queue,
    telegram_callback_ux_contract_for_callback,
)
from ..chat.chat_ux_telemetry import (
    ChatUxMilestone,
    ChatUxTimingSnapshot,
    emit_chat_ux_timing,
)
from ..chat.queue_status import build_queue_item_preview
from .adapter import (
    TelegramUpdate,
    allowlist_allows,
)
from .chat_callbacks import TelegramCallbackCodec
from .immediate_feedback_bridge import (
    telegram_immediate_callback_ack,
)
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
    operation_id: Optional[str] = None


DispatchRoute = Callable[[Any, TelegramUpdate, DispatchContext], Awaitable[None]]


async def _mark_chat_operation_state(
    handlers: Any,
    operation_id: Optional[str],
    *,
    state: ChatOperationState,
    **changes: Any,
) -> None:
    marker = getattr(handlers, "_mark_chat_operation_state", None)
    if not callable(marker):
        return
    await marker(operation_id, state=state, **changes)


def _with_chat_operation(
    handlers: Any,
    operation_id: Optional[str],
    work: Callable[[], Awaitable[None]],
) -> Callable[[], Awaitable[None]]:
    wrapper = getattr(handlers, "_with_chat_operation", None)
    if not callable(wrapper):
        return work
    return wrapper(operation_id, work)


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
        except (
            Exception
        ) as exc:  # intentional: typing indicator is best-effort; API errors must not disrupt message handling
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
            except (
                Exception
            ) as exc:  # intentional: typing indicator is best-effort; API errors must not disrupt message handling
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
        except (ValueError, TypeError):
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
    operation_id = getattr(context, "operation_id", None)

    decoded = TelegramCallbackCodec().decode(callback.data)
    callback_ux = (
        telegram_callback_ux_contract_for_callback(
            decoded.callback_id,
            decoded.payload,
        )
        if decoded is not None
        else None
    )

    should_bypass_queue = callback_entry_bypasses_queue(callback_ux)

    target_state = (
        ChatOperationState.INTERRUPTING
        if callback_ux is not None and callback_ux.control_priority == "interrupt"
        else ChatOperationState.RUNNING
    )

    async def _handle() -> None:
        await _mark_chat_operation_state(
            handlers,
            operation_id,
            state=target_state,
        )
        try:
            await handlers._handle_callback(callback)
        except Exception:
            await _mark_chat_operation_state(
                handlers,
                operation_id,
                state=ChatOperationState.FAILED,
                terminal_outcome="failed",
            )
            raise
        await _mark_chat_operation_state(
            handlers,
            operation_id,
            state=ChatOperationState.COMPLETED,
        )

    await telegram_immediate_callback_ack(
        handlers,
        callback_id=callback.callback_id,
        chat_id=callback.chat_id,
        thread_id=callback.thread_id,
        message_id=callback.message_id,
        operation_id=operation_id,
        ux_entry=callback_ux,
        logger=handlers._logger if hasattr(handlers, "_logger") else None,
    )

    if context.topic_key:
        if not should_bypass_queue:
            handlers._enqueue_topic_work(
                context.topic_key,
                lambda: _with_chat_operation(
                    handlers,
                    operation_id,
                    lambda: _run_with_typing_indicator(
                        handlers,
                        chat_id=callback.chat_id,
                        thread_id=callback.thread_id,
                        work=_handle,
                    ),
                )(),
                force_queue=True,
            )
            return
    await _with_chat_operation(
        handlers,
        operation_id,
        lambda: _run_with_typing_indicator(
            handlers,
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            work=_handle,
        ),
    )()


async def _dispatch_message(
    handlers: Any, update: TelegramUpdate, context: DispatchContext
) -> None:
    message = update.message
    if message is None:
        return
    operation_id = getattr(context, "operation_id", None)

    async def _handle() -> None:
        await _mark_chat_operation_state(
            handlers,
            operation_id,
            state=ChatOperationState.RUNNING,
        )
        try:
            await handlers._handle_message(message)
        except Exception:
            await _mark_chat_operation_state(
                handlers,
                operation_id,
                state=ChatOperationState.FAILED,
                terminal_outcome="failed",
            )
            raise
        await _mark_chat_operation_state(
            handlers,
            operation_id,
            state=ChatOperationState.COMPLETED,
        )

    if context.topic_key:
        if handlers._should_bypass_topic_queue(message):
            await _with_chat_operation(
                handlers,
                operation_id,
                lambda: _run_with_typing_indicator(
                    handlers,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    work=_handle,
                ),
            )()
            return

        runtime = _get_topic_runtime(handlers, context.topic_key)
        is_busy = runtime is not None and (
            runtime.current_turn_id is not None or runtime.queue.pending() > 0
        )

        await _mark_chat_operation_state(
            handlers,
            operation_id,
            state=ChatOperationState.QUEUED,
        )
        enqueue_kwargs = {
            "force_queue": True,
            "item_id": str(message.message_id),
            "item_label": build_queue_item_preview(
                message.text or message.caption,
                fallback=f"Request {message.message_id}",
            ),
        }
        try:
            handlers._enqueue_topic_work(
                context.topic_key,
                lambda: _with_chat_operation(
                    handlers,
                    operation_id,
                    lambda: _run_with_typing_indicator(
                        handlers,
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                        work=_handle,
                    ),
                )(),
                **enqueue_kwargs,
            )
        except TypeError as exc:
            if "item_label" not in str(exc):
                raise
            enqueue_kwargs.pop("item_label", None)
            handlers._enqueue_topic_work(
                context.topic_key,
                lambda: _with_chat_operation(
                    handlers,
                    operation_id,
                    lambda: _run_with_typing_indicator(
                        handlers,
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                        work=_handle,
                    ),
                )(),
                **enqueue_kwargs,
            )
        if is_busy:
            queue_snapshot = ChatUxTimingSnapshot(
                platform="telegram",
                conversation_id=context.topic_key,
                channel_id=(
                    str(message.chat_id) if message.chat_id is not None else None
                ),
            )
            queue_snapshot.record(ChatUxMilestone.RAW_EVENT_RECEIVED)
            queue_snapshot.record(ChatUxMilestone.ACK_FINISHED)
            queue_snapshot.record(ChatUxMilestone.FIRST_VISIBLE_FEEDBACK)
            refresh_queue_status = getattr(
                handlers,
                "_refresh_topic_queue_status_message",
                None,
            )
            if callable(refresh_queue_status):
                await refresh_queue_status(
                    topic_key=context.topic_key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    reply_to_message_id=message.message_id,
                )
                queue_snapshot.record(ChatUxMilestone.QUEUE_VISIBLE)
                queue_snapshot.record(ChatUxMilestone.TERMINAL_DELIVERY)
                emit_chat_ux_timing(
                    handlers._logger,
                    logging.INFO,
                    queue_snapshot,
                    event_suffix="managed_thread_turn",
                    topic_key=context.topic_key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    status="queued",
                )
        return
    await _with_chat_operation(
        handlers,
        operation_id,
        lambda: _run_with_typing_indicator(
            handlers,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            work=_handle,
        ),
    )()


def _get_topic_runtime(handlers: Any, topic_key_str: str) -> Any:
    router = getattr(handlers, "_router", None)
    if router is None:
        return None
    runtime_fn = getattr(router, "runtime_for", None)
    if not callable(runtime_fn):
        return None
    return runtime_fn(topic_key_str)


_ROUTES: tuple[tuple[str, DispatchRoute], ...] = (
    ("callback", _dispatch_callback),
    ("message", _dispatch_message),
)


def _operation_identity(
    update: TelegramUpdate,
) -> tuple[Optional[str], Optional[str], str]:
    if update.callback is not None:
        update_id = update.callback.update_id
        if update_id is not None:
            value = f"telegram:update:{update_id}"
            return value, value, "callback"
        callback_id = str(update.callback.callback_id or "").strip()
        if callback_id:
            value = f"telegram:callback:{callback_id}"
            return value, value, "callback"
        return None, None, "callback"
    if update.message is not None:
        update_id = update.message.update_id
        if update_id is not None:
            value = f"telegram:update:{update_id}"
            return value, value, "message"
        message_id = update.message.message_id
        if message_id is not None:
            value = f"telegram:message:{message_id}"
            return value, value, "message"
    return None, None, "message"


async def dispatch_update(handlers: Any, update: TelegramUpdate) -> None:
    context = await _build_context(handlers, update)
    conversation_id = None
    if context.chat_id is not None:
        try:
            conversation_id = topic_key(context.chat_id, context.thread_id)
        except (ValueError, TypeError):
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
        operation_id, surface_operation_key, kind = _operation_identity(update)
        if operation_id and surface_operation_key:
            inserted = await handlers._register_accepted_chat_operation(
                operation_id=operation_id,
                surface_operation_key=surface_operation_key,
                conversation_id=conversation_id,
                chat_id=context.chat_id,
                thread_id=context.thread_id,
                user_id=context.user_id,
                message_id=context.message_id,
                kind=kind,
            )
            if not inserted:
                log_event(
                    handlers._logger,
                    logging.INFO,
                    "telegram.update.duplicate",
                    update_id=update.update_id,
                    chat_id=context.chat_id,
                    thread_id=context.thread_id,
                    message_id=context.message_id,
                    conversation_id=conversation_id,
                    operation_id=operation_id,
                    duplicate_source="shared_operation_ledger",
                )
                return
            context = replace(context, operation_id=operation_id)
        for name, route in _ROUTES:
            if name == "callback" and update.callback:
                await route(handlers, update, context)
                return
            if name == "message" and update.message:
                await route(handlers, update, context)
                return
    finally:
        reset_conversation_id(token)
