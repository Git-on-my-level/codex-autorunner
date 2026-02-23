from __future__ import annotations

from codex_autorunner.integrations.chat.turn_policy import (
    PlainTextTurnContext,
    should_trigger_plain_text_turn,
)


def test_mentions_policy_triggers_in_private_chat() -> None:
    assert should_trigger_plain_text_turn(
        mode="mentions",
        context=PlainTextTurnContext(
            text="hello",
            chat_type="private",
            bot_username="MyBot",
        ),
    )


def test_mentions_policy_triggers_on_username_mention() -> None:
    assert should_trigger_plain_text_turn(
        mode="mentions",
        context=PlainTextTurnContext(
            text="hey @MyBot please run",
            chat_type="supergroup",
            bot_username="MyBot",
        ),
    )


def test_mentions_policy_triggers_on_reply_to_bot() -> None:
    assert should_trigger_plain_text_turn(
        mode="mentions",
        context=PlainTextTurnContext(
            text="",
            chat_type="supergroup",
            bot_username="MyBot",
            reply_to_is_bot=True,
            reply_to_message_id=999,
        ),
    )


def test_mentions_policy_ignores_implicit_topic_root_reply() -> None:
    assert not should_trigger_plain_text_turn(
        mode="mentions",
        context=PlainTextTurnContext(
            text="",
            chat_type="supergroup",
            bot_username="MyBot",
            reply_to_is_bot=True,
            reply_to_message_id=77,
            thread_id=77,
        ),
    )


def test_mentions_policy_does_not_trigger_without_invocation() -> None:
    assert not should_trigger_plain_text_turn(
        mode="mentions",
        context=PlainTextTurnContext(
            text="regular message",
            chat_type="supergroup",
            bot_username="MyBot",
        ),
    )


def test_mentions_policy_triggers_on_reply_to_bot_username_match() -> None:
    assert should_trigger_plain_text_turn(
        mode="mentions",
        context=PlainTextTurnContext(
            text="",
            chat_type="supergroup",
            bot_username="MyBot",
            reply_to_username="mybot",
            reply_to_message_id=123,
            thread_id=456,
        ),
    )


def test_always_policy_triggers_for_discord_like_plain_text() -> None:
    assert should_trigger_plain_text_turn(
        mode="always",
        context=PlainTextTurnContext(text="ship it"),
    )
