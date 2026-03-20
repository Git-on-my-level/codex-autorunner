from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.app_server.event_buffer import AppServerEventBuffer
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


@pytest.mark.anyio
async def test_service_exposes_app_server_event_buffer_context(
    tmp_path: Path, monkeypatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    monkeypatch.setattr(
        discord_service_module, "load_repo_config", lambda *args, **kwargs: None
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test.discord.context"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    try:
        assert isinstance(service.app_server_events, AppServerEventBuffer)
        assert service.app_server_supervisor is not None
        assert service.opencode_supervisor is not None

        supervisor = await service._app_server_supervisor_for_workspace(workspace)
        handler = getattr(supervisor, "_notification_handler", None)
        assert callable(handler)

        await handler(
            {
                "method": "turn/error",
                "params": {
                    "threadId": "discord-thread-1",
                    "turnId": "discord-turn-1",
                    "message": "failed",
                },
            }
        )

        events = await service.app_server_events.list_events(
            "discord-thread-1",
            "discord-turn-1",
        )
        assert len(events) == 1
    finally:
        await service._close_all_app_server_supervisors()
        await store.close()


@pytest.mark.anyio
async def test_workspace_supervisor_uses_resolved_repo_app_server_command(
    tmp_path: Path, monkeypatch
) -> None:
    captured: list[dict[str, object]] = []

    class _FakeSupervisor:
        def __init__(self, command, **kwargs) -> None:
            captured.append({"command": list(command), **kwargs})

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    monkeypatch.setattr(
        discord_service_module,
        "load_repo_config",
        lambda *args, **kwargs: SimpleNamespace(
            app_server=SimpleNamespace(command=["/custom/codex", "app-server", "--x"])
        ),
    )
    monkeypatch.setattr(
        discord_service_module, "WorkspaceAppServerSupervisor", _FakeSupervisor
    )

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test.discord.command"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    try:
        await service._app_server_supervisor_for_workspace(workspace)
        assert captured
        assert captured[0]["command"] == ["/custom/codex", "app-server", "--x"]
    finally:
        await store.close()
