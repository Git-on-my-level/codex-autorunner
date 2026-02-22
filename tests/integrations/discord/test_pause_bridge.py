from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


class _FakeRest:
    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        return None

    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {"id": "msg-1", "channel_id": channel_id, "payload": payload}


class _FakeGateway:
    async def run(self, on_dispatch) -> None:
        return None

    async def stop(self) -> None:
        return None


class _FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        await asyncio.Event().wait()


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
        pma_enabled=True,
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()
    seed_repo_files(workspace, git_required=False)
    return workspace


def _create_paused_run_with_dispatch(workspace: Path, run_id: str, seq: str) -> None:
    db_path = workspace / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        if store.get_flow_run(run_id) is None:
            store.create_flow_run(run_id, "ticket_flow", input_data={}, state={})
        store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)

    history_dir = (
        workspace / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / seq
    )
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / "DISPATCH.md").write_text("Paused: need reply", encoding="utf-8")


@pytest.mark.anyio
async def test_pause_bridge_dedupes_by_run_and_dispatch_seq(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run_id = str(uuid.uuid4())
    _create_paused_run_with_dispatch(workspace, run_id, "0001")

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service._scan_and_enqueue_pause_notifications()
        await service._scan_and_enqueue_pause_notifications()
        queued = await store.list_outbox()
        assert len(queued) == 1

        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["last_pause_run_id"] == run_id
        assert binding["last_pause_dispatch_seq"] == "0001"

        _create_paused_run_with_dispatch(workspace, run_id, "0002")
        await service._scan_and_enqueue_pause_notifications()
        queued = await store.list_outbox()
        assert len(queued) == 2
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["last_pause_dispatch_seq"] == "0002"
    finally:
        await store.close()
