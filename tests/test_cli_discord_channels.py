from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.orchestration.sqlite import (
    initialize_orchestration_sqlite,
    open_orchestration_sqlite,
)

runner = CliRunner()


class _FakeDiscordRestClient:
    channels: dict[str, dict[str, Any]] = {}
    guilds: dict[str, dict[str, Any]] = {}
    channel_calls: list[str] = []

    def __init__(self, *, bot_token: str) -> None:
        self.bot_token = bot_token

    async def __aenter__(self) -> "_FakeDiscordRestClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get_channel(self, *, channel_id: str) -> dict[str, Any]:
        self.channel_calls.append(channel_id)
        payload = self.channels.get(channel_id)
        if payload is None:
            raise RuntimeError("missing channel")
        return dict(payload)

    async def get_guild(self, *, guild_id: str) -> dict[str, Any]:
        return dict(self.guilds.get(guild_id, {}))


def _insert_discord_binding(
    hub_root: Path,
    *,
    channel_id: str,
    thread_id: str,
    agent: str = "codex",
    repo_id: str = "repo",
    lifecycle_status: str = "idle",
    disabled_at: str | None = None,
) -> None:
    initialize_orchestration_sqlite(hub_root, durable=False)
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        conn.execute(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id,
                agent_id,
                repo_id,
                resource_kind,
                resource_id,
                workspace_root,
                display_name,
                lifecycle_status,
                runtime_status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'repo', ?, ?, ?, ?, 'idle', ?, ?)
            """,
            (
                thread_id,
                agent,
                repo_id,
                repo_id,
                str(hub_root / "worktrees" / repo_id),
                repo_id,
                lifecycle_status,
                "2026-05-14T00:00:00Z",
                "2026-05-14T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO orch_bindings (
                binding_id,
                surface_kind,
                surface_key,
                target_kind,
                target_id,
                agent_id,
                repo_id,
                resource_kind,
                resource_id,
                mode,
                created_at,
                updated_at,
                disabled_at
            )
            VALUES (?, 'discord', ?, 'thread', ?, ?, ?, 'repo', ?, 'bound', ?, ?, ?)
            """,
            (
                f"binding-{channel_id}",
                channel_id,
                thread_id,
                agent,
                repo_id,
                repo_id,
                "2026-05-14T00:00:00Z",
                "2026-05-14T00:00:00Z",
                disabled_at,
            ),
        )


def test_discord_channels_help_registered() -> None:
    result = runner.invoke(app, ["discord", "--help"])
    assert result.exit_code == 0
    assert "channels" in result.stdout


def test_discord_channels_outputs_resolved_table(hub_env, monkeypatch) -> None:
    _insert_discord_binding(
        hub_env.hub_root,
        channel_id="1495134681929355404",
        thread_id="21ee4260-0000-0000-0000-000000000000",
        agent="codex",
        repo_id="agent-nexus-saas",
    )
    _FakeDiscordRestClient.channels = {
        "1495134681929355404": {
            "id": "1495134681929355404",
            "name": "anx-saas",
            "type": 0,
            "guild_id": "guild-1",
        }
    }
    _FakeDiscordRestClient.guilds = {
        "guild-1": {"id": "guild-1", "name": "David's Server"}
    }
    _FakeDiscordRestClient.channel_calls = []
    monkeypatch.setattr(
        "codex_autorunner.adapters.chat.surface_resolver.DiscordRestClient",
        _FakeDiscordRestClient,
    )
    monkeypatch.setenv("CAR_DISCORD_BOT_TOKEN", "token")

    result = runner.invoke(
        app, ["discord", "channels", "--path", str(hub_env.hub_root)]
    )

    assert result.exit_code == 0, result.output
    assert "CHANNEL ID" in result.output
    assert "#anx-saas" in result.output
    assert "text" in result.output
    assert "David's Server" in result.output
    assert "21ee4260 (codex/agent-nexus-saas)" in result.output


def test_discord_channels_json_and_bound_only_filters(hub_env, monkeypatch) -> None:
    _insert_discord_binding(
        hub_env.hub_root,
        channel_id="1495134681929355404",
        thread_id="21ee4260-0000-0000-0000-000000000000",
    )
    _insert_discord_binding(
        hub_env.hub_root,
        channel_id="1488827014600331415",
        thread_id="1a2a98a4-0000-0000-0000-000000000000",
        lifecycle_status="archived",
        disabled_at="2026-05-14T00:01:00Z",
    )
    _FakeDiscordRestClient.channels = {
        "1495134681929355404": {
            "id": "1495134681929355404",
            "name": "discord-2",
            "type": 0,
            "guild_id": "guild-1",
        },
        "1488827014600331415": {
            "id": "1488827014600331415",
            "type": 1,
        },
    }
    _FakeDiscordRestClient.guilds = {
        "guild-1": {"id": "guild-1", "name": "David's Server"}
    }
    _FakeDiscordRestClient.channel_calls = []
    monkeypatch.setattr(
        "codex_autorunner.adapters.chat.surface_resolver.DiscordRestClient",
        _FakeDiscordRestClient,
    )
    monkeypatch.setenv("CAR_DISCORD_BOT_TOKEN", "token")

    result = runner.invoke(
        app,
        [
            "discord",
            "channels",
            "--json",
            "--bound-only",
            "--guild",
            "guild-1",
            "--path",
            str(hub_env.hub_root),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["channel_id"] for row in payload] == ["1495134681929355404"]
    assert payload[0]["guild"] == "David's Server"
    assert payload[0]["type"] == "text"


def test_discord_channels_excludes_notification_bindings(hub_env, monkeypatch) -> None:
    _insert_discord_binding(
        hub_env.hub_root,
        channel_id="1495134681929355404",
        thread_id="21ee4260-0000-0000-0000-000000000000",
    )
    _insert_discord_binding(
        hub_env.hub_root,
        channel_id="notification:notif-123",
        thread_id="1a2a98a4-0000-0000-0000-000000000000",
    )
    _FakeDiscordRestClient.channels = {
        "1495134681929355404": {
            "id": "1495134681929355404",
            "name": "discord-2",
            "type": 0,
            "guild_id": "guild-1",
        },
    }
    _FakeDiscordRestClient.guilds = {
        "guild-1": {"id": "guild-1", "name": "David's Server"}
    }
    _FakeDiscordRestClient.channel_calls = []
    monkeypatch.setattr(
        "codex_autorunner.adapters.chat.surface_resolver.DiscordRestClient",
        _FakeDiscordRestClient,
    )
    monkeypatch.setenv("CAR_DISCORD_BOT_TOKEN", "token")

    result = runner.invoke(
        app, ["discord", "channels", "--json", "--path", str(hub_env.hub_root)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["channel_id"] for row in payload] == ["1495134681929355404"]
    assert "notification:notif-123" not in result.output
    assert _FakeDiscordRestClient.channel_calls == ["1495134681929355404"]


def test_discord_channels_degrades_when_token_missing(hub_env, monkeypatch) -> None:
    _insert_discord_binding(
        hub_env.hub_root,
        channel_id="1495134681929355404",
        thread_id="21ee4260-0000-0000-0000-000000000000",
    )
    monkeypatch.delenv("CAR_DISCORD_BOT_TOKEN", raising=False)

    result = runner.invoke(
        app, ["discord", "channels", "--path", str(hub_env.hub_root)]
    )

    assert result.exit_code == 0, result.output
    assert "1495134681929355404" in result.output
    assert "(unavailable)" in result.output
