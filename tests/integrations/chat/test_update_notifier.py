from __future__ import annotations

from codex_autorunner.integrations.chat.update_notifier import (
    build_update_notify_metadata,
)


def test_build_update_notify_metadata_excludes_notify_sent_at() -> None:
    payload = build_update_notify_metadata(platform="discord", chat_id="channel-1")

    assert payload == {
        "notify_platform": "discord",
        "notify_context": {"chat_id": "channel-1"},
    }
    assert "notify_sent_at" not in payload


def test_build_update_notify_metadata_telegram_notify_context() -> None:
    payload = build_update_notify_metadata(
        platform="telegram",
        chat_id=123,
        thread_id=456,
        reply_to=789,
    )

    assert payload == {
        "notify_platform": "telegram",
        "notify_context": {
            "chat_id": 123,
            "thread_id": 456,
            "reply_to": 789,
        },
    }
