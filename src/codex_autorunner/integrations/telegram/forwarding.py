"""Helpers for Telegram forwarded inbound messages."""

from __future__ import annotations

from typing import Optional

from ..chat.forwarding import compose_forwarded_message_text
from ..chat.models import ChatForwardInfo
from .adapter import TelegramMessage


def is_forwarded_telegram_message(message: TelegramMessage) -> bool:
    return message.forward_origin is not None


def format_forwarded_telegram_message_text(
    message: TelegramMessage,
    text: Optional[str],
) -> str:
    if message.forward_origin is None:
        return (text or "").strip()
    return compose_forwarded_message_text(
        None,
        forwarded_from=message_forward_info(message, text),
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
