"""Helpers for Telegram forwarded inbound messages."""

from __future__ import annotations

from typing import Optional

from ..chat.forwarding import compose_inbound_message_text
from ..chat.models import ChatForwardInfo, ChatMessageRef, ChatReplyInfo, ChatThreadRef
from .adapter import TelegramMessage


def is_forwarded_telegram_message(message: TelegramMessage) -> bool:
    return message.forward_origin is not None


def format_forwarded_telegram_message_text(
    message: TelegramMessage,
    text: Optional[str],
) -> str:
    return compose_inbound_message_text(
        None if message.forward_origin is not None else text,
        forwarded_from=message_forward_info(message, text),
        reply_context=message_reply_info(message),
    )


def message_forward_info(
    message: TelegramMessage, text: Optional[str]
) -> Optional[ChatForwardInfo]:
    origin = message.forward_origin
    if origin is None:
        return None
    return ChatForwardInfo(
        source_label=origin.source_label,
        message_id=str(origin.message_id) if origin.message_id is not None else None,
        text=(text or "").strip() or None,
        is_automatic=origin.is_automatic,
    )


def message_reply_info(message: TelegramMessage) -> Optional[ChatReplyInfo]:
    if message.reply_to_message_id is None:
        return None
    thread = ChatThreadRef(
        platform="telegram",
        chat_id=str(message.chat_id),
        thread_id=str(message.thread_id) if message.thread_id is not None else None,
    )
    return ChatReplyInfo(
        message=ChatMessageRef(
            thread=thread, message_id=str(message.reply_to_message_id)
        ),
        text=message.reply_to_text,
        author_label=message.reply_to_author_label or message.reply_to_username,
        is_bot=message.reply_to_is_bot,
    )
