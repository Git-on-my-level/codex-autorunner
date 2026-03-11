from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.discord import service as discord_service_module
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


class _FakeRest:
    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, object]],
        guild_id: str | None = None,
    ) -> list[dict[str, object]]:
        return commands


class _FakeGateway:
    def __init__(self) -> None:
        self.ran = False

    async def run(self, _on_dispatch) -> None:
        self.ran = True


class _FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
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
        pma_enabled=False,
    )


@pytest.mark.anyio
async def test_service_startup_reaps_managed_processes(
    tmp_path: Path, monkeypatch
) -> None:
    called_roots: list[Path] = []

    def _fake_reap(root: Path) -> SimpleNamespace:
        called_roots.append(root)
        return SimpleNamespace(killed=0, signaled=0, removed=0, skipped=0)

    monkeypatch.setattr(discord_service_module, "reap_managed_processes", _fake_reap)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test.discord.startup"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert called_roots == [tmp_path, tmp_path]
    finally:
        await store.close()
