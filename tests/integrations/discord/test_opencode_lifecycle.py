from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    async def run(self, _on_dispatch) -> None:
        return None


class _FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        return None


class _StubOpenCodeSupervisor:
    def __init__(self, *, pruned_handles: int, handles_after_prune: int) -> None:
        self._pruned_handles = pruned_handles
        self._handles_after_prune = handles_after_prune
        self.close_all_calls = 0

    async def prune_idle(self) -> int:
        return self._pruned_handles

    async def lifecycle_snapshot(self) -> Any:
        return SimpleNamespace(
            cached_handles=self._handles_after_prune,
            active_turns=0,
        )

    async def close_all(self) -> None:
        self.close_all_calls += 1


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
async def test_opencode_prune_sweep_evicts_idle_supervisors_and_logs_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    events: list[tuple[str, dict[str, Any]]] = []

    def _fake_log_event(_logger, _level, event: str, **kwargs: Any) -> None:
        events.append((event, kwargs))

    monkeypatch.setattr(discord_service_module, "log_event", _fake_log_event)
    monkeypatch.setattr(
        discord_service_module,
        "load_repo_config",
        lambda workspace_root, hub_path=None: SimpleNamespace(
            opencode=SimpleNamespace(idle_ttl_seconds=120),
        ),
    )

    supervisors: dict[str, _StubOpenCodeSupervisor] = {
        str(workspace_a): _StubOpenCodeSupervisor(
            pruned_handles=1,
            handles_after_prune=0,
        ),
        str(workspace_b): _StubOpenCodeSupervisor(
            pruned_handles=0,
            handles_after_prune=1,
        ),
    }

    monkeypatch.setattr(
        discord_service_module,
        "build_opencode_supervisor_from_repo_config",
        lambda repo_config, *, workspace_root, logger, base_env=None: supervisors[
            str(workspace_root)
        ],
    )

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test.discord.opencode"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        supervisor_a = await service._opencode_supervisor_for_workspace(workspace_a)
        supervisor_b = await service._opencode_supervisor_for_workspace(workspace_b)

        assert supervisor_a is supervisors[str(workspace_a)]
        assert supervisor_b is supervisors[str(workspace_b)]
        assert await service._next_opencode_prune_interval_seconds() == 60.0

        for entry in service._opencode_supervisors.values():
            entry.last_requested_at = time.monotonic() - 120.0

        await service._prune_opencode_supervisors()

        assert set(service._opencode_supervisors.keys()) == {str(workspace_b)}
        assert supervisors[str(workspace_a)].close_all_calls == 1
        assert supervisors[str(workspace_b)].close_all_calls == 0

        prune_events = [
            payload
            for event, payload in events
            if event == "discord.opencode.prune_sweep"
        ]
        assert prune_events
        assert prune_events[-1] == {
            "cached_supervisors": 2,
            "cached_supervisors_after": 1,
            "live_handles": 1,
            "killed_processes": 1,
            "evicted_supervisors": 1,
        }
    finally:
        await store.close()
