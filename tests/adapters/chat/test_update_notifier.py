from __future__ import annotations

from codex_autorunner.adapters.chat.update_notifier import (
    build_update_notify_metadata,
    format_update_status_message,
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


def test_format_update_status_message_includes_slowest_phase_timings() -> None:
    rendered = format_update_status_message(
        {
            "status": "running",
            "message": "Restarted hub service.",
            "phase_timings": [
                {"phase": "pip_install", "status": "ok", "duration_ms": 178000},
                {"phase": "hub_restart", "status": "ok", "duration_ms": 31000},
                {"phase": "checks", "status": "failed", "duration_ms": 186000},
                {"phase": "quick", "status": "ok", "duration_ms": 25},
            ],
        }
    )

    assert (
        "Timings: checks=3m06s (failed), pip_install=2m58s, hub_restart=31.0s"
        in rendered
    )
