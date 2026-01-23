from __future__ import annotations

from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.trigger_mode import should_trigger_run


def _base_message(**kwargs) -> TelegramMessage:
    base = dict(
        update_id=1,
        message_id=10,
        chat_id=-100,
        thread_id=None,
        from_user_id=123,
        text="hi",
        date=0,
        is_topic_message=False,
        chat_type="supergroup",
    )
    base.update(kwargs)
    return TelegramMessage(**base)


def test_mentions_mode_triggers_in_private_chat() -> None:
    msg = _base_message(chat_type="private", text="hello")
    assert should_trigger_run(msg, text="hello", bot_username="MyBot") is True


def test_mentions_mode_triggers_on_username_mention() -> None:
    msg = _base_message(text="hey @MyBot please run")
    assert should_trigger_run(msg, text=msg.text or "", bot_username="MyBot") is True


def test_mentions_mode_triggers_on_reply_to_bot() -> None:
    msg = _base_message(reply_to_is_bot=True, reply_to_message_id=999)
    assert should_trigger_run(msg, text=msg.text or "", bot_username="MyBot") is True


def test_mentions_mode_ignores_implicit_topic_root_reply() -> None:
    msg = _base_message(thread_id=77, reply_to_is_bot=True, reply_to_message_id=77)
    assert should_trigger_run(msg, text=msg.text or "", bot_username="MyBot") is False


def test_mentions_mode_does_not_trigger_without_invocation() -> None:
    msg = _base_message(text="regular message")
    assert should_trigger_run(msg, text=msg.text or "", bot_username="MyBot") is False
