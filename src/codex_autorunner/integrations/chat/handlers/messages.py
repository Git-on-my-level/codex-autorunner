"""Platform-agnostic message handler helpers."""

from __future__ import annotations

from typing import Any, Optional, Sequence


def message_text_candidate(
    *,
    text: Optional[str],
    caption: Optional[str],
    entities: Sequence[Any],
    caption_entities: Sequence[Any],
) -> tuple[str, str, Sequence[Any]]:
    """Pick message text/caption and the matching entity set."""

    raw_text = text or ""
    raw_caption = caption or ""
    text_candidate = raw_text if raw_text.strip() else raw_caption
    active_entities = entities if raw_text.strip() else caption_entities
    return raw_text, text_candidate, active_entities


def is_ticket_reply(
    *,
    reply_to_is_bot: bool,
    reply_to_message_id: Optional[int],
    reply_to_username: Optional[str],
    bot_username: Optional[str],
) -> bool:
    """Return whether a message is a reply to the bot (ticket-flow style)."""

    if not reply_to_is_bot or reply_to_message_id is None:
        return False
    if bot_username and reply_to_username:
        return reply_to_username.lower() == bot_username.lower()
    return True
