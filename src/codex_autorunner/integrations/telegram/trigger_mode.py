from __future__ import annotations

from typing import Literal, Optional

from ..chat.turn_policy import PlainTextTurnContext, should_trigger_plain_text_turn
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

    return should_trigger_plain_text_turn(
        mode="mentions",
        context=PlainTextTurnContext(
            text=text,
            chat_type=message.chat_type,
            bot_username=bot_username,
            reply_to_is_bot=message.reply_to_is_bot,
            reply_to_username=message.reply_to_username,
            reply_to_message_id=message.reply_to_message_id,
            thread_id=message.thread_id,
        ),
    )
