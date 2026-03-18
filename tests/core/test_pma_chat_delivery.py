from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.pma_chat_delivery import (
    notify_preferred_bound_chat_for_workspace,
    notify_primary_pma_chat_for_repo,
)
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.telegram.state import TelegramStateStore, topic_key
from tests.conftest import write_test_config


def _hub(tmp_path: Path) -> Path:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["discord_bot"]["enabled"] = True
    cfg["telegram_bot"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    return hub_root


def _set_discord_binding_updated_at(
    state_path: Path, channel_id: str, updated_at: str
) -> None:
    conn = sqlite3.connect(state_path)
    try:
        conn.execute(
            "UPDATE channel_bindings SET updated_at = ? WHERE channel_id = ?",
            (updated_at, channel_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_telegram_topic_updated_at(state_path: Path, key: str, updated_at: str) -> None:
    conn = sqlite3.connect(state_path)
    try:
        conn.execute(
            "UPDATE telegram_topics SET updated_at = ? WHERE topic_key = ?",
            (updated_at, key),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.anyio
async def test_notify_primary_pma_chat_prefers_freshest_matching_discord_binding(
    tmp_path: Path,
) -> None:
    hub_root = _hub(tmp_path)
    workspace = (hub_root / "worktrees" / "repo-a").resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    discord_store = DiscordStateStore(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    telegram_store = TelegramStateStore(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        await discord_store.upsert_binding(
            channel_id="older-discord",
            guild_id="guild-1",
            workspace_path=str(workspace),
            repo_id="repo-a",
        )
        await discord_store.update_pma_state(
            channel_id="older-discord",
            pma_enabled=True,
            pma_prev_workspace_path=str(workspace),
            pma_prev_repo_id="repo-a",
        )
        _set_discord_binding_updated_at(
            hub_root / ".codex-autorunner" / "discord_state.sqlite3",
            "older-discord",
            "2026-03-18T08:22:01Z",
        )

        await discord_store.upsert_binding(
            channel_id="newer-discord",
            guild_id="guild-1",
            workspace_path=str(workspace),
            repo_id="repo-a",
        )
        await discord_store.update_pma_state(
            channel_id="newer-discord",
            pma_enabled=True,
            pma_prev_workspace_path=str(workspace),
            pma_prev_repo_id="repo-a",
        )
        _set_discord_binding_updated_at(
            hub_root / ".codex-autorunner" / "discord_state.sqlite3",
            "newer-discord",
            "2026-03-18T08:22:09Z",
        )

        await telegram_store.bind_topic(
            topic_key(1001, 2002),
            str(workspace),
            repo_id="repo-a",
        )
        telegram_state = await telegram_store.load()
        telegram_topic = telegram_state.topics[topic_key(1001, 2002)]
        telegram_topic.pma_enabled = True
        telegram_topic.pma_prev_repo_id = "repo-a"
        await telegram_store.save(telegram_state)
        _set_telegram_topic_updated_at(
            hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
            topic_key(1001, 2002),
            "2026-03-18T08:22:05Z",
        )

        outcome = await notify_primary_pma_chat_for_repo(
            hub_root=hub_root,
            repo_id="repo-a",
            message="Escalation message",
            correlation_id="corr-1",
        )

        assert outcome["targets"] == 1
        assert outcome["published"] == 1

        await discord_store.close()
        await telegram_store.close()
        discord_store = DiscordStateStore(
            hub_root / ".codex-autorunner" / "discord_state.sqlite3"
        )
        telegram_store = TelegramStateStore(
            hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
        )
        discord_outbox = await discord_store.list_outbox()
        telegram_outbox = await telegram_store.list_outbox()
        assert any(
            record.channel_id == "newer-discord"
            and record.payload_json.get("content") == "Escalation message"
            for record in discord_outbox
        )
        assert not telegram_outbox
    finally:
        await discord_store.close()
        await telegram_store.close()


@pytest.mark.anyio
async def test_notify_preferred_bound_chat_for_workspace_uses_non_pma_preferred_surface(
    tmp_path: Path,
) -> None:
    hub_root = _hub(tmp_path)
    workspace = (hub_root / "worktrees" / "repo-b").resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    discord_store = DiscordStateStore(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    telegram_store = TelegramStateStore(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        await discord_store.upsert_binding(
            channel_id="repo-discord",
            guild_id="guild-1",
            workspace_path=str(workspace),
            repo_id="repo-b",
        )
        _set_discord_binding_updated_at(
            hub_root / ".codex-autorunner" / "discord_state.sqlite3",
            "repo-discord",
            "2026-03-18T09:00:10Z",
        )
        await telegram_store.bind_topic(
            topic_key(2001, 3002),
            str(workspace),
            repo_id="repo-b",
        )
        _set_telegram_topic_updated_at(
            hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
            topic_key(2001, 3002),
            "2026-03-18T09:00:01Z",
        )

        outcome = await notify_preferred_bound_chat_for_workspace(
            hub_root=hub_root,
            workspace_root=workspace,
            repo_id="repo-b",
            message="Auto reply mirror",
            correlation_id="corr-2",
        )

        assert outcome["targets"] == 1
        assert outcome["published"] == 1

        await discord_store.close()
        await telegram_store.close()
        discord_store = DiscordStateStore(
            hub_root / ".codex-autorunner" / "discord_state.sqlite3"
        )
        telegram_store = TelegramStateStore(
            hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
        )
        discord_outbox = await discord_store.list_outbox()
        telegram_outbox = await telegram_store.list_outbox()
        assert any(
            record.channel_id == "repo-discord"
            and record.payload_json.get("content") == "Auto reply mirror"
            for record in discord_outbox
        )
        assert not telegram_outbox
    finally:
        await discord_store.close()
        await telegram_store.close()
