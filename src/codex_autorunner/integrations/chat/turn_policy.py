from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

TurnTriggerMode = Literal["always", "mentions"]


@dataclass(frozen=True)
class PlainTextTurnContext:
    """Shared trigger context for plain-text direct-chat turns."""

    text: str
    chat_type: Optional[str] = None
    bot_username: Optional[str] = None
    reply_to_is_bot: bool = False
    reply_to_username: Optional[str] = None
    reply_to_message_id: Optional[int] = None
    thread_id: Optional[int] = None


def should_trigger_plain_text_turn(
    *,
    mode: TurnTriggerMode,
    context: PlainTextTurnContext,
) -> bool:
    """Return True when a plain-text message should trigger an agent turn."""

    if mode == "always":
        return True
    if mode != "mentions":
        return False

    if context.chat_type == "private":
        return True

    lowered = (context.text or "").lower()
    if context.bot_username:
        needle = f"@{context.bot_username}".lower()
        if needle in lowered:
            return True

    implicit_topic_reply = (
        context.thread_id is not None
        and context.reply_to_message_id is not None
        and context.reply_to_message_id == context.thread_id
    )
    if context.reply_to_is_bot and not implicit_topic_reply:
        return True
    if (
        context.bot_username
        and context.reply_to_username
        and context.reply_to_username.lower() == context.bot_username.lower()
        and not implicit_topic_reply
    ):
        return True

    return False
