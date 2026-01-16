from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ....core.logging_utils import log_event
from ..adapter import (
    TelegramDocument,
    TelegramMessage,
    TelegramPhotoSize,
    is_interrupt_alias,
    parse_command,
)
from ..config import TelegramMediaCandidate

COALESCE_WINDOW_SECONDS = 2.0
MEDIA_BATCH_WINDOW_SECONDS = 1.0
IMAGE_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
IMAGE_EXTS = set(IMAGE_CONTENT_TYPES.values())
MAX_BATCH_ITEMS = 10


@dataclass
class _CoalescedBuffer:
    message: TelegramMessage
    parts: list[str]
    topic_key: str
    task: Optional[asyncio.Task[None]] = None


@dataclass
class _MediaBatchBuffer:
    topic_key: str
    messages: list[TelegramMessage] = field(default_factory=list)
    task: Optional[asyncio.Task[None]] = None
    media_group_id: Optional[str] = None
    created_at: float = 0.0


def _message_text_candidate(message: TelegramMessage) -> tuple[str, str, Any]:
    raw_text = message.text or ""
    raw_caption = message.caption or ""
    text_candidate = raw_text if raw_text.strip() else raw_caption
    entities = message.entities if raw_text.strip() else message.caption_entities
    return raw_text, text_candidate, entities


async def handle_message(handlers: Any, message: TelegramMessage) -> None:
    if message.is_edited:
        await handle_edited_message(handlers, message)
        return

    _raw_text, text_candidate, entities = _message_text_candidate(message)
    trimmed_text = text_candidate.strip()
    has_media = message_has_media(message)
    if not trimmed_text and not has_media:
        return

    should_bypass = False
    if trimmed_text:
        if is_interrupt_alias(trimmed_text):
            should_bypass = True
        elif trimmed_text.startswith("!") and not has_media:
            should_bypass = True
        elif parse_command(
            text_candidate, entities=entities, bot_username=handlers._bot_username
        ):
            should_bypass = True

    if has_media and not should_bypass:
        if handlers._config.media.batch_uploads and has_batchable_media(message):
            if handlers._config.media.enabled:
                await flush_coalesced_message(handlers, message)
                await buffer_media_batch(handlers, message)
                return
            should_bypass = True
        else:
            should_bypass = True

    if should_bypass:
        await flush_coalesced_message(handlers, message)
        await handle_message_inner(handlers, message)
        return

    await buffer_coalesced_message(handlers, message, text_candidate)


def should_bypass_topic_queue(handlers: Any, message: TelegramMessage) -> bool:
    _raw_text, text_candidate, entities = _message_text_candidate(message)
    if not text_candidate:
        return False
    trimmed_text = text_candidate.strip()
    if not trimmed_text:
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


async def handle_edited_message(handlers: Any, message: TelegramMessage) -> None:
    text = (message.text or "").strip()
    if not text:
        text = (message.caption or "").strip()
    if not text:
        return
    key = handlers._resolve_topic_key(message.chat_id, message.thread_id)
    runtime = handlers._router.runtime_for(key)
    turn_key = runtime.current_turn_key
    if not turn_key:
        return
    ctx = handlers._turn_contexts.get(turn_key)
    if ctx is None or ctx.reply_to_message_id != message.message_id:
        return
    await handlers._handle_interrupt(message, runtime)
    edited_text = f"Edited: {text}"
    handlers._enqueue_topic_work(
        key,
        lambda: handlers._handle_normal_message(
            message,
            runtime,
            text_override=edited_text,
        ),
    )


async def handle_message_inner(
    handlers: Any, message: TelegramMessage, *, topic_key: Optional[str] = None
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
        return
    key = (
        topic_key
        if isinstance(topic_key, str) and topic_key
        else handlers._resolve_topic_key(message.chat_id, message.thread_id)
    )
    runtime = handlers._router.runtime_for(key)

    if text and handlers._handle_pending_resume(key, text):
        return
    if text and handlers._handle_pending_bind(key, text):
        return

    if text and is_interrupt_alias(text):
        await handlers._handle_interrupt(message, runtime)
        return

    if text and text.startswith("!") and not has_media:
        handlers._resume_options.pop(key, None)
        handlers._bind_options.pop(key, None)
        handlers._model_options.pop(key, None)
        handlers._model_pending.pop(key, None)
        handlers._enqueue_topic_work(
            key,
            lambda: handlers._handle_bang_shell(message, text, runtime),
        )
        return

    if text and await handlers._handle_pending_review_commit(
        message, runtime, key, text
    ):
        return

    command_text = raw_text if raw_text.strip() else raw_caption
    command = (
        parse_command(
            command_text, entities=entities, bot_username=handlers._bot_username
        )
        if command_text
        else None
    )
    if await handlers._handle_pending_review_custom(
        key, message, runtime, command, raw_text, raw_caption
    ):
        return
    if command:
        if command.name != "resume":
            handlers._resume_options.pop(key, None)
        if command.name != "bind":
            handlers._bind_options.pop(key, None)
        if command.name != "model":
            handlers._model_options.pop(key, None)
            handlers._model_pending.pop(key, None)
        if command.name != "review":
            handlers._review_commit_options.pop(key, None)
            handlers._review_commit_subjects.pop(key, None)
            pending_review_custom = handlers._pending_review_custom.pop(key, None)
            await handlers._dismiss_review_custom_prompt(message, pending_review_custom)
    else:
        handlers._resume_options.pop(key, None)
        handlers._bind_options.pop(key, None)
        handlers._model_options.pop(key, None)
        handlers._model_pending.pop(key, None)
        handlers._review_commit_options.pop(key, None)
        handlers._review_commit_subjects.pop(key, None)
        pending_review_custom = handlers._pending_review_custom.pop(key, None)
        await handlers._dismiss_review_custom_prompt(message, pending_review_custom)
    if command:
        spec = handlers._command_specs.get(command.name)
        if spec and spec.allow_during_turn:
            handlers._spawn_task(handlers._handle_command(command, message, runtime))
        else:
            handlers._enqueue_topic_work(
                key,
                lambda: handlers._handle_command(command, message, runtime),
            )
        return

    if has_media:
        handlers._enqueue_topic_work(
            key,
            lambda: handle_media_message(handlers, message, runtime, text),
        )
        return

    handlers._enqueue_topic_work(
        key,
        lambda: handlers._handle_normal_message(message, runtime, text_override=text),
    )


def coalesce_key_for_topic(handlers: Any, key: str, user_id: Optional[int]) -> str:
    if user_id is None:
        return f"{key}:user:unknown"
    return f"{key}:user:{user_id}"


def coalesce_key(handlers: Any, message: TelegramMessage) -> str:
    key = handlers._resolve_topic_key(message.chat_id, message.thread_id)
    return coalesce_key_for_topic(handlers, key, message.from_user_id)


async def buffer_coalesced_message(
    handlers: Any, message: TelegramMessage, text: str
) -> None:
    topic_key = handlers._resolve_topic_key(message.chat_id, message.thread_id)
    key = coalesce_key_for_topic(handlers, topic_key, message.from_user_id)
    lock = handlers._coalesce_locks.setdefault(key, asyncio.Lock())
    async with lock:
        buffer = handlers._coalesced_buffers.get(key)
        if buffer is None:
            buffer = _CoalescedBuffer(
                message=message, parts=[text], topic_key=topic_key
            )
            handlers._coalesced_buffers[key] = buffer
        else:
            buffer.parts.append(text)
        handlers._touch_cache_timestamp("coalesced_buffers", key)
        task = buffer.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        buffer.task = handlers._spawn_task(coalesce_flush_after(handlers, key))


async def coalesce_flush_after(handlers: Any, key: str) -> None:
    try:
        await asyncio.sleep(COALESCE_WINDOW_SECONDS)
    except asyncio.CancelledError:
        return
    try:
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
    await flush_coalesced_key(handlers, coalesce_key(handlers, message))


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
    await handle_message_inner(
        handlers,
        combined_message,
        topic_key=buffer.topic_key,
    )


def build_coalesced_message(buffer: _CoalescedBuffer) -> TelegramMessage:
    combined_text = "\n".join(buffer.parts)
    return dataclasses.replace(buffer.message, text=combined_text, caption=None)


def message_has_media(message: TelegramMessage) -> bool:
    return bool(message.photos or message.document or message.voice or message.audio)


def select_photo(
    photos: Sequence[TelegramPhotoSize],
) -> Optional[TelegramPhotoSize]:
    if not photos:
        return None
    return max(
        photos,
        key=lambda item: ((item.file_size or 0), item.width * item.height),
    )


def document_is_image(document: TelegramDocument) -> bool:
    if document.mime_type:
        base = document.mime_type.lower().split(";", 1)[0].strip()
        if base.startswith("image/"):
            return True
    if document.file_name:
        suffix = Path(document.file_name).suffix.lower()
        if suffix in IMAGE_EXTS:
            return True
    return False


def select_image_candidate(
    message: TelegramMessage,
) -> Optional[TelegramMediaCandidate]:
    photo = select_photo(message.photos)
    if photo:
        return TelegramMediaCandidate(
            kind="photo",
            file_id=photo.file_id,
            file_name=None,
            mime_type=None,
            file_size=photo.file_size,
        )
    if message.document and document_is_image(message.document):
        document = message.document
        return TelegramMediaCandidate(
            kind="document",
            file_id=document.file_id,
            file_name=document.file_name,
            mime_type=document.mime_type,
            file_size=document.file_size,
        )
    return None


def select_voice_candidate(
    message: TelegramMessage,
) -> Optional[TelegramMediaCandidate]:
    if message.voice:
        voice = message.voice
        return TelegramMediaCandidate(
            kind="voice",
            file_id=voice.file_id,
            file_name=None,
            mime_type=voice.mime_type,
            file_size=voice.file_size,
            duration=voice.duration,
        )
    if message.audio:
        audio = message.audio
        return TelegramMediaCandidate(
            kind="audio",
            file_id=audio.file_id,
            file_name=audio.file_name,
            mime_type=audio.mime_type,
            file_size=audio.file_size,
            duration=audio.duration,
        )
    return None


def select_file_candidate(
    message: TelegramMessage,
) -> Optional[TelegramMediaCandidate]:
    if message.document and not document_is_image(message.document):
        document = message.document
        return TelegramMediaCandidate(
            kind="file",
            file_id=document.file_id,
            file_name=document.file_name,
            mime_type=document.mime_type,
            file_size=document.file_size,
        )
    return None


def has_batchable_media(message: TelegramMessage) -> bool:
    return bool(message.photos or message.document)


def media_batch_key(handlers: Any, message: TelegramMessage) -> str:
    topic_key = handlers._resolve_topic_key(message.chat_id, message.thread_id)
    user_id = message.from_user_id
    if message.media_group_id:
        return f"{topic_key}:user:{user_id}:mg:{message.media_group_id}"
    return f"{topic_key}:user:{user_id}:burst"


async def buffer_media_batch(handlers: Any, message: TelegramMessage) -> None:
    if not has_batchable_media(message):
        return
    topic_key = handlers._resolve_topic_key(message.chat_id, message.thread_id)
    key = media_batch_key(handlers, message)
    lock = handlers._media_batch_locks.setdefault(key, asyncio.Lock())
    async with lock:
        buffer = handlers._media_batch_buffers.get(key)
        if buffer is not None and len(buffer.messages) >= MAX_BATCH_ITEMS:
            if buffer.task and buffer.task is not asyncio.current_task():
                buffer.task.cancel()
            handlers._enqueue_topic_work(
                buffer.topic_key,
                lambda msgs=buffer.messages: handlers._handle_media_batch(msgs),
            )
            handlers._media_batch_buffers.pop(key, None)
            buffer = None

        if buffer is None:
            buffer = _MediaBatchBuffer(
                topic_key=topic_key,
                messages=[message],
                media_group_id=message.media_group_id,
                created_at=time.monotonic(),
            )
            handlers._media_batch_buffers[key] = buffer
        else:
            buffer.messages.append(message)

        handlers._touch_cache_timestamp("media_batch_buffers", key)
        task = buffer.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        window_seconds = handlers._config.media.batch_window_seconds
        buffer.task = handlers._spawn_task(
            flush_media_batch_after(handlers, key, window_seconds)
        )


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
        handlers._enqueue_topic_work(
            buffer.topic_key,
            lambda: handlers._handle_media_batch(buffer.messages),
        )


async def handle_media_message(
    handlers: Any, message: TelegramMessage, runtime: Any, caption_text: str
) -> None:
    if not handlers._config.media.enabled:
        await handlers._send_message(
            message.chat_id,
            "Media handling is disabled.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    key = handlers._resolve_topic_key(message.chat_id, message.thread_id)
    record = handlers._router.get_topic(key)
    if record is None or not record.workspace_path:
        await handlers._send_message(
            message.chat_id,
            handlers._with_conversation_id(
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return

    image_candidate = select_image_candidate(message)
    if image_candidate:
        if not handlers._config.media.images:
            await handlers._send_message(
                message.chat_id,
                "Image handling is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await handlers._handle_image_message(
            message, runtime, record, image_candidate, caption_text
        )
        return

    voice_candidate = select_voice_candidate(message)
    if voice_candidate:
        if not handlers._config.media.voice:
            await handlers._send_message(
                message.chat_id,
                "Voice transcription is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await handlers._handle_voice_message(
            message, runtime, record, voice_candidate, caption_text
        )
        return

    file_candidate = select_file_candidate(message)
    if file_candidate:
        if not handlers._config.media.files:
            await handlers._send_message(
                message.chat_id,
                "File handling is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await handlers._handle_file_message(
            message, runtime, record, file_candidate, caption_text
        )
        return

    if caption_text:
        await handlers._handle_normal_message(
            message,
            runtime,
            text_override=caption_text,
            record=record,
        )
        return
    await handlers._send_message(
        message.chat_id,
        "Unsupported media type.",
        thread_id=message.thread_id,
        reply_to=message.message_id,
    )
