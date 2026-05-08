from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Optional

from ....core.logging_utils import log_event
from ....core.utils import canonicalize_path
from ...chat.forwarding import compose_forwarded_message_text
from ...chat.handlers.messages import message_text_candidate
from ..adapter import (
    TelegramMessage,
    is_interrupt_alias,
    parse_command,
)
from ..constants import TELEGRAM_MAX_MESSAGE_LENGTH
from ..forwarding import (
    format_forwarded_telegram_message_text,
    is_forwarded_telegram_message,
    message_forward_info,
)
from ..ui_state import TelegramUiState
from .media_ingress import (
    MediaBatchBuffer as _MediaBatchBuffer,
)
from .media_ingress import (
    handle_media_message as _handle_media_message_impl,
)
from .media_ingress import (
    has_batchable_media,
    media_batch_key,
    message_has_media,
)
from .message_policy import (
    activated_record_allows_plain_text_turn as _activated_record_allows_plain_text_turn,
)
from .message_policy import (
    evaluate_message_policy as _evaluate_message_policy,
)
from .message_policy import (
    event_logger as _event_logger,
)
from .message_policy import (
    log_message_policy_result as _log_message_policy_result,
)
from .questions import handle_custom_text_input
from .surface_ingress import (
    TelegramSurfaceTurnDispatch as _TelegramSurfaceTurnDispatch,
)
from .surface_ingress import (
    submit_telegram_surface_turn as _submit_telegram_surface_turn,
)

_logger = logging.getLogger(__name__)

COALESCE_LONG_MESSAGE_WINDOW_SECONDS = 6.0
COALESCE_LONG_MESSAGE_THRESHOLD = TELEGRAM_MAX_MESSAGE_LENGTH - 256


async def _run_with_typing_indicator(
    handlers: Any,
    *,
    chat_id: Optional[int],
    thread_id: Optional[int],
    work: Any,
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
        except Exception:
            began = False
    try:
        await work()
    finally:
        if began and callable(end):
            try:
                await end(chat_id, thread_id)
            except Exception:
                _logger.debug("typing indicator end failed", exc_info=True)


def _has_pending_custom_question(handlers: Any, message: TelegramMessage) -> bool:
    for pending in handlers._pending_questions.values():
        if (
            pending.awaiting_custom_input
            and pending.chat_id == message.chat_id
            and (pending.thread_id is None or pending.thread_id == message.thread_id)
        ):
            return True
    return False


def _ui_state(handlers: Any) -> TelegramUiState:
    state = getattr(handlers, "_ui_state", None)
    if isinstance(state, TelegramUiState):
        return state
    fallback = TelegramUiState()
    fallback.pending_questions = getattr(handlers, "_pending_questions", {})
    fallback.resume_options = getattr(handlers, "_resume_options", {})
    fallback.bind_options = getattr(handlers, "_bind_options", {})
    fallback.flow_run_options = getattr(handlers, "_flow_run_options", {})
    fallback.update_options = getattr(handlers, "_update_options", {})
    fallback.update_confirm_options = getattr(handlers, "_update_confirm_options", {})
    fallback.review_commit_options = getattr(handlers, "_review_commit_options", {})
    fallback.review_commit_subjects = getattr(handlers, "_review_commit_subjects", {})
    fallback.pending_review_custom = getattr(handlers, "_pending_review_custom", {})
    fallback.compact_pending = getattr(handlers, "_compact_pending", {})
    fallback.agent_options = getattr(handlers, "_agent_options", {})
    fallback.agent_profile_options = getattr(handlers, "_agent_profile_options", {})
    fallback.model_options = getattr(handlers, "_model_options", {})
    fallback.model_pending = getattr(handlers, "_model_pending", {})
    return fallback


@dataclass
class _CoalescedBuffer:
    message: TelegramMessage
    parts: list[str]
    topic_key: str
    placeholder_id: Optional[int] = None
    task: Optional[asyncio.Task[None]] = None
    last_received_at: float = 0.0
    last_part_len: int = 0


def _message_text_candidate(message: TelegramMessage) -> tuple[str, str, Any]:
    return message_text_candidate(
        text=message.text,
        caption=message.caption,
        entities=message.entities,
        caption_entities=message.caption_entities,
    )


async def _clear_pending_options(
    handlers: Any, key: str, message: TelegramMessage
) -> None:
    actor_id = str(message.from_user_id) if message.from_user_id is not None else None
    pending_review_custom = _ui_state(handlers).clear_pending_options(key, actor_id)
    await handlers._dismiss_review_custom_prompt(message, pending_review_custom)


async def _enqueue_or_run_topic_work(
    handlers: Any,
    key: str,
    *,
    chat_id: int,
    thread_id: Optional[int],
    placeholder_id: Optional[int],
    work: Any,
) -> None:
    async def _run_wrapped() -> None:
        await _run_placeholder_wrapped_work(
            handlers,
            chat_id=chat_id,
            placeholder_id=placeholder_id,
            work=work,
        )

    async def _run_wrapped_with_typing() -> None:
        await _run_with_typing_indicator(
            handlers,
            chat_id=chat_id,
            thread_id=thread_id,
            work=_run_wrapped,
        )

    enqueue = getattr(handlers, "_enqueue_topic_work", None)
    if callable(enqueue):
        enqueue(key, _run_wrapped_with_typing)
        return
    await _run_wrapped_with_typing()


async def _enqueue_or_run_topic_call(
    handlers: Any,
    key: str,
    *,
    chat_id: int,
    thread_id: Optional[int],
    placeholder_id: Optional[int],
    callback: Any,
) -> None:
    await _enqueue_or_run_topic_work(
        handlers,
        key,
        chat_id=chat_id,
        thread_id=thread_id,
        placeholder_id=placeholder_id,
        work=callback,
    )


async def _run_placeholder_wrapped_work(
    handlers: Any,
    *,
    chat_id: int,
    placeholder_id: Optional[int],
    work: Any,
) -> None:
    wrapped = work
    wrap = getattr(handlers, "_wrap_placeholder_work", None)
    if callable(wrap):
        wrapped = wrap(
            chat_id=chat_id,
            placeholder_id=placeholder_id,
            work=work,
        )
    if callable(wrapped):
        await wrapped()
        return
    await wrapped


async def _clear_message_placeholder(
    handlers: Any,
    message: TelegramMessage,
    placeholder_id: Optional[int],
) -> None:
    if placeholder_id is not None:
        await handlers._delete_message(message.chat_id, placeholder_id)


async def handle_message(handlers: Any, message: TelegramMessage) -> None:
    claim_queued_placeholder = getattr(handlers, "_claim_queued_placeholder", None)
    placeholder_id = (
        claim_queued_placeholder(message.chat_id, message.message_id)
        if callable(claim_queued_placeholder)
        else None
    )
    refresh_queue_status = getattr(
        handlers, "_refresh_topic_queue_status_message", None
    )
    if callable(refresh_queue_status):
        resolve_topic_key = getattr(handlers, "_resolve_topic_key", None)
        if callable(resolve_topic_key):
            topic_key = resolve_topic_key(message.chat_id, message.thread_id)
            if inspect.isawaitable(topic_key):
                topic_key = await topic_key
        else:
            topic_key = None
        if isinstance(topic_key, str) and topic_key:
            await refresh_queue_status(
                topic_key=topic_key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            )
    if message.is_edited:
        await handle_edited_message(handlers, message, placeholder_id=placeholder_id)
        return

    _raw_text, text_candidate, entities = _message_text_candidate(message)
    trimmed_text = text_candidate.strip()
    has_media = message_has_media(message)
    is_forwarded = is_forwarded_telegram_message(message)
    if not trimmed_text and not has_media:
        if placeholder_id is not None:
            await handlers._delete_message(message.chat_id, placeholder_id)
        return

    if trimmed_text and not has_media:
        if _has_pending_custom_question(handlers, message):
            policy_result = _evaluate_message_policy(
                handlers,
                message,
                text=trimmed_text,
                is_explicit_command=not is_forwarded,
            )
            if not policy_result.command_allowed:
                _log_message_policy_result(handlers, message, policy_result)
                if placeholder_id is not None:
                    await handlers._delete_message(message.chat_id, placeholder_id)
                return
        custom_handled = await handle_custom_text_input(handlers, message)
        if custom_handled:
            return

    should_bypass = False
    if trimmed_text:
        if is_interrupt_alias(trimmed_text) and not is_forwarded:
            should_bypass = True
        elif trimmed_text.startswith("!") and not has_media and not is_forwarded:
            should_bypass = True
        elif not is_forwarded and parse_command(
            text_candidate, entities=entities, bot_username=handlers._bot_username
        ):
            should_bypass = True

    if has_media and not should_bypass:
        if handlers._config.media.batch_uploads and has_batchable_media(message):
            if handlers._config.media.enabled:
                topic_key = await handlers._resolve_topic_key(
                    message.chat_id, message.thread_id
                )
                await _clear_pending_options(handlers, topic_key, message)
                await flush_coalesced_message(handlers, message)
                await buffer_media_batch(
                    handlers, message, placeholder_id=placeholder_id
                )
                return
            should_bypass = True
        else:
            should_bypass = True

    if should_bypass:
        await flush_coalesced_message(handlers, message)
        await handle_message_inner(handlers, message, placeholder_id=placeholder_id)
        return

    if is_forwarded:
        await flush_coalesced_message(handlers, message)
        await handle_message_inner(handlers, message, placeholder_id=placeholder_id)
        return

    await buffer_coalesced_message(
        handlers, message, text_candidate, placeholder_id=placeholder_id
    )


def should_bypass_topic_queue(handlers: Any, message: TelegramMessage) -> bool:
    for pending in handlers._pending_questions.values():
        if (
            pending.awaiting_custom_input
            and pending.chat_id == message.chat_id
            and (pending.thread_id is None or pending.thread_id == message.thread_id)
        ):
            return True
    _raw_text, text_candidate, entities = _message_text_candidate(message)
    if not text_candidate:
        return False
    trimmed_text = text_candidate.strip()
    if not trimmed_text:
        return False
    if is_forwarded_telegram_message(message):
        return False
    if is_interrupt_alias(trimmed_text):
        return True
    command = parse_command(
        text_candidate, entities=entities, bot_username=handlers._bot_username
    )
    if not command:
        return False
    spec = handlers._command_specs.get(command.name)
    return bool(spec and spec.allow_during_turn)


async def handle_edited_message(
    handlers: Any,
    message: TelegramMessage,
    *,
    placeholder_id: Optional[int] = None,
) -> None:
    text = (message.text or "").strip()
    if not text:
        text = (message.caption or "").strip()
    if not text:
        if placeholder_id is not None:
            await handlers._delete_message(message.chat_id, placeholder_id)
        return
    key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    runtime = handlers._router.runtime_for(key)
    turn_key = runtime.current_turn_key
    if not turn_key:
        if placeholder_id is not None:
            await handlers._delete_message(message.chat_id, placeholder_id)
        return
    ctx = handlers._turn_contexts.get(turn_key)
    if ctx is None or ctx.reply_to_message_id != message.message_id:
        await _clear_message_placeholder(handlers, message, placeholder_id)
        return
    await handlers._handle_interrupt(message, runtime)
    edited_text = f"Edited: {text}"

    await _enqueue_or_run_topic_call(
        handlers,
        key,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        placeholder_id=placeholder_id,
        callback=partial(
            handlers._handle_normal_message,
            message,
            runtime,
            text_override=edited_text,
            placeholder_id=placeholder_id,
        ),
    )


async def handle_message_inner(
    handlers: Any,
    message: TelegramMessage,
    *,
    topic_key: Optional[str] = None,
    placeholder_id: Optional[int] = None,
) -> None:
    raw_text = message.text or ""
    raw_caption = message.caption or ""
    text = raw_text.strip()
    entities = message.entities
    if not text:
        text = raw_caption.strip()
        entities = message.caption_entities
    has_media = message_has_media(message)
    if not text and not has_media:
        await _clear_message_placeholder(handlers, message, placeholder_id)
        return

    if isinstance(topic_key, str) and topic_key:
        key = topic_key
    else:
        key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    runtime = handlers._router.runtime_for(key)
    actor_id = str(message.from_user_id) if message.from_user_id is not None else None
    is_forwarded = is_forwarded_telegram_message(message)
    turn_text = format_forwarded_telegram_message_text(message, text)
    flow_reply_text = compose_forwarded_message_text(
        text,
        message_forward_info(message, text),
    )

    command_policy_result = _evaluate_message_policy(
        handlers,
        message,
        text=text,
        is_explicit_command=not is_forwarded,
    )

    if text and not command_policy_result.command_allowed:
        has_pending_state = _ui_state(handlers).has_policy_blocking_state(key)
        if has_pending_state:
            _log_message_policy_result(handlers, message, command_policy_result)
            await _clear_message_placeholder(handlers, message, placeholder_id)
            return

    if text and handlers._handle_pending_resume(
        key,
        text,
        user_id=message.from_user_id,
    ):
        await _clear_message_placeholder(handlers, message, placeholder_id)
        return
    if text and handlers._handle_pending_bind(
        key,
        text,
        user_id=message.from_user_id,
    ):
        await _clear_message_placeholder(handlers, message, placeholder_id)
        return

    if text and is_interrupt_alias(text) and not is_forwarded:
        if not command_policy_result.command_allowed:
            _log_message_policy_result(handlers, message, command_policy_result)
            await _clear_message_placeholder(handlers, message, placeholder_id)
            return
        await handlers._handle_interrupt(message, runtime)
        await _clear_message_placeholder(handlers, message, placeholder_id)
        return

    if text and text.startswith("!") and not has_media and not is_forwarded:
        if not command_policy_result.command_allowed:
            _log_message_policy_result(handlers, message, command_policy_result)
            await _clear_message_placeholder(handlers, message, placeholder_id)
            return
        _ui_state(handlers).clear_for_bang_command(key, actor_id)

        await _enqueue_or_run_topic_call(
            handlers,
            key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            placeholder_id=placeholder_id,
            callback=partial(handlers._handle_bang_shell, message, text, runtime),
        )
        return

    if text and await handlers._handle_pending_review_commit(
        message,
        runtime,
        key,
        text,
    ):
        await _clear_message_placeholder(handlers, message, placeholder_id)
        return

    command_text = raw_text if raw_text.strip() else raw_caption
    command = (
        parse_command(
            command_text, entities=entities, bot_username=handlers._bot_username
        )
        if command_text and not is_forwarded
        else None
    )
    if await handlers._handle_pending_review_custom(
        key, message, runtime, command, raw_text, raw_caption
    ):
        await _clear_message_placeholder(handlers, message, placeholder_id)
        return
    if command:
        pending_review_custom = _ui_state(handlers).clear_for_command(
            key, actor_id, command.name
        )
        await handlers._dismiss_review_custom_prompt(message, pending_review_custom)
    else:
        pending_review_custom = _ui_state(handlers).clear_for_message(key, actor_id)
        await handlers._dismiss_review_custom_prompt(message, pending_review_custom)
    if command:
        if not command_policy_result.command_allowed:
            _log_message_policy_result(handlers, message, command_policy_result)
            await _clear_message_placeholder(handlers, message, placeholder_id)
            return
        spec = handlers._command_specs.get(command.name)
        if spec and spec.allow_during_turn:
            handlers._spawn_task(
                _run_placeholder_wrapped_work(
                    handlers,
                    chat_id=message.chat_id,
                    placeholder_id=placeholder_id,
                    work=partial(handlers._handle_command, command, message, runtime),
                )
            )
        else:
            await _enqueue_or_run_topic_call(
                handlers,
                key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                placeholder_id=placeholder_id,
                callback=partial(handlers._handle_command, command, message, runtime),
            )
        return

    record = await handlers._router.get_topic(key)
    paused = None
    workspace_root: Optional[Path] = None
    pma_enabled = bool(record and getattr(record, "pma_enabled", False))
    notification_reply = None
    if message.reply_to_message_id is not None:
        hub_client = getattr(handlers, "_hub_client", None)
        if hub_client is not None:
            from ....core.hub_control_plane import (
                NotificationReplyTargetLookupRequest as _CPReplyTargetRequest,
            )

            try:
                cp_response = await hub_client.get_notification_reply_target(
                    _CPReplyTargetRequest(
                        surface_kind="telegram",
                        surface_key=key,
                        delivered_message_id=str(message.reply_to_message_id),
                    )
                )
                if cp_response.record is not None:
                    notification_reply = cp_response.record
            except Exception as exc:
                log_event(
                    _event_logger(handlers),
                    logging.WARNING,
                    "telegram.notification.reply_target.control_plane_failed",
                    topic_key=key,
                    message_id=message.message_id,
                    exc=exc,
                )
    if not pma_enabled and record and record.workspace_path:
        workspace_root = canonicalize_path(Path(record.workspace_path))
        preferred_run_id = handlers._ticket_flow_pause_targets.get(
            str(workspace_root), None
        )
        paused = handlers._get_paused_ticket_flow(
            workspace_root, preferred_run_id=preferred_run_id
        )
    policy_result = _evaluate_message_policy(
        handlers,
        message,
        text=text,
        is_explicit_command=False,
    )
    if not paused and not policy_result.should_start_turn:
        if _activated_record_allows_plain_text_turn(
            handlers,
            message,
            text=text,
            record=record,
            policy_result=policy_result,
        ):
            pass
        else:
            _log_message_policy_result(handlers, message, policy_result)
            await _clear_message_placeholder(handlers, message, placeholder_id)
            return

    if has_media:
        await _enqueue_or_run_topic_call(
            handlers,
            key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            placeholder_id=placeholder_id,
            callback=partial(
                handle_media_message,
                handlers,
                message,
                runtime,
                text,
                placeholder_id=placeholder_id,
            ),
        )
        return

    dispatch = _TelegramSurfaceTurnDispatch(
        handlers=handlers,
        message=message,
        runtime=runtime,
        record=record,
        topic_key=key,
        workspace_root=workspace_root or Path("."),
        prompt_text=turn_text,
        flow_reply_text=flow_reply_text,
        pma_enabled=pma_enabled,
        paused=paused,
        notification_reply=notification_reply,
        placeholder_id=placeholder_id,
    )
    await _enqueue_or_run_topic_call(
        handlers,
        key,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        placeholder_id=placeholder_id,
        callback=partial(_submit_telegram_surface_turn, dispatch),
    )


def coalesce_key_for_topic(handlers: Any, key: str, user_id: Optional[int]) -> str:
    if user_id is None:
        return f"{key}:user:unknown"
    return f"{key}:user:{user_id}"


async def coalesce_key(handlers: Any, message: TelegramMessage) -> str:
    key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    return coalesce_key_for_topic(handlers, key, message.from_user_id)


async def _ensure_key_lock(
    handlers: Any,
    *,
    locks_attr: str,
    guard_attr: str,
    key: str,
) -> asyncio.Lock:
    locks: dict[str, asyncio.Lock] = getattr(handlers, locks_attr)
    lock = locks.get(key)
    if lock is not None:
        return lock
    guard = getattr(handlers, guard_attr, None)
    if guard is None:
        guard = asyncio.Lock()
        setattr(handlers, guard_attr, guard)
    async with guard:
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
    return lock


async def buffer_coalesced_message(
    handlers: Any,
    message: TelegramMessage,
    text: str,
    *,
    placeholder_id: Optional[int] = None,
) -> None:
    topic_key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    key = coalesce_key_for_topic(handlers, topic_key, message.from_user_id)
    lock = await _ensure_key_lock(
        handlers,
        locks_attr="_coalesce_locks",
        guard_attr="_coalesce_locks_guard",
        key=key,
    )
    drop_placeholder = False
    async with lock:
        now = time.monotonic()
        buffer = handlers._coalesced_buffers.get(key)
        if buffer is None:
            buffer = _CoalescedBuffer(
                message=message,
                parts=[text],
                topic_key=topic_key,
                placeholder_id=placeholder_id,
                last_received_at=now,
                last_part_len=len(text),
            )
            handlers._coalesced_buffers[key] = buffer
        else:
            buffer.parts.append(text)
            buffer.last_received_at = now
            buffer.last_part_len = len(text)
            if placeholder_id is not None:
                if buffer.placeholder_id is None:
                    buffer.placeholder_id = placeholder_id
                else:
                    drop_placeholder = True
        handlers._touch_cache_timestamp("coalesced_buffers", key)
        task = buffer.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        window_seconds = handlers._config.coalesce_window_seconds
        buffer.task = handlers._spawn_task(
            coalesce_flush_after(handlers, key, window_seconds)
        )
    if drop_placeholder and placeholder_id is not None:
        await handlers._delete_message(message.chat_id, placeholder_id)


async def coalesce_flush_after(handlers: Any, key: str, window_seconds: float) -> None:
    try:
        await asyncio.sleep(window_seconds)
    except asyncio.CancelledError:
        return
    try:
        while True:
            buffer = handlers._coalesced_buffers.get(key)
            if buffer is None:
                return
            if buffer.last_part_len >= COALESCE_LONG_MESSAGE_THRESHOLD:
                elapsed = time.monotonic() - buffer.last_received_at
                long_window = max(window_seconds, COALESCE_LONG_MESSAGE_WINDOW_SECONDS)
                remaining = long_window - elapsed
                if remaining > 0:
                    try:
                        await asyncio.sleep(remaining)
                    except asyncio.CancelledError:
                        return
                    continue
            break
        await flush_coalesced_key(handlers, key)
    except Exception as exc:
        log_event(
            handlers._logger,
            logging.WARNING,
            "telegram.coalesce.flush_failed",
            key=key,
            exc=exc,
        )


async def flush_coalesced_message(handlers: Any, message: TelegramMessage) -> None:
    await flush_coalesced_key(handlers, await coalesce_key(handlers, message))


async def flush_coalesced_key(handlers: Any, key: str) -> None:
    lock = handlers._coalesce_locks.get(key)
    if lock is None:
        return
    buffer = None
    async with lock:
        buffer = handlers._coalesced_buffers.pop(key, None)
    if buffer is None:
        return
    task = buffer.task
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    combined_message = build_coalesced_message(buffer)

    async def _handle() -> None:
        await handle_message_inner(
            handlers,
            combined_message,
            topic_key=buffer.topic_key,
            placeholder_id=buffer.placeholder_id,
        )

    await _run_with_typing_indicator(
        handlers,
        chat_id=combined_message.chat_id,
        thread_id=combined_message.thread_id,
        work=_handle,
    )


def build_coalesced_message(buffer: _CoalescedBuffer) -> TelegramMessage:
    combined_text = "\n".join(buffer.parts)
    return dataclasses.replace(buffer.message, text=combined_text, caption=None)


async def buffer_media_batch(
    handlers: Any,
    message: TelegramMessage,
    *,
    placeholder_id: Optional[int] = None,
) -> None:
    if not has_batchable_media(message):
        return
    topic_key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    key = await media_batch_key(handlers, message)
    lock = await _ensure_key_lock(
        handlers,
        locks_attr="_media_batch_locks",
        guard_attr="_media_batch_locks_guard",
        key=key,
    )
    drop_placeholder = False
    async with lock:
        buffer = handlers._media_batch_buffers.get(key)
        if buffer is not None and len(buffer.messages) >= 10:
            if buffer.task and buffer.task is not asyncio.current_task():
                buffer.task.cancel()

            async def work(
                msgs: list[TelegramMessage] = buffer.messages,
                pid: Optional[int] = buffer.placeholder_id,
            ) -> None:
                await handlers._handle_media_batch(msgs, placeholder_id=pid)

            await _enqueue_or_run_topic_work(
                handlers,
                buffer.topic_key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                placeholder_id=buffer.placeholder_id,
                work=work,
            )
            handlers._media_batch_buffers.pop(key, None)
            buffer = None

        if buffer is None:
            buffer = _MediaBatchBuffer(
                topic_key=topic_key,
                messages=[message],
                placeholder_id=placeholder_id,
                media_group_id=message.media_group_id,
                created_at=time.monotonic(),
            )
            handlers._media_batch_buffers[key] = buffer
        else:
            buffer.messages.append(message)
            if placeholder_id is not None:
                if buffer.placeholder_id is None:
                    buffer.placeholder_id = placeholder_id
                else:
                    drop_placeholder = True

        handlers._touch_cache_timestamp("media_batch_buffers", key)
        task = buffer.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        window_seconds = handlers._config.media.batch_window_seconds
        buffer.task = handlers._spawn_task(
            flush_media_batch_after(handlers, key, window_seconds)
        )
    if drop_placeholder and placeholder_id is not None:
        await handlers._delete_message(message.chat_id, placeholder_id)


async def flush_media_batch_after(
    handlers: Any, key: str, window_seconds: float
) -> None:
    try:
        await asyncio.sleep(window_seconds)
    except asyncio.CancelledError:
        return
    try:
        await flush_media_batch_key(handlers, key)
    except Exception as exc:
        log_event(
            handlers._logger,
            logging.WARNING,
            "telegram.media_batch.flush_failed",
            key=key,
            exc=exc,
        )


async def flush_media_batch_key(handlers: Any, key: str) -> None:
    lock = handlers._media_batch_locks.get(key)
    if lock is None:
        return
    buffer = None
    async with lock:
        buffer = handlers._media_batch_buffers.pop(key, None)
        if buffer is None:
            return
        task = buffer.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        handlers._media_batch_locks.pop(key, None)
    if buffer.messages:

        async def work() -> None:
            await handlers._handle_media_batch(
                buffer.messages, placeholder_id=buffer.placeholder_id
            )

        first_message = buffer.messages[0]
        await _enqueue_or_run_topic_work(
            handlers,
            buffer.topic_key,
            chat_id=first_message.chat_id,
            thread_id=first_message.thread_id,
            placeholder_id=buffer.placeholder_id,
            work=work,
        )


async def handle_media_message(
    handlers: Any,
    message: TelegramMessage,
    runtime: Any,
    caption_text: str,
    *,
    placeholder_id: Optional[int] = None,
) -> None:
    await _handle_media_message_impl(
        handlers,
        message,
        runtime,
        caption_text,
        placeholder_id=placeholder_id,
    )
