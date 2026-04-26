from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.app_server.event_buffer import AppServerEventBuffer
from codex_autorunner.integrations.discord import (
    managed_thread_startup_recovery as discord_startup_recovery_module,
)
from codex_autorunner.integrations.discord import service as discord_service_module
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotDispatchConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore

pytestmark = pytest.mark.integration


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
async def test_service_startup_starts_gateway_before_command_sync_finishes(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    sync_started = asyncio.Event()

    class _OrderedGateway(_FakeGateway):
        async def run(self, _on_dispatch) -> None:
            order.append("gateway")
            await sync_started.wait()
            await super().run(_on_dispatch)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    gateway = _OrderedGateway()
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test.discord.startup.sync"),
        rest_client=_FakeRest(),
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_sync_commands() -> None:
        order.append("sync")
        sync_started.set()

    service._sync_application_commands_on_startup = (  # type: ignore[method-assign]
        _fake_sync_commands
    )

    try:
        await service.run_forever()
        assert order == ["gateway", "sync"]
        assert gateway.ran is True
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_startup_runs_managed_thread_recovery_hook(
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: list[str] = []
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test.discord.startup.recovery"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    monkeypatch.setattr(
        discord_service_module,
        "_recover_managed_thread_executions_on_startup_impl",
        lambda owner: called.append(owner.__class__.__name__) or asyncio.sleep(0),
    )

    try:
        await service.run_forever()
        assert called == ["DiscordBotService"]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_startup_recovery_cleans_interrupted_progress_leases(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    cleanup_calls: list[dict[str, object]] = []

    async def _fake_recover_surface_managed_thread_executions_on_startup(
        service: object,
        **kwargs: object,
    ) -> None:
        _ = service
        captured.update(kwargs)

    async def _fake_cleanup_discord_terminal_progress_leases(
        owner: object,
        *,
        managed_thread_id: str,
        execution_id: str | None,
        channel_id: str,
        note: str,
        record_prefix: str,
    ) -> int:
        cleanup_calls.append(
            {
                "owner": owner,
                "managed_thread_id": managed_thread_id,
                "execution_id": execution_id,
                "channel_id": channel_id,
                "note": note,
                "record_prefix": record_prefix,
            }
        )
        return 1

    monkeypatch.setattr(
        discord_startup_recovery_module,
        "recover_surface_managed_thread_executions_on_startup",
        _fake_recover_surface_managed_thread_executions_on_startup,
    )
    monkeypatch.setattr(
        discord_startup_recovery_module,
        "cleanup_discord_terminal_progress_leases",
        _fake_cleanup_discord_terminal_progress_leases,
    )

    service = SimpleNamespace(
        _config=SimpleNamespace(root=tmp_path),
        _logger=logging.getLogger("test.discord.startup.interrupted_cleanup"),
    )

    await discord_startup_recovery_module.recover_managed_thread_executions_on_startup(
        service
    )

    build_execution_hooks = captured["build_execution_hooks"]
    hooks = build_execution_hooks(
        service,
        "channel-1",
        "thread-1",
        SimpleNamespace(workspace_root=tmp_path),
    )
    assert hooks.on_execution_finalized is not None

    await hooks.on_execution_finalized(
        SimpleNamespace(execution=SimpleNamespace(execution_id="turn-1")),
        SimpleNamespace(status="interrupted", managed_turn_id="turn-1"),
    )

    assert cleanup_calls == [
        {
            "owner": service,
            "managed_thread_id": "thread-1",
            "execution_id": "turn-1",
            "channel_id": "channel-1",
            "note": "Status: this turn was interrupted.",
            "record_prefix": (
                "discord:startup-recovery-interrupted-progress-cleanup:"
                "thread-1:turn-1"
            ),
        }
    ]


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
