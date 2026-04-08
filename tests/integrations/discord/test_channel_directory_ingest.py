from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.integrations.chat.channel_directory import (
    ChannelDirectoryStore,
    channel_entry_key,
)
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService


class _FakeRest:
    def __init__(self) -> None:
        self.channels_by_id: dict[str, dict[str, Any]] = {}
        self.guilds_by_id: dict[str, dict[str, Any]] = {}
        self.get_channel_calls: list[str] = []
        self.get_guild_calls: list[str] = []

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

    async def get_channel(self, *, channel_id: str) -> dict[str, Any]:
        self.get_channel_calls.append(channel_id)
        return dict(self.channels_by_id.get(channel_id, {}))

    async def get_guild(self, *, guild_id: str) -> dict[str, Any]:
        self.get_guild_calls.append(guild_id)
        return dict(self.guilds_by_id.get(guild_id, {}))


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
    tmp_path: Path,
) -> None:
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
    )

    await service._record_channel_directory_seen_from_message_payload(
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
    assert channel_entry_key(entry) == "discord:channel-1"
    assert "thread_id" not in entry
    assert entry["display"] == "CAR HQ / #general"
    assert entry["meta"] == {"guild_id": "guild-1"}


@pytest.mark.anyio
async def test_message_create_records_channel_directory_with_id_fallbacks(
    tmp_path: Path,
) -> None:
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
    )

    await service._record_channel_directory_seen_from_message_payload(
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
    assert channel_entry_key(entry) == "discord:channel-9"
    assert "thread_id" not in entry
    assert entry["display"] == "guild:guild-9 / #channel-9"
    assert entry["meta"] == {"guild_id": "guild-9"}


@pytest.mark.anyio
async def test_message_create_resolves_channel_directory_names_via_rest(
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
    rest.channels_by_id["channel-9"] = {
        "id": "channel-9",
        "guild_id": "guild-9",
        "name": "renamed-room",
    }
    rest.guilds_by_id["guild-9"] = {"id": "guild-9", "name": "CAR HQ"}

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway(),
    )

    payload = {
        "id": "m-3",
        "channel_id": "channel-9",
        "guild_id": "guild-9",
        "content": "hello",
        "author": {"id": "user-1", "bot": False},
    }
    await service._record_channel_directory_seen_from_message_payload(dict(payload))
    await service._record_channel_directory_seen_from_message_payload(dict(payload))

    entries = ChannelDirectoryStore(tmp_path).list_entries(limit=None)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["display"] == "CAR HQ / #renamed-room"
    assert entry["meta"] == {"guild_id": "guild-9"}
    assert rest.get_channel_calls == ["channel-9"]
    assert rest.get_guild_calls == ["guild-9"]


@pytest.mark.anyio
async def test_message_create_uses_negative_lookup_cache_for_missing_names(
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway(),
    )

    payload = {
        "id": "m-4",
        "channel_id": "channel-missing",
        "guild_id": "guild-missing",
        "content": "hello",
        "author": {"id": "user-1", "bot": False},
    }
    await service._record_channel_directory_seen_from_message_payload(dict(payload))
    await service._record_channel_directory_seen_from_message_payload(dict(payload))

    entries = ChannelDirectoryStore(tmp_path).list_entries(limit=None)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["display"] == "guild:guild-missing / #channel-missing"
    assert rest.get_channel_calls == ["channel-missing"]
    assert rest.get_guild_calls == ["guild-missing"]


@pytest.mark.anyio
async def test_message_create_concurrent_rest_resolution_deduplicates_lookups(
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway(),
    )

    async def _slow_get_channel(*, channel_id: str) -> dict[str, Any]:
        rest.get_channel_calls.append(channel_id)
        await asyncio.sleep(0.01)
        return {
            "id": channel_id,
            "guild_id": "guild-9",
            "name": "renamed-room",
        }

    async def _slow_get_guild(*, guild_id: str) -> dict[str, Any]:
        rest.get_guild_calls.append(guild_id)
        await asyncio.sleep(0.01)
        return {"id": guild_id, "name": "CAR HQ"}

    rest.get_channel = _slow_get_channel  # type: ignore[assignment]
    rest.get_guild = _slow_get_guild  # type: ignore[assignment]

    payload = {
        "id": "m-5",
        "channel_id": "channel-9",
        "guild_id": "guild-9",
        "content": "hello",
        "author": {"id": "user-1", "bot": False},
    }

    await asyncio.gather(
        service._record_channel_directory_seen_from_message_payload(dict(payload)),
        service._record_channel_directory_seen_from_message_payload(dict(payload)),
    )

    entries = ChannelDirectoryStore(tmp_path).list_entries(limit=None)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["display"] == "CAR HQ / #renamed-room"
    assert rest.get_channel_calls == ["channel-9"]
    assert rest.get_guild_calls == ["guild-9"]


@pytest.mark.anyio
async def test_message_create_dispatch_does_not_wait_for_channel_directory_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
    )

    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()
    submitted_message_ids: list[str] = []

    async def _slow_record(payload: dict[str, Any]) -> None:
        lookup_started.set()
        await release_lookup.wait()

    def _submit_event(event: Any) -> None:
        submitted_message_ids.append(event.message.message_id)

    monkeypatch.setattr(
        service,
        "_record_channel_directory_seen_from_message_payload",
        _slow_record,
    )
    monkeypatch.setattr(service._command_runner, "submit_event", _submit_event)

    payload = {
        "id": "m-hot-path",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "content": "hello",
        "author": {"id": "user-1", "bot": False},
    }

    await asyncio.wait_for(service._on_dispatch("MESSAGE_CREATE", dict(payload)), 0.1)
    await asyncio.wait_for(lookup_started.wait(), 0.1)

    assert submitted_message_ids == ["m-hot-path"]

    release_lookup.set()
    await asyncio.sleep(0)
