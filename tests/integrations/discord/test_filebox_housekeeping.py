from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.discord import service as discord_service_module
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotDispatchConfig,
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
    async def run(self, _on_dispatch) -> None:
        return None


class _FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        return None


def _config(root: Path, *, ack_budget_ms: int = 10_000) -> DiscordBotConfig:
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
        dispatch=DiscordBotDispatchConfig(ack_budget_ms=ack_budget_ms),
    )


@pytest.mark.anyio
async def test_filebox_prune_cycle_includes_bound_workspace_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "workspace-a"
    missing_root = tmp_path / "missing-workspace"
    workspace_root.mkdir()

    store = DiscordStateStore(tmp_path / ".codex-autorunner" / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace_root),
        repo_id="repo-1",
    )
    await store.upsert_binding(
        channel_id="channel-2",
        guild_id="guild-1",
        workspace_path=str(workspace_root),
        repo_id="repo-1",
    )
    await store.upsert_binding(
        channel_id="channel-3",
        guild_id="guild-1",
        workspace_path=str(missing_root),
        repo_id="repo-2",
    )

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test.discord.filebox"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._hub_config_path = tmp_path / "codex-autorunner.yml"

    load_calls: list[tuple[Path, Path | None]] = []
    prune_calls: list[tuple[Path, str]] = []

    def _fake_load_repo_config(root: Path, hub_path: Path | None = None):
        load_calls.append((root, hub_path))
        interval_seconds = 45 if root == workspace_root else 120
        return SimpleNamespace(
            housekeeping=SimpleNamespace(interval_seconds=interval_seconds),
            pma={"root": root.name},
        )

    def _fake_resolve_policy(pma: dict[str, str]) -> str:
        return f"policy:{pma['root']}"

    def _fake_prune(root: Path, *, policy: str):
        prune_calls.append((root, policy))
        return SimpleNamespace(
            inbox_pruned=0,
            outbox_pruned=0,
            bytes_before=0,
            bytes_after=0,
        )

    service._dependencies = discord_service_module.DiscordServiceDependencies(
        load_repo_config=_fake_load_repo_config,
        resolve_filebox_retention_policy=_fake_resolve_policy,
        prune_filebox_root=_fake_prune,
        reap_managed_processes=service._dependencies.reap_managed_processes,
    )

    try:
        interval_seconds = await service._run_filebox_prune_cycle()
    finally:
        await store.close()

    assert load_calls == [
        (tmp_path.resolve(), service._hub_config_path),
        (workspace_root.resolve(), service._hub_config_path),
    ]
    assert prune_calls == [
        (tmp_path.resolve(), f"policy:{tmp_path.resolve().name}"),
        (workspace_root.resolve(), "policy:workspace-a"),
    ]
    assert interval_seconds == 45.0
