from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence, Union

import httpx

from ...core.circuit_breaker import CircuitBreaker
from ...core.exceptions import CodexError, PermanentError, TransientError
from ...core.logging_utils import log_event
from ...core.retry import retry_transient
from .constants import TELEGRAM_CALLBACK_DATA_LIMIT, TELEGRAM_MAX_MESSAGE_LENGTH
from .retry import _extract_retry_after_seconds

_RATE_LIMIT_BUFFER_SECONDS = 0.0

_COMMAND_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")

INTERRUPT_ALIASES = {
    "^c",
    "ctrl-c",
    "ctrl+c",
    "esc",
    "escape",
    "/stop",
}


class TelegramAPIError(CodexError):
    """Raised when the Telegram Bot API returns an error."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: Optional[int] = None,
        user_message: Optional[str] = None,
    ) -> None:
        if user_message is None:
            user_message = "Telegram API error. Retrying with backoff..."
        super().__init__(message, user_message=user_message)
        self.retry_after = retry_after


class TelegramTransientError(TelegramAPIError, TransientError):
    """Retryable Telegram API error."""


class TelegramPermanentError(TelegramAPIError, PermanentError):
    """Non-retryable Telegram API error."""

    recoverable = PermanentError.recoverable
    severity = PermanentError.severity


@dataclass(frozen=True)
class TelegramPhotoSize:
    file_id: str
    file_unique_id: Optional[str]
    width: int
    height: int
    file_size: Optional[int]


@dataclass(frozen=True)
class TelegramDocument:
    file_id: str
    file_unique_id: Optional[str]
    file_name: Optional[str]
    mime_type: Optional[str]
    file_size: Optional[int]


@dataclass(frozen=True)
class TelegramAudio:
    file_id: str
    file_unique_id: Optional[str]
    duration: Optional[int]
    file_name: Optional[str]
    mime_type: Optional[str]
    file_size: Optional[int]


@dataclass(frozen=True)
class TelegramVoice:
    file_id: str
    file_unique_id: Optional[str]
    duration: Optional[int]
    mime_type: Optional[str]
    file_size: Optional[int]


@dataclass(frozen=True)
class TelegramMessageEntity:
    type: str
    offset: int
    length: int


@dataclass(frozen=True)
class TelegramMessage:
    update_id: int
    message_id: int
    chat_id: int
    thread_id: Optional[int]
    from_user_id: Optional[int]
    text: Optional[str]
    date: Optional[int]
    is_topic_message: bool
    is_edited: bool = False
    caption: Optional[str] = None
    entities: tuple[TelegramMessageEntity, ...] = field(default_factory=tuple)
    caption_entities: tuple[TelegramMessageEntity, ...] = field(default_factory=tuple)
    photos: tuple[TelegramPhotoSize, ...] = field(default_factory=tuple)
    document: Optional[TelegramDocument] = None
    audio: Optional[TelegramAudio] = None
    voice: Optional[TelegramVoice] = None
    media_group_id: Optional[str] = None


@dataclass(frozen=True)
class TelegramCallbackQuery:
    update_id: int
    callback_id: str
    from_user_id: Optional[int]
    data: Optional[str]
    message_id: Optional[int]
    chat_id: Optional[int]
    thread_id: Optional[int]


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    message: Optional[TelegramMessage]
    callback: Optional[TelegramCallbackQuery]


@dataclass(frozen=True)
class TelegramCommand:
    name: str
    args: str
    raw: str


@dataclass(frozen=True)
class TelegramAllowlist:
    allowed_chat_ids: set[int]
    allowed_user_ids: set[int]
    require_topic: bool = False


@dataclass(frozen=True)
class InlineButton:
    text: str
    callback_data: str


@dataclass(frozen=True)
class ApprovalCallback:
    decision: str
    request_id: str


@dataclass(frozen=True)
class QuestionOptionCallback:
    request_id: str
    question_index: int
    option_index: int


@dataclass(frozen=True)
class QuestionDoneCallback:
    request_id: str


@dataclass(frozen=True)
class QuestionCustomCallback:
    request_id: str


@dataclass(frozen=True)
class QuestionCancelCallback:
    request_id: str


@dataclass(frozen=True)
class ResumeCallback:
    thread_id: str


@dataclass(frozen=True)
class BindCallback:
    repo_id: str


@dataclass(frozen=True)
class AgentCallback:
    agent: str


@dataclass(frozen=True)
class ModelCallback:
    model_id: str


@dataclass(frozen=True)
class EffortCallback:
    effort: str


@dataclass(frozen=True)
class UpdateCallback:
    target: str


@dataclass(frozen=True)
class UpdateConfirmCallback:
    decision: str


@dataclass(frozen=True)
class ReviewCommitCallback:
    sha: str


@dataclass(frozen=True)
class CancelCallback:
    kind: str


@dataclass(frozen=True)
class CompactCallback:
    action: str


@dataclass(frozen=True)
class PageCallback:
    kind: str
    page: int


def parse_command(
    text: Optional[str],
    *,
    entities: Optional[Sequence[TelegramMessageEntity]] = None,
    bot_username: Optional[str] = None,
) -> Optional[TelegramCommand]:
    if not text:
        return None

    # Primary path: use entities if available
    if entities:
        command_entity = next(
            (
                entity
                for entity in entities
                if entity.type == "bot_command" and entity.offset == 0
            ),
            None,
        )
        if command_entity is not None:
            if command_entity.length <= 1:
                return None
            if command_entity.length > len(text):
                return None
            command_text = text[: command_entity.length]
            if not command_text.startswith("/"):
                return None
            command = command_text.lstrip("/")
            tail = text[command_entity.length :].strip()
            if not command:
                return None
            if "@" in command:
                name, _, target = command.partition("@")
                if bot_username and target.lower() != bot_username.lower():
                    return None
                command = name
            return TelegramCommand(
                name=command.lower(), args=tail.strip(), raw=text.strip()
            )
        return None

    # Fallback: simple text-based parsing when entities are missing
    # Validates against command name regex (^[a-z0-9_]{1,32}$) to avoid false positives
    trimmed = text.strip()
    if trimmed.startswith("/"):
        parts = trimmed.split(None, 1)
        command = parts[0].lstrip("/")
        if not command:
            return None
        if "@" in command:
            name, _, target = command.partition("@")
            if bot_username and target.lower() != bot_username.lower():
                return None
            command = name
        if not command:
            return None
        if not _COMMAND_NAME_RE.fullmatch(command):
            return None
        args = parts[1].strip() if len(parts) > 1 else ""
        return TelegramCommand(name=command.lower(), args=args, raw=trimmed)

    return None


def is_interrupt_alias(text: Optional[str]) -> bool:
    if not text:
        return False
    normalized = text.strip().lower()
    if normalized in INTERRUPT_ALIASES:
        return True
    return normalized == "/interrupt"


def parse_update(update: dict[str, Any]) -> Optional[TelegramUpdate]:
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return None
    message = _parse_message(update_id, update.get("message"), edited=False)
    if message is None:
        message = _parse_message(update_id, update.get("edited_message"), edited=True)
    callback = _parse_callback(update_id, update.get("callback_query"))
    if message is None and callback is None:
        return None
    return TelegramUpdate(update_id=update_id, message=message, callback=callback)


def _parse_message(
    update_id: int, payload: Any, *, edited: bool = False
) -> Optional[TelegramMessage]:
    if not isinstance(payload, dict):
        return None
    message_id = payload.get("message_id")
    chat = payload.get("chat")
    if not isinstance(message_id, int) or not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return None
    thread_id = payload.get("message_thread_id")
    if thread_id is not None and not isinstance(thread_id, int):
        thread_id = None
    sender = payload.get("from")
    from_user_id = sender.get("id") if isinstance(sender, dict) else None
    if from_user_id is not None and not isinstance(from_user_id, int):
        from_user_id = None
    text = payload.get("text")
    if text is not None and not isinstance(text, str):
        text = None
    caption = payload.get("caption")
    if caption is not None and not isinstance(caption, str):
        caption = None
    entities = _parse_entities(payload.get("entities"))
    caption_entities = _parse_entities(payload.get("caption_entities"))
    photos = _parse_photo_sizes(payload.get("photo"))
    document = _parse_document(payload.get("document"))
    audio = _parse_audio(payload.get("audio"))
    voice = _parse_voice(payload.get("voice"))
    media_group_id = payload.get("media_group_id")
    if media_group_id is not None and not isinstance(media_group_id, str):
        media_group_id = None
    date = payload.get("date")
    if date is not None and not isinstance(date, int):
        date = None
    is_topic_message = bool(payload.get("is_topic_message"))
    return TelegramMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        thread_id=thread_id,
        from_user_id=from_user_id,
        text=text,
        date=date,
        is_topic_message=is_topic_message,
        is_edited=edited,
        caption=caption,
        entities=entities,
        caption_entities=caption_entities,
        photos=photos,
        document=document,
        audio=audio,
        voice=voice,
        media_group_id=media_group_id,
    )


def _parse_callback(update_id: int, payload: Any) -> Optional[TelegramCallbackQuery]:
    if not isinstance(payload, dict):
        return None
    callback_id = payload.get("id")
    if not isinstance(callback_id, str):
        return None
    sender = payload.get("from")
    from_user_id = sender.get("id") if isinstance(sender, dict) else None
    if from_user_id is not None and not isinstance(from_user_id, int):
        from_user_id = None
    data = payload.get("data")
    if data is not None and not isinstance(data, str):
        data = None
    message = payload.get("message")
    message_id = None
    chat_id = None
    thread_id = None
    if isinstance(message, dict):
        message_id = message.get("message_id")
        chat = message.get("chat")
        if isinstance(chat, dict):
            chat_id = chat.get("id")
        thread_id = message.get("message_thread_id")
    if message_id is not None and not isinstance(message_id, int):
        message_id = None
    if chat_id is not None and not isinstance(chat_id, int):
        chat_id = None
    if thread_id is not None and not isinstance(thread_id, int):
        thread_id = None
    return TelegramCallbackQuery(
        update_id=update_id,
        callback_id=callback_id,
        from_user_id=from_user_id,
        data=data,
        message_id=message_id,
        chat_id=chat_id,
        thread_id=thread_id,
    )


def _parse_photo_sizes(payload: Any) -> tuple[TelegramPhotoSize, ...]:
    if not isinstance(payload, list):
        return ()
    sizes: list[TelegramPhotoSize] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        file_id = item.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            continue
        file_unique_id = item.get("file_unique_id")
        if file_unique_id is not None and not isinstance(file_unique_id, str):
            file_unique_id = None
        width = item.get("width")
        height = item.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            continue
        file_size = item.get("file_size")
        if file_size is not None and not isinstance(file_size, int):
            file_size = None
        sizes.append(
            TelegramPhotoSize(
                file_id=file_id,
                file_unique_id=file_unique_id,
                width=width,
                height=height,
                file_size=file_size,
            )
        )
    return tuple(sizes)


def _parse_document(payload: Any) -> Optional[TelegramDocument]:
    if not isinstance(payload, dict):
        return None
    file_id = payload.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    file_unique_id = payload.get("file_unique_id")
    if file_unique_id is not None and not isinstance(file_unique_id, str):
        file_unique_id = None
    file_name = payload.get("file_name")
    if file_name is not None and not isinstance(file_name, str):
        file_name = None
    mime_type = payload.get("mime_type")
    if mime_type is not None and not isinstance(mime_type, str):
        mime_type = None
    file_size = payload.get("file_size")
    if file_size is not None and not isinstance(file_size, int):
        file_size = None
    return TelegramDocument(
        file_id=file_id,
        file_unique_id=file_unique_id,
        file_name=file_name,
        mime_type=mime_type,
        file_size=file_size,
    )


def _parse_audio(payload: Any) -> Optional[TelegramAudio]:
    if not isinstance(payload, dict):
        return None
    file_id = payload.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    file_unique_id = payload.get("file_unique_id")
    if file_unique_id is not None and not isinstance(file_unique_id, str):
        file_unique_id = None
    duration = payload.get("duration")
    if duration is not None and not isinstance(duration, int):
        duration = None
    file_name = payload.get("file_name")
    if file_name is not None and not isinstance(file_name, str):
        file_name = None
    mime_type = payload.get("mime_type")
    if mime_type is not None and not isinstance(mime_type, str):
        mime_type = None
    file_size = payload.get("file_size")
    if file_size is not None and not isinstance(file_size, int):
        file_size = None
    return TelegramAudio(
        file_id=file_id,
        file_unique_id=file_unique_id,
        duration=duration,
        file_name=file_name,
        mime_type=mime_type,
        file_size=file_size,
    )


def _parse_voice(payload: Any) -> Optional[TelegramVoice]:
    if not isinstance(payload, dict):
        return None
    file_id = payload.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    file_unique_id = payload.get("file_unique_id")
    if file_unique_id is not None and not isinstance(file_unique_id, str):
        file_unique_id = None
    duration = payload.get("duration")
    if duration is not None and not isinstance(duration, int):
        duration = None
    mime_type = payload.get("mime_type")
    if mime_type is not None and not isinstance(mime_type, str):
        mime_type = None
    file_size = payload.get("file_size")
    if file_size is not None and not isinstance(file_size, int):
        file_size = None
    return TelegramVoice(
        file_id=file_id,
        file_unique_id=file_unique_id,
        duration=duration,
        mime_type=mime_type,
        file_size=file_size,
    )


def _parse_entities(payload: Any) -> tuple[TelegramMessageEntity, ...]:
    if not isinstance(payload, list):
        return ()
    entities: list[TelegramMessageEntity] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        offset = item.get("offset")
        length = item.get("length")
        if not isinstance(kind, str):
            continue
        if not isinstance(offset, int) or not isinstance(length, int):
            continue
        entities.append(TelegramMessageEntity(type=kind, offset=offset, length=length))
    return tuple(entities)


def allowlist_allows(update: TelegramUpdate, allowlist: TelegramAllowlist) -> bool:
    if not allowlist.allowed_chat_ids or not allowlist.allowed_user_ids:
        return False
    chat_id = None
    user_id = None
    thread_id = None
    if update.message:
        chat_id = update.message.chat_id
        user_id = update.message.from_user_id
        thread_id = update.message.thread_id
    elif update.callback:
        chat_id = update.callback.chat_id
        user_id = update.callback.from_user_id
        thread_id = update.callback.thread_id
    if chat_id is None or user_id is None:
        return False
    if chat_id not in allowlist.allowed_chat_ids:
        return False
    if user_id not in allowlist.allowed_user_ids:
        return False
    if allowlist.require_topic and thread_id is None:
        return False
    return True


def chunk_message(
    text: Optional[str],
    *,
    max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH,
    with_numbering: bool = True,
) -> list[str]:
    if not text:
        return []
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    if len(text) <= max_len:
        return [text]
    parts = _split_text(text, max_len)
    if not with_numbering or len(parts) == 1:
        return parts
    parts = _apply_numbering(text, max_len)
    return parts


def _apply_numbering(text: str, max_len: int) -> list[str]:
    parts = _split_text(text, max_len)
    total = len(parts)
    while True:
        prefix_len = len(_part_prefix(total, total))
        allowed = max_len - prefix_len
        if allowed <= 0:
            raise ValueError("max_len too small for numbering")
        parts = _split_text(text, allowed)
        new_total = len(parts)
        if new_total == total:
            break
        total = new_total
    return [f"{_part_prefix(idx, total)}{chunk}" for idx, chunk in enumerate(parts, 1)]


def _part_prefix(index: int, total: int) -> str:
    return f"Part {index}/{total}\n"


def _split_text(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut == -1:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunk = remaining[:cut]
        remaining = remaining[cut:]
        parts.append(chunk)
    return parts


def build_inline_keyboard(
    rows: Sequence[Sequence[InlineButton]],
) -> dict[str, Any]:
    keyboard: list[list[dict[str, str]]] = []
    for row in rows:
        keyboard.append(
            [
                {"text": button.text, "callback_data": button.callback_data}
                for button in row
            ]
        )
    return {"inline_keyboard": keyboard}


def encode_approval_callback(decision: str, request_id: str) -> str:
    data = f"appr:{decision}:{request_id}"
    _validate_callback_data(data)
    return data


def encode_question_option_callback(
    request_id: str, question_index: int, option_index: int
) -> str:
    data = f"qopt:{question_index}:{option_index}:{request_id}"
    _validate_callback_data(data)
    return data


def encode_question_done_callback(request_id: str) -> str:
    data = f"qdone:{request_id}"
    _validate_callback_data(data)
    return data


def encode_question_custom_callback(request_id: str) -> str:
    data = f"qcustom:{request_id}"
    _validate_callback_data(data)
    return data


def encode_question_cancel_callback(request_id: str) -> str:
    data = f"qcancel:{request_id}"
    _validate_callback_data(data)
    return data


def encode_resume_callback(thread_id: str) -> str:
    data = f"resume:{thread_id}"
    _validate_callback_data(data)
    return data


def encode_bind_callback(repo_id: str) -> str:
    data = f"bind:{repo_id}"
    _validate_callback_data(data)
    return data


def encode_agent_callback(agent: str) -> str:
    data = f"agent:{agent}"
    _validate_callback_data(data)
    return data


def encode_model_callback(model_id: str) -> str:
    data = f"model:{model_id}"
    _validate_callback_data(data)
    return data


def encode_effort_callback(effort: str) -> str:
    data = f"effort:{effort}"
    _validate_callback_data(data)
    return data


def encode_update_callback(target: str) -> str:
    data = f"update:{target}"
    _validate_callback_data(data)
    return data


def encode_update_confirm_callback(decision: str) -> str:
    data = f"update_confirm:{decision}"
    _validate_callback_data(data)
    return data


def encode_review_commit_callback(sha: str) -> str:
    data = f"review_commit:{sha}"
    _validate_callback_data(data)
    return data


def encode_cancel_callback(kind: str) -> str:
    data = f"cancel:{kind}"
    _validate_callback_data(data)
    return data


def encode_page_callback(kind: str, page: int) -> str:
    data = f"page:{kind}:{page}"
    _validate_callback_data(data)
    return data


def encode_compact_callback(action: str) -> str:
    data = f"compact:{action}"
    _validate_callback_data(data)
    return data


def parse_callback_data(
    data: Optional[str],
) -> Optional[
    Union[
        ApprovalCallback,
        QuestionOptionCallback,
        QuestionDoneCallback,
        QuestionCustomCallback,
        QuestionCancelCallback,
        ResumeCallback,
        BindCallback,
        AgentCallback,
        ModelCallback,
        EffortCallback,
        UpdateCallback,
        UpdateConfirmCallback,
        ReviewCommitCallback,
        CancelCallback,
        CompactCallback,
        PageCallback,
    ]
]:
    if not data:
        return None
    if data.startswith("appr:"):
        _, _, rest = data.partition(":")
        decision, sep, request_id = rest.partition(":")
        if not decision or not sep or not request_id:
            return None
        return ApprovalCallback(decision=decision, request_id=request_id)
    if data.startswith("qopt:"):
        _, _, rest = data.partition(":")
        question_raw, sep, rest = rest.partition(":")
        option_raw, sep2, request_id = rest.partition(":")
        if not question_raw or not sep or not option_raw or not sep2 or not request_id:
            return None
        if not question_raw.isdigit() or not option_raw.isdigit():
            return None
        return QuestionOptionCallback(
            request_id=request_id,
            question_index=int(question_raw),
            option_index=int(option_raw),
        )
    if data.startswith("qdone:"):
        _, _, request_id = data.partition(":")
        if not request_id:
            return None
        return QuestionDoneCallback(request_id=request_id)
    if data.startswith("qcustom:"):
        _, _, request_id = data.partition(":")
        if not request_id:
            return None
        return QuestionCustomCallback(request_id=request_id)
    if data.startswith("qcancel:"):
        _, _, request_id = data.partition(":")
        if not request_id:
            return None
        return QuestionCancelCallback(request_id=request_id)
    if data.startswith("resume:"):
        _, _, thread_id = data.partition(":")
        if not thread_id:
            return None
        return ResumeCallback(thread_id=thread_id)
    if data.startswith("bind:"):
        _, _, repo_id = data.partition(":")
        if not repo_id:
            return None
        return BindCallback(repo_id=repo_id)
    if data.startswith("agent:"):
        _, _, agent = data.partition(":")
        if not agent:
            return None
        return AgentCallback(agent=agent)
    if data.startswith("model:"):
        _, _, model_id = data.partition(":")
        if not model_id:
            return None
        return ModelCallback(model_id=model_id)
    if data.startswith("effort:"):
        _, _, effort = data.partition(":")
        if not effort:
            return None
        return EffortCallback(effort=effort)
    if data.startswith("update:"):
        _, _, target = data.partition(":")
        if not target:
            return None
        return UpdateCallback(target=target)
    if data.startswith("update_confirm:"):
        _, _, decision = data.partition(":")
        if not decision:
            return None
        return UpdateConfirmCallback(decision=decision)
    if data.startswith("review_commit:"):
        _, _, sha = data.partition(":")
        if not sha:
            return None
        return ReviewCommitCallback(sha=sha)
    if data.startswith("cancel:"):
        _, _, kind = data.partition(":")
        if not kind:
            return None
        return CancelCallback(kind=kind)
    if data.startswith("compact:"):
        _, _, action = data.partition(":")
        if not action:
            return None
        return CompactCallback(action=action)
    if data.startswith("page:"):
        _, _, rest = data.partition(":")
        kind, sep, page = rest.partition(":")
        if not kind or not sep or not page:
            return None
        if not page.isdigit():
            return None
        return PageCallback(kind=kind, page=int(page))
    return None


def build_approval_keyboard(
    request_id: str, *, include_session: bool = False
) -> dict[str, Any]:
    rows: list[list[InlineButton]] = [
        [
            InlineButton("Accept", encode_approval_callback("accept", request_id)),
            InlineButton("Decline", encode_approval_callback("decline", request_id)),
        ],
        [InlineButton("Cancel", encode_approval_callback("cancel", request_id))],
    ]
    if include_session:
        rows[0].insert(
            1,
            InlineButton(
                "Accept session", encode_approval_callback("accept_session", request_id)
            ),
        )
    return build_inline_keyboard(rows)


def build_question_keyboard(
    request_id: str,
    *,
    question_index: int,
    options: Sequence[str],
    multiple: bool = False,
    custom: bool = True,
    selected_indices: set[int] | None = None,
    include_cancel: bool = True,
) -> dict[str, Any]:
    selected = selected_indices or set()
    rows = []
    for index, label in enumerate(options):
        if multiple and index in selected:
            display_label = f"✓ {label}"
        else:
            display_label = label
        rows.append(
            [
                InlineButton(
                    display_label,
                    encode_question_option_callback(request_id, question_index, index),
                )
            ]
        )
    if custom:
        rows.append(
            [
                InlineButton(
                    "Type your own answer", encode_question_custom_callback(request_id)
                )
            ]
        )
    if multiple:
        rows.append([InlineButton("Done", encode_question_done_callback(request_id))])
    if include_cancel:
        rows.append(
            [InlineButton("Cancel", encode_question_cancel_callback(request_id))]
        )
    return build_inline_keyboard(rows)


def build_resume_keyboard(
    options: Sequence[tuple[str, str]],
    *,
    page_button: Optional[tuple[str, str]] = None,
    include_cancel: bool = False,
) -> dict[str, Any]:
    rows = [
        [InlineButton(label, encode_resume_callback(thread_id))]
        for thread_id, label in options
    ]
    if page_button:
        label, callback_data = page_button
        rows.append([InlineButton(label, callback_data)])
    if include_cancel:
        rows.append([InlineButton("Cancel", encode_cancel_callback("resume"))])
    return build_inline_keyboard(rows)


def build_agent_keyboard(
    options: Sequence[tuple[str, str]],
    *,
    page_button: Optional[tuple[str, str]] = None,
    include_cancel: bool = False,
) -> dict[str, Any]:
    rows = [
        [InlineButton(label, encode_agent_callback(agent))] for agent, label in options
    ]
    if page_button:
        label, callback_data = page_button
        rows.append([InlineButton(label, callback_data)])
    if include_cancel:
        rows.append([InlineButton("Cancel", encode_cancel_callback("agent"))])
    return build_inline_keyboard(rows)


def build_model_keyboard(
    options: Sequence[tuple[str, str]],
    *,
    page_button: Optional[tuple[str, str]] = None,
    include_cancel: bool = False,
) -> dict[str, Any]:
    rows = [
        [InlineButton(label, encode_model_callback(model_id))]
        for model_id, label in options
    ]
    if page_button:
        label, callback_data = page_button
        rows.append([InlineButton(label, callback_data)])
    if include_cancel:
        rows.append([InlineButton("Cancel", encode_cancel_callback("model"))])
    return build_inline_keyboard(rows)


def build_compact_keyboard() -> dict[str, Any]:
    rows = [
        [
            InlineButton(
                "Start new thread with this summary",
                encode_compact_callback("apply"),
            )
        ],
        [InlineButton("Cancel", encode_compact_callback("cancel"))],
    ]
    return build_inline_keyboard(rows)


def build_effort_keyboard(
    options: Sequence[tuple[str, str]],
    *,
    include_cancel: bool = False,
) -> dict[str, Any]:
    rows = [
        [InlineButton(label, encode_effort_callback(effort))]
        for effort, label in options
    ]
    if include_cancel:
        rows.append([InlineButton("Cancel", encode_cancel_callback("model"))])
    return build_inline_keyboard(rows)


def build_update_keyboard(
    options: Sequence[tuple[str, str]],
    *,
    include_cancel: bool = False,
) -> dict[str, Any]:
    rows = [
        [InlineButton(label, encode_update_callback(target))]
        for target, label in options
    ]
    if include_cancel:
        rows.append([InlineButton("Cancel", encode_cancel_callback("update"))])
    return build_inline_keyboard(rows)


def build_update_confirm_keyboard() -> dict[str, Any]:
    rows = [
        [
            InlineButton("Yes, continue", encode_update_confirm_callback("yes")),
            InlineButton("Cancel", encode_cancel_callback("update-confirm")),
        ]
    ]
    return build_inline_keyboard(rows)


def build_review_commit_keyboard(
    options: Sequence[tuple[str, str]],
    *,
    page_button: Optional[tuple[str, str]] = None,
    include_cancel: bool = False,
) -> dict[str, Any]:
    rows = [
        [InlineButton(label, encode_review_commit_callback(sha))]
        for sha, label in options
    ]
    if page_button:
        label, callback_data = page_button
        rows.append([InlineButton(label, callback_data)])
    if include_cancel:
        rows.append([InlineButton("Cancel", encode_cancel_callback("review-commit"))])
    return build_inline_keyboard(rows)


def build_bind_keyboard(
    options: Sequence[tuple[str, str]],
    *,
    page_button: Optional[tuple[str, str]] = None,
    include_cancel: bool = False,
) -> dict[str, Any]:
    rows = [
        [InlineButton(label, encode_bind_callback(repo_id))]
        for repo_id, label in options
    ]
    if page_button:
        label, callback_data = page_button
        rows.append([InlineButton(label, callback_data)])
    if include_cancel:
        rows.append([InlineButton("Cancel", encode_cancel_callback("bind"))])
    return build_inline_keyboard(rows)


def _validate_callback_data(data: str) -> None:
    if len(data.encode("utf-8")) > TELEGRAM_CALLBACK_DATA_LIMIT:
        raise ValueError("callback_data exceeds Telegram limit")


def next_update_offset(
    updates: Iterable[dict[str, Any]], current: Optional[int]
) -> Optional[int]:
    max_update_id = None
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            if max_update_id is None or update_id > max_update_id:
                max_update_id = update_id
    if max_update_id is None:
        return current
    return max_update_id + 1


class TelegramBotClient:
    def __init__(
        self,
        bot_token: str,
        *,
        timeout_seconds: float = 30.0,
        logger: Optional[logging.Logger] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._bot_token = bot_token
        self._base_url = "https://api.telegram.org"
        self._file_base_url = f"https://api.telegram.org/file/bot{bot_token}"
        self._logger = logger or logging.getLogger(__name__)
        if client is None:
            self._client = httpx.AsyncClient(timeout=timeout_seconds)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        self._rate_limit_until: Optional[float] = None
        self._rate_limit_lock: Optional[asyncio.Lock] = None
        self._rate_limit_lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._circuit_breaker = CircuitBreaker("Telegram", logger=self._logger)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "TelegramBotClient":
        return self

    async def __aexit__(self, *_exc_info) -> None:
        await self.close()

    async def get_updates(
        self,
        *,
        offset: Optional[int] = None,
        timeout: int = 30,
        allowed_updates: Optional[Sequence[str]] = None,
    ) -> list[dict[str, Any]]:
        log_event(
            self._logger,
            logging.DEBUG,
            "telegram.request",
            method="getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=list(allowed_updates) if allowed_updates else None,
        )
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates:
            payload["allowed_updates"] = list(allowed_updates)
        result = await self._request("getUpdates", payload)
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    async def send_message(
        self,
        chat_id: Union[int, str],
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            responses = await self.send_message_chunks(
                chat_id,
                text,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            return responses[0] if responses else {}
        return await self._send_message_raw(
            chat_id,
            text,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )

    async def _send_message_raw(
        self,
        chat_id: Union[int, str],
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.send_message",
            chat_id=chat_id,
            thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            text_len=len(text),
            has_markup=reply_markup is not None,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        result = await self._request("sendMessage", payload)
        return result if isinstance(result, dict) else {}

    async def send_document(
        self,
        chat_id: Union[int, str],
        document: bytes,
        *,
        filename: str,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.send_document",
            chat_id=chat_id,
            thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            filename=filename,
            bytes_len=len(document),
            parse_mode=parse_mode,
        )
        data: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id is not None:
            data["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = reply_to_message_id
        if caption is not None:
            data["caption"] = caption
        if parse_mode is not None:
            data["parse_mode"] = parse_mode
        files = {"document": (filename, document, "text/plain")}
        result = await self._request_multipart("sendDocument", data, files)
        return result if isinstance(result, dict) else {}

    async def get_me(self) -> dict[str, Any]:
        log_event(self._logger, logging.DEBUG, "telegram.request", method="getMe")
        result = await self._request("getMe", {})
        return result if isinstance(result, dict) else {}

    async def get_file(self, file_id: str) -> dict[str, Any]:
        log_event(self._logger, logging.DEBUG, "telegram.request", method="getFile")
        result = await self._request("getFile", {"file_id": file_id})
        return result if isinstance(result, dict) else {}

    async def get_my_commands(
        self,
        *,
        scope: Optional[dict[str, Any]] = None,
        language_code: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        log_event(
            self._logger,
            logging.DEBUG,
            "telegram.request",
            method="getMyCommands",
            scope=scope,
            language_code=language_code,
        )
        payload: dict[str, Any] = {}
        if scope is not None:
            payload["scope"] = scope
        if language_code is not None:
            payload["language_code"] = language_code
        result = await self._request("getMyCommands", payload)
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    async def set_my_commands(
        self,
        commands: Sequence[dict[str, str]],
        *,
        scope: Optional[dict[str, Any]] = None,
        language_code: Optional[str] = None,
    ) -> bool:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.set_my_commands",
            command_count=len(commands),
            scope=scope,
            language_code=language_code,
        )
        payload: dict[str, Any] = {"commands": list(commands)}
        if scope is not None:
            payload["scope"] = scope
        if language_code is not None:
            payload["language_code"] = language_code
        result = await self._request("setMyCommands", payload)
        return bool(result) if isinstance(result, bool) else False

    async def download_file(
        self, file_path: str, max_size_bytes: int = 50 * 1024 * 1024
    ) -> bytes:
        safe_path = file_path.lstrip("/")
        url = f"{self._file_base_url}/{safe_path}"
        log_event(
            self._logger, logging.INFO, "telegram.file.download", file_path=file_path
        )
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    file_size = int(content_length)
                    if file_size > max_size_bytes:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.file.too_large",
                            file_path=file_path,
                            size=file_size,
                            max_size=max_size_bytes,
                        )
                        raise TelegramPermanentError(
                            f"File too large: {file_size} bytes (max {max_size_bytes})",
                            user_message="Telegram file too large.",
                        )
                except ValueError:
                    pass
            if len(response.content) > max_size_bytes:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.file.too_large",
                    file_path=file_path,
                    size=len(response.content),
                    max_size=max_size_bytes,
                )
                raise TelegramPermanentError(
                    f"File too large: {len(response.content)} bytes (max {max_size_bytes})",
                    user_message="Telegram file too large.",
                )
            return response.content
        except TelegramAPIError:
            raise
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.file.download_failed",
                file_path=file_path,
                exc=exc,
            )
            raise TelegramTransientError(
                "Telegram file download failed",
                user_message="Telegram file download failed. Retrying...",
            ) from exc

    async def send_message_chunks(
        self,
        chat_id: Union[int, str],
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
        max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH,
    ) -> list[dict[str, Any]]:
        chunks = chunk_message(text, max_len=max_len, with_numbering=True)
        if not chunks:
            return []
        responses: list[dict[str, Any]] = []
        log_event(
            self._logger,
            logging.INFO,
            "telegram.send_message.chunks",
            chat_id=chat_id,
            thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            parts=len(chunks),
            total_len=len(text),
        )
        for idx, chunk in enumerate(chunks):
            response = await self._send_message_raw(
                chat_id,
                chunk,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id if idx == 0 else None,
                reply_markup=reply_markup if idx == 0 else None,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            responses.append(response)
        return responses

    async def edit_message_text(
        self,
        chat_id: Union[int, str],
        message_id: int,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.edit_message",
            chat_id=chat_id,
            message_id=message_id,
            text_len=len(text),
            has_markup=reply_markup is not None,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        result = await self._request("editMessageText", payload)
        return result if isinstance(result, dict) else {}

    async def delete_message(
        self,
        chat_id: Union[int, str],
        message_id: int,
    ) -> bool:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.delete_message",
            chat_id=chat_id,
            message_id=message_id,
        )
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        result = await self._request("deleteMessage", payload)
        return bool(result) if isinstance(result, bool) else False

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.answer_callback",
            callback_query_id=callback_query_id,
            text_len=len(text) if text else 0,
            show_alert=show_alert,
        )
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        result = await self._request("answerCallbackQuery", payload)
        return result if isinstance(result, dict) else {}

    async def _request(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base_url}/bot{self._bot_token}/{method}"

        async def send() -> httpx.Response:
            return await self._client.post(url, json=payload)

        return await self._request_with_retry(method, send)

    async def _request_multipart(
        self, method: str, data: dict[str, Any], files: dict[str, Any]
    ) -> Any:
        url = f"{self._base_url}/bot{self._bot_token}/{method}"

        async def send() -> httpx.Response:
            return await self._client.post(url, data=data, files=files)

        return await self._request_with_retry(method, send)

    @retry_transient(max_attempts=5, base_wait=1.0, max_wait=60.0)
    async def _request_with_retry(
        self, method: str, send: Callable[[], Awaitable[httpx.Response]]
    ) -> Any:
        async with self._circuit_breaker.call():
            await self._wait_for_rate_limit(method)
            try:
                response = await send()
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                retry_after = _extract_retry_after_seconds(exc)
                status_code = exc.response.status_code
                if retry_after is not None:
                    await self._apply_rate_limit(method, retry_after)
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.request.failed",
                    method=method,
                    status_code=status_code,
                    retry_after=retry_after,
                    exc=exc,
                )
                if status_code == 429 or retry_after is not None or status_code >= 500:
                    raise TelegramTransientError(
                        "Telegram request failed",
                        retry_after=retry_after,
                    ) from exc
                raise TelegramPermanentError(
                    "Telegram request failed",
                    user_message="Telegram API error.",
                ) from exc
            except httpx.RequestError as exc:
                retry_after = _extract_retry_after_seconds(exc)
                if retry_after is not None:
                    await self._apply_rate_limit(method, retry_after)
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.request.failed",
                    method=method,
                    retry_after=retry_after,
                    exc=exc,
                )
                raise TelegramTransientError(
                    "Telegram request failed",
                    retry_after=retry_after,
                ) from exc
            except Exception as exc:
                retry_after = _extract_retry_after_seconds(exc)
                if retry_after is not None:
                    await self._apply_rate_limit(method, retry_after)
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.request.failed",
                    method=method,
                    retry_after=retry_after,
                    exc=exc,
                )
                raise TelegramTransientError(
                    "Telegram request failed",
                    retry_after=retry_after,
                ) from exc
            if not isinstance(payload, dict) or not payload.get("ok"):
                description = (
                    payload.get("description") if isinstance(payload, dict) else None
                )
                retry_after = self._retry_after_from_payload(payload)
                if isinstance(payload, dict):
                    error_code = payload.get("error_code")
                else:
                    error_code = None
                if not isinstance(error_code, int) or isinstance(error_code, bool):
                    error_code = None
                if retry_after is not None:
                    await self._apply_rate_limit(method, retry_after)
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.request.failed",
                    method=method,
                    error_code=error_code,
                    retry_after=retry_after,
                    description=description,
                )
                if (
                    error_code == 429
                    or retry_after is not None
                    or (error_code is not None and error_code >= 500)
                ):
                    raise TelegramTransientError(
                        description or "Telegram API returned error",
                        retry_after=retry_after,
                    )
                if error_code is not None and 400 <= error_code < 500:
                    raise TelegramPermanentError(
                        description or "Telegram API returned error",
                        user_message="Telegram API error.",
                    )
                raise TelegramTransientError(
                    description or "Telegram API returned error",
                    retry_after=retry_after,
                )
            return payload.get("result")

    def _retry_after_from_payload(self, payload: Any) -> Optional[int]:
        if not isinstance(payload, dict):
            return None
        parameters = payload.get("parameters")
        if isinstance(parameters, dict):
            retry_after = parameters.get("retry_after")
            if isinstance(retry_after, int):
                return retry_after
        description = payload.get("description")
        if isinstance(description, str) and description:
            return _extract_retry_after_seconds(Exception(description))
        return None

    def _ensure_rate_limit_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._rate_limit_lock
        lock_loop = self._rate_limit_lock_loop
        if (
            lock is None
            or lock_loop is None
            or lock_loop is not loop
            or lock_loop.is_closed()
        ):
            lock = asyncio.Lock()
            self._rate_limit_lock = lock
            self._rate_limit_lock_loop = loop
            self._rate_limit_until = None
        return lock

    async def _apply_rate_limit(self, method: str, retry_after: int) -> None:
        delay = float(retry_after)
        loop = asyncio.get_running_loop()
        until = loop.time() + delay + _RATE_LIMIT_BUFFER_SECONDS
        lock = self._ensure_rate_limit_lock()
        async with lock:
            if self._rate_limit_until is None or until > self._rate_limit_until:
                self._rate_limit_until = until
        log_event(
            self._logger,
            logging.INFO,
            "telegram.rate_limit.hit",
            method=method,
            retry_after=retry_after,
        )
        await self._wait_for_rate_limit(method, min_delay=delay)

    async def _wait_for_rate_limit(
        self, method: str, min_delay: Optional[float] = None
    ) -> None:
        lock = self._ensure_rate_limit_lock()
        async with lock:
            until = self._rate_limit_until
        if until is None:
            return
        loop = asyncio.get_running_loop()
        delay = until - loop.time()
        if min_delay is not None and delay < min_delay:
            delay = min_delay
        if delay <= 0:
            async with lock:
                if self._rate_limit_until == until:
                    self._rate_limit_until = None
            return
        log_event(
            self._logger,
            logging.INFO,
            "telegram.rate_limit.wait",
            method=method,
            delay_seconds=delay,
        )
        await asyncio.sleep(delay)
        async with lock:
            if self._rate_limit_until == until:
                self._rate_limit_until = None


class TelegramUpdatePoller:
    def __init__(
        self,
        client: TelegramBotClient,
        *,
        allowed_updates: Optional[Sequence[str]] = None,
        offset: Optional[int] = None,
    ) -> None:
        self._client = client
        self._offset: Optional[int] = None
        self._allowed_updates = list(allowed_updates) if allowed_updates else None
        if isinstance(offset, int) and not isinstance(offset, bool):
            self._offset = offset

    @property
    def offset(self) -> Optional[int]:
        return self._offset

    def set_offset(self, offset: Optional[int]) -> None:
        if offset is None:
            return
        if not isinstance(offset, int) or isinstance(offset, bool):
            return
        self._offset = offset

    async def poll(self, *, timeout: int = 30) -> list[TelegramUpdate]:
        updates = await self._client.get_updates(
            offset=self._offset,
            timeout=timeout,
            allowed_updates=self._allowed_updates,
        )
        self._offset = next_update_offset(updates, self._offset)
        parsed: list[TelegramUpdate] = []
        for update in updates:
            parsed_update = parse_update(update)
            if parsed_update is not None:
                parsed.append(parsed_update)
        return parsed
