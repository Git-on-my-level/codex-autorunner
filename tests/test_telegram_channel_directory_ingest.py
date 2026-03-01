from __future__ import annotations

from pathlib import Path

import pytest

import codex_autorunner.integrations.telegram.service as telegram_service_module
from codex_autorunner.integrations.chat.channel_directory import ChannelDirectoryStore
from codex_autorunner.integrations.telegram.adapter import parse_update
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService


def _config(tmp_path: Path) -> TelegramBotConfig:
    return TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "allowed_chat_ids": [-1001],
            "allowed_user_ids": [42],
        },
        root=tmp_path,
        env={"CAR_TELEGRAM_BOT_TOKEN": "test-token"},
    )


@pytest.mark.anyio
async def test_inbound_message_records_channel_directory_with_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TelegramBotService(_config(tmp_path), hub_root=tmp_path)

    async def _noop_handle_message(_service: TelegramBotService, _message) -> None:
        return None

    monkeypatch.setattr(
        telegram_service_module.message_handlers,
        "handle_message",
        _noop_handle_message,
    )

    update = parse_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": -1001, "type": "supergroup", "title": "Team Room"},
                "message_thread_id": 77,
                "is_topic_message": True,
                "from": {"id": 42},
                "text": "hello",
                "date": 1700000000,
                "forum_topic_created": {"name": "Ops"},
            },
        }
    )
    assert update is not None
    assert update.message is not None

    try:
        await service._handle_message(update.message)
        entries = ChannelDirectoryStore(tmp_path).list_entries(limit=None)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["platform"] == "telegram"
        assert entry["chat_id"] == "-1001"
        assert entry["thread_id"] == "77"
        assert entry["display"] == "Team Room / Ops"
        assert entry["meta"] == {"chat_type": "supergroup", "topic_title": "Ops"}
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_inbound_message_records_channel_directory_with_id_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TelegramBotService(_config(tmp_path), hub_root=tmp_path)

    async def _noop_handle_message(_service: TelegramBotService, _message) -> None:
        return None

    monkeypatch.setattr(
        telegram_service_module.message_handlers,
        "handle_message",
        _noop_handle_message,
    )

    update = parse_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "chat": {"id": -1001, "type": "supergroup"},
                "message_thread_id": 88,
                "is_topic_message": True,
                "from": {"id": 42},
                "text": "fallback",
                "date": 1700000001,
            },
        }
    )
    assert update is not None
    assert update.message is not None

    try:
        await service._handle_message(update.message)
        entries = ChannelDirectoryStore(tmp_path).list_entries(limit=None)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["platform"] == "telegram"
        assert entry["chat_id"] == "-1001"
        assert entry["thread_id"] == "88"
        assert entry["display"] == "-1001 / 88"
        assert entry["meta"] == {"chat_type": "supergroup"}
    finally:
        await service._bot.close()


def test_parse_update_ignores_stale_reply_topic_created() -> None:
    update = parse_update(
        {
            "update_id": 3,
            "message": {
                "message_id": 12,
                "chat": {"id": -1001, "type": "supergroup", "title": "Team Room"},
                "message_thread_id": 77,
                "is_topic_message": True,
                "from": {"id": 42},
                "text": "normal message",
                "date": 1700000002,
                "reply_to_message": {
                    "message_id": 1,
                    "forum_topic_created": {"name": "Original Topic"},
                },
            },
        }
    )
    assert update is not None
    assert update.message is not None
    assert update.message.thread_title is None


@pytest.mark.anyio
async def test_inbound_message_preserves_and_updates_topic_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TelegramBotService(_config(tmp_path), hub_root=tmp_path)

    async def _noop_handle_message(_service: TelegramBotService, _message) -> None:
        return None

    monkeypatch.setattr(
        telegram_service_module.message_handlers,
        "handle_message",
        _noop_handle_message,
    )

    created = parse_update(
        {
            "update_id": 4,
            "message": {
                "message_id": 20,
                "chat": {"id": -1001, "type": "supergroup", "title": "Team Room"},
                "message_thread_id": 77,
                "is_topic_message": True,
                "from": {"id": 42},
                "text": "created",
                "date": 1700000100,
                "forum_topic_created": {"name": "Ops"},
            },
        }
    )
    stale_reply = parse_update(
        {
            "update_id": 5,
            "message": {
                "message_id": 21,
                "chat": {"id": -1001, "type": "supergroup", "title": "Team Room"},
                "message_thread_id": 77,
                "is_topic_message": True,
                "from": {"id": 42},
                "text": "normal",
                "date": 1700000101,
                "reply_to_message": {
                    "message_id": 1,
                    "forum_topic_created": {"name": "Old Name"},
                },
            },
        }
    )
    edited = parse_update(
        {
            "update_id": 6,
            "message": {
                "message_id": 22,
                "chat": {"id": -1001, "type": "supergroup", "title": "Team Room"},
                "message_thread_id": 77,
                "is_topic_message": True,
                "from": {"id": 42},
                "text": "renamed",
                "date": 1700000102,
                "forum_topic_edited": {"name": "Ops Renamed"},
            },
        }
    )
    after_rename = parse_update(
        {
            "update_id": 7,
            "message": {
                "message_id": 23,
                "chat": {"id": -1001, "type": "supergroup", "title": "Team Room"},
                "message_thread_id": 77,
                "is_topic_message": True,
                "from": {"id": 42},
                "text": "after rename",
                "date": 1700000103,
            },
        }
    )

    assert created and created.message
    assert stale_reply and stale_reply.message
    assert edited and edited.message
    assert after_rename and after_rename.message

    try:
        await service._handle_message(created.message)
        await service._handle_message(stale_reply.message)
        await service._handle_message(edited.message)
        await service._handle_message(after_rename.message)
        entries = ChannelDirectoryStore(tmp_path).list_entries(limit=None)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["display"] == "Team Room / Ops Renamed"
        assert entry["meta"] == {
            "chat_type": "supergroup",
            "topic_title": "Ops Renamed",
        }
    finally:
        await service._bot.close()
