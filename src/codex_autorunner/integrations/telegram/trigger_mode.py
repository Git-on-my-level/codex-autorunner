from __future__ import annotations

from typing import Literal, Optional

from .adapter import TelegramMessage

TriggerMode = Literal["all", "mentions"]


def should_trigger_run(
    message: TelegramMessage,
    *,
    text: str,
    bot_username: Optional[str],
) -> bool:
    """Return True if this message should start a run in mentions-only mode.

    This mirrors Takopi's "mentions" trigger mode semantics (subset):

    - Always trigger in private chats.
    - Trigger when the bot is explicitly mentioned: "@<bot_username>" anywhere in the text.
    - Trigger when replying to a bot message (but ignore the common forum-topic
      "implicit root reply" case where clients set reply_to_message_id == thread_id).
    - Otherwise, do not trigger (commands and other explicit affordances are handled elsewhere).
    """

    if message.chat_type == "private":
        return True

    lowered = (text or "").lower()
    if bot_username:
        needle = f"@{bot_username}".lower()
        if needle in lowered:
            return True

    implicit_topic_reply = (
        message.thread_id is not None
        and message.reply_to_message_id is not None
        and message.reply_to_message_id == message.thread_id
    )

    if message.reply_to_is_bot and not implicit_topic_reply:
        return True

    if (
        bot_username
        and message.reply_to_username
        and message.reply_to_username.lower() == bot_username.lower()
        and not implicit_topic_reply
    ):
        return True

    return False
