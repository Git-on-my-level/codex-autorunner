from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from codex_autorunner.core.chat_bindings import (
    active_chat_binding_counts,
    repo_has_active_chat_binding,
)
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from tests.conftest import write_test_config


def _write_discord_binding(db_path: Path, *, channel_id: str, repo_id: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel_id TEXT PRIMARY KEY,
                    repo_id TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO channel_bindings (channel_id, repo_id)
                VALUES (?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET repo_id=excluded.repo_id
                """,
                (channel_id, repo_id),
            )
    finally:
        conn.close()


def _write_telegram_binding(db_path: Path, *, topic_key: str, repo_id: str) -> None:
    if ":" not in topic_key:
        raise ValueError(
            "topic_key must be in '<chat_id>:<thread_or_root>[:scope]' form"
        )
    parts = topic_key.split(":", 2)
    chat_id = int(parts[0])
    thread_raw = parts[1]
    thread_id = None if thread_raw == "root" else int(thread_raw)
    scope = parts[2] if len(parts) == 3 else None

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topics (
                    topic_key TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    repo_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topic_scopes (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO telegram_topics (topic_key, chat_id, thread_id, scope, repo_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(topic_key) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    scope=excluded.scope,
                    repo_id=excluded.repo_id
                """,
                (topic_key, chat_id, thread_id, scope, repo_id),
            )
    finally:
        conn.close()


def _write_telegram_topic_scope(
    db_path: Path, *, chat_id: int, thread_id: int | None, scope: str | None
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topic_scopes (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO telegram_topic_scopes (chat_id, thread_id, scope)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET scope=excluded.scope
                """,
                (chat_id, thread_id, scope),
            )
    finally:
        conn.close()


def test_active_chat_binding_counts_aggregates_persisted_sources(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    thread_store = PmaThreadStore(hub_root)
    thread_store.create_thread(
        "codex",
        (hub_root / "worktrees" / "repo-a-1").resolve(),
        repo_id="repo-a",
    )
    thread_store.create_thread(
        "codex",
        (hub_root / "worktrees" / "repo-a-2").resolve(),
        repo_id="repo-a",
    )

    _write_discord_binding(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        channel_id="discord-chan-1",
        repo_id="repo-a",
    )
    _write_discord_binding(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        channel_id="discord-chan-2",
        repo_id="repo-b",
    )
    _write_telegram_binding(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
        topic_key="123:root",
        repo_id="repo-b",
    )

    counts = active_chat_binding_counts(hub_root=hub_root, raw_config=cfg)
    assert counts["repo-a"] == 3
    assert counts["repo-b"] == 2


def test_repo_has_active_chat_binding_uses_configured_state_files(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["discord_bot"]["state_file"] = "state/custom-discord.sqlite3"
    cfg["telegram_bot"]["state_file"] = "state/custom-telegram.sqlite3"
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    discord_db = hub_root / "state" / "custom-discord.sqlite3"
    telegram_db = hub_root / "state" / "custom-telegram.sqlite3"
    _write_discord_binding(discord_db, channel_id="discord-chan", repo_id="repo-x")
    _write_telegram_binding(telegram_db, topic_key="999:root", repo_id="repo-y")

    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root, raw_config=cfg, repo_id="repo-x"
        )
        is True
    )
    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root, raw_config=cfg, repo_id="repo-y"
        )
        is True
    )
    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root, raw_config=cfg, repo_id="repo-z"
        )
        is False
    )


def test_telegram_binding_lookup_ignores_non_current_scoped_topics(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    telegram_db = hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    _write_telegram_topic_scope(
        telegram_db, chat_id=200, thread_id=17, scope="scope-current"
    )
    _write_telegram_binding(
        telegram_db,
        topic_key="200:17:scope-old",
        repo_id="repo-stale",
    )
    _write_telegram_binding(
        telegram_db,
        topic_key="200:17:scope-current",
        repo_id="repo-current",
    )

    counts = active_chat_binding_counts(hub_root=hub_root, raw_config=cfg)
    assert counts.get("repo-stale") is None
    assert counts.get("repo-current") == 1

    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root,
            raw_config=cfg,
            repo_id="repo-stale",
        )
        is False
    )
    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root,
            raw_config=cfg,
            repo_id="repo-current",
        )
        is True
    )
