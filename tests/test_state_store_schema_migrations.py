from __future__ import annotations

import sqlite3

import pytest

from codex_autorunner.core.sqlite_utils import read_schema_version
from codex_autorunner.integrations.discord.state import (
    DISCORD_STATE_SCHEMA_VERSION,
    DiscordStateStore,
)
from codex_autorunner.integrations.telegram.state import (
    TELEGRAM_SCHEMA_VERSION,
    TelegramStateStore,
)


def _column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows if row["name"] is not None}


@pytest.mark.asyncio
async def test_discord_state_store_migrates_legacy_schema(tmp_path):
    db_path = tmp_path / "discord_state.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_info(version) VALUES (1)")
        conn.execute(
            """
            CREATE TABLE channel_bindings (
                channel_id TEXT PRIMARY KEY,
                guild_id TEXT,
                workspace_path TEXT NOT NULL,
                repo_id TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE outbox (
                record_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                message_id TEXT,
                operation TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                created_at TEXT NOT NULL,
                last_error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE interaction_ledger (
                interaction_id TEXT PRIMARY KEY,
                interaction_token TEXT NOT NULL,
                interaction_kind TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                guild_id TEXT,
                user_id TEXT,
                metadata_json TEXT NOT NULL,
                ack_mode TEXT,
                ack_completed_at TEXT,
                execution_status TEXT NOT NULL,
                execution_started_at TEXT,
                execution_finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    store = DiscordStateStore(db_path)
    await store.initialize()
    await store.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert read_schema_version(conn) == DISCORD_STATE_SCHEMA_VERSION
        assert "operation_id" in _column_names(conn, "outbox")
        assert "pending_compact_session_key" in _column_names(conn, "channel_bindings")
        assert "original_response_message_id" in _column_names(
            conn, "interaction_ledger"
        )
        run_row = conn.execute(
            """
            SELECT status
              FROM car_migration_runs
             ORDER BY started_at DESC
             LIMIT 1
            """
        ).fetchone()
        assert run_row is not None
        assert str(run_row["status"]) == "completed"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_telegram_state_store_migrates_legacy_outbox_columns(tmp_path):
    db_path = tmp_path / "telegram_state.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE telegram_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO telegram_meta (key, value, updated_at)
            VALUES ('schema_version', '0', '2025-01-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            CREATE TABLE telegram_outbox (
                record_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    store = TelegramStateStore(db_path)
    await store.list_outbox()
    await store.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert read_schema_version(conn) == TELEGRAM_SCHEMA_VERSION
        columns = _column_names(conn, "telegram_outbox")
        assert {"next_attempt_at", "operation", "message_id", "outbox_key"} <= columns
        run_row = conn.execute(
            """
            SELECT status
              FROM car_migration_runs
             ORDER BY started_at DESC
             LIMIT 1
            """
        ).fetchone()
        assert run_row is not None
        assert str(run_row["status"]) == "completed"
    finally:
        conn.close()
