from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.integrations.discord.test_service_routing import (
    _config,
    _FakeGateway,
    _FakeOutboxManager,
    _FakeRest,
)
from tests.telegram_pma_routing_support import _ManagedThreadPMAHandler

from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as execution_commands_module,
)
from codex_autorunner.integrations.telegram.state_types import TelegramTopicRecord


@pytest.mark.anyio
async def test_managed_thread_interrupt_already_finished_reconciles_on_both_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    discord_store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await discord_store.initialize()
    await discord_store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    discord_rest = _FakeRest()
    discord_service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=discord_rest,
        gateway_client=_FakeGateway([]),
        state_store=discord_store,
        outbox_manager=_FakeOutboxManager(),
    )

    class _DiscordThreadService:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            assert surface_kind == "discord"
            assert surface_key == "channel-1"
            return SimpleNamespace(thread_target_id="thread-1", mode="repo")

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return SimpleNamespace(thread_target_id="thread-1")

        def get_running_execution(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return None

        async def stop_thread(self, thread_target_id: str, **kwargs: Any) -> Any:
            assert thread_target_id == "thread-1"
            _ = kwargs
            return SimpleNamespace(
                interrupted_active=False,
                recovered_lost_backend=False,
                cancelled_queued=0,
                execution=None,
            )

    discord_service._discord_thread_service = lambda: _DiscordThreadService()  # type: ignore[assignment]

    telegram_handler = _ManagedThreadPMAHandler(
        TelegramTopicRecord(
            pma_enabled=True,
            workspace_path=None,
            repo_id="repo-1",
            agent="codex",
        ),
        tmp_path,
    )

    class _TelegramThreadService:
        def get_running_execution(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return None

        async def stop_thread(self, thread_target_id: str, **kwargs: Any) -> Any:
            assert thread_target_id == "thread-1"
            _ = kwargs
            return SimpleNamespace(
                interrupted_active=False,
                recovered_lost_backend=False,
                cancelled_queued=0,
                execution=None,
            )

    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _TelegramThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="pma"),
            SimpleNamespace(thread_target_id="thread-1"),
        ),
    )

    try:
        await discord_service._handle_car_interrupt(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        await telegram_handler._process_interrupt(
            chat_id=-1001,
            thread_id=101,
            reply_to=10,
            runtime=SimpleNamespace(
                current_turn_id=None,
                interrupt_requested=False,
                interrupt_turn_id=None,
            ),
            message_id=100,
        )
        assert discord_rest.followup_messages[0]["payload"]["content"] == (
            "Current turn already finished."
        )
        assert telegram_handler._sent == ["Active PMA turn already finished."]
    finally:
        await discord_store.close()
