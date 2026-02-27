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
        assert entry["meta"] == {"chat_type": "supergroup"}
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
