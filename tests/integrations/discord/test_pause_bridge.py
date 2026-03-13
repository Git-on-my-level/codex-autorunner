from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return commands


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
        message_overflow="split",
        pma_enabled=True,
    )


def _workspace(tmp_path: Path) -> Path:
    seed_hub_files(tmp_path, force=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()
    seed_repo_files(workspace, git_required=False)
    return workspace


def _create_paused_run_with_dispatch(
    workspace: Path,
    run_id: str,
    seq: str,
    *,
    dispatch_text: str = "Paused: need reply",
) -> None:
    db_path = workspace / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        if store.get_flow_run(run_id) is None:
            store.create_flow_run(run_id, "ticket_flow", input_data={}, state={})
        store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)

    history_dir = (
        workspace / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / seq
    )
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / "DISPATCH.md").write_text(dispatch_text, encoding="utf-8")


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
        mirror_path = (
            workspace
            / ".codex-autorunner"
            / "flows"
            / run_id
            / "chat"
            / "outbound.jsonl"
        )
        mirrored = _read_jsonl(mirror_path)
        assert mirrored
        assert mirrored[-1]["event_type"] == "flow_pause_dispatch_notice"
        assert mirrored[-1]["kind"] == "dispatch"
        assert mirrored[-1]["actor"] == "car"

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


@pytest.mark.anyio
async def test_pause_bridge_chunked_messages_have_no_part_prefix(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    run_id = str(uuid.uuid4())
    dispatch_text = ("Paused: need reply\n" * 400).strip()
    _create_paused_run_with_dispatch(
        workspace,
        run_id,
        "0001",
        dispatch_text=dispatch_text,
    )

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
        queued = await store.list_outbox()
        assert len(queued) >= 2
        for record in queued:
            content = str(record.payload_json.get("content", ""))
            assert not content.startswith("Part ")
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pause_bridge_prefers_pause_dispatch_over_turn_summary(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    run_id = str(uuid.uuid4())
    _create_paused_run_with_dispatch(
        workspace,
        run_id,
        "0001",
        dispatch_text=(
            "---\nmode: pause\ntitle: Need input\n---\n\n"
            "Please answer the blocker before I continue.\n"
        ),
    )
    _create_paused_run_with_dispatch(
        workspace,
        run_id,
        "0002",
        dispatch_text=(
            "---\nmode: turn_summary\n---\n\n"
            "This summary should not be mirrored to Discord.\n"
        ),
    )

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
        queued = await store.list_outbox()
        assert len(queued) == 1
        content = str(queued[0].payload_json.get("content", ""))
        assert "Need input" in content
        assert "Please answer the blocker before I continue." in content
        assert "turn_summary" not in content
        assert "This summary should not be mirrored to Discord." not in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pause_bridge_skips_when_telegram_binding_is_preferred(
    tmp_path: Path,
) -> None:
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

    telegram_db = tmp_path / ".codex-autorunner" / "telegram_state.sqlite3"
    conn = sqlite3.connect(telegram_db)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE telegram_topics (
                    topic_key TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    workspace_path TEXT,
                    repo_id TEXT,
                    pma_enabled INTEGER NOT NULL DEFAULT 0,
                    last_active_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE telegram_topic_scopes (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO telegram_topics (
                    topic_key,
                    chat_id,
                    thread_id,
                    scope,
                    workspace_path,
                    repo_id,
                    last_active_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "123:456",
                    123,
                    456,
                    None,
                    str(workspace),
                    None,
                    "2026-03-12T03:30:00Z",
                    "2026-03-12T03:00:00Z",
                ),
            )
    finally:
        conn.close()

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway(),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._hub_raw_config_cache = {"telegram_bot": {"enabled": True}}

    try:
        await service._scan_and_enqueue_pause_notifications()
        queued = await store.list_outbox()
        assert queued == []
    finally:
        await store.close()
