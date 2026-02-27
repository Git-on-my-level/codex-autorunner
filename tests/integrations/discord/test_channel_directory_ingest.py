from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.integrations.chat.channel_directory import ChannelDirectoryStore
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService


class _FakeRest:
    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {"id": "msg-1", "channel_id": channel_id, "payload": payload}

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: str | None = None,
    ) -> list[dict[str, Any]]:
        _ = (application_id, guild_id)
        return commands


class _FakeGateway:
    async def run(self, _on_dispatch) -> None:
        return None

    async def stop(self) -> None:
        return None


def _config(root: Path) -> DiscordBotConfig:
    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN_ENV",
        app_id_env="APP_ENV",
        bot_token="token",
        application_id="app-1",
        allowed_guild_ids=frozenset({"guild-1"}),
        allowed_channel_ids=frozenset({"channel-1"}),
        allowed_user_ids=frozenset({"user-1"}),
        command_registration=DiscordCommandRegistration(
            enabled=True,
            scope="guild",
            guild_ids=("guild-1",),
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=2000,
        message_overflow="split",
        pma_enabled=True,
    )


@pytest.mark.anyio
async def test_message_create_records_channel_directory_with_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
    )

    async def _noop_dispatch(_event) -> None:
        return None

    monkeypatch.setattr(service, "_dispatch_chat_event", _noop_dispatch)

    await service._on_dispatch(
        "MESSAGE_CREATE",
        {
            "id": "m-1",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "channel_name": "general",
            "guild_name": "CAR HQ",
            "content": "hello",
            "author": {"id": "user-1", "bot": False},
        },
    )

    entries = ChannelDirectoryStore(tmp_path).list_entries(limit=None)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["platform"] == "discord"
    assert entry["chat_id"] == "channel-1"
    assert entry["thread_id"] == "guild-1"
    assert entry["display"] == "CAR HQ / #general"
    assert entry["meta"] == {"guild_id": "guild-1"}


@pytest.mark.anyio
async def test_message_create_records_channel_directory_with_id_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
    )

    async def _noop_dispatch(_event) -> None:
        return None

    monkeypatch.setattr(service, "_dispatch_chat_event", _noop_dispatch)

    await service._on_dispatch(
        "MESSAGE_CREATE",
        {
            "id": "m-2",
            "channel_id": "channel-9",
            "guild_id": "guild-9",
            "content": "hello",
            "author": {"id": "user-1", "bot": False},
        },
    )

    entries = ChannelDirectoryStore(tmp_path).list_entries(limit=None)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["platform"] == "discord"
    assert entry["chat_id"] == "channel-9"
    assert entry["thread_id"] == "guild-9"
    assert entry["display"] == "guild:guild-9 / #channel-9"
    assert entry["meta"] == {"guild_id": "guild-9"}
