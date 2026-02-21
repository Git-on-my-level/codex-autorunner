from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.sqlite_utils import connect_sqlite
from ...core.state import now_iso

DISCORD_STATE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class OutboxRecord:
    record_id: str
    channel_id: str
    message_id: Optional[str]
    operation: str
    payload_json: dict[str, Any]
    attempts: int = 0
    next_attempt_at: Optional[str] = None
    created_at: str = ""
    last_error: Optional[str] = None


class DiscordStateStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="discord-state"
        )
        self._connection: Optional[sqlite3.Connection] = None

    @property
    def path(self) -> Path:
        return self._db_path

    async def initialize(self) -> None:
        await self._run(self._ensure_initialized_sync)

    async def close(self) -> None:
        await self._run(self._close_sync)
        self._executor.shutdown(wait=True)

    async def upsert_binding(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        workspace_path: str,
        repo_id: str | None,
    ) -> None:
        await self._run(
            self._upsert_binding_sync,
            channel_id,
            guild_id,
            workspace_path,
            repo_id,
        )

    async def get_binding(self, *, channel_id: str) -> Optional[dict[str, Any]]:
        return await self._run(self._get_binding_sync, channel_id)

    async def list_bindings(self) -> list[dict[str, Any]]:
        return await self._run(self._list_bindings_sync)

    async def mark_pause_dispatch_seen(
        self,
        *,
        channel_id: str,
        run_id: str,
        dispatch_seq: str,
    ) -> None:
        await self._run(
            self._mark_pause_dispatch_seen_sync,
            channel_id,
            run_id,
            dispatch_seq,
        )

    async def enqueue_outbox(self, record: OutboxRecord) -> OutboxRecord:
        return await self._run(self._upsert_outbox_sync, record)

    async def get_outbox(self, record_id: str) -> Optional[OutboxRecord]:
        return await self._run(self._get_outbox_sync, record_id)

    async def list_outbox(self) -> list[OutboxRecord]:
        return await self._run(self._list_outbox_sync)

    async def mark_outbox_delivered(self, record_id: str) -> None:
        await self._run(self._delete_outbox_sync, record_id)

    async def record_outbox_failure(
        self,
        record_id: str,
        *,
        error: str,
        retry_after_seconds: float | None,
    ) -> None:
        await self._run(
            self._record_outbox_failure_sync, record_id, error, retry_after_seconds
        )

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, func, *args)

    def _connection_sync(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = connect_sqlite(self._db_path)
            self._ensure_schema(self._connection)
        return self._connection

    def _ensure_initialized_sync(self) -> None:
        self._connection_sync()

    def _close_sync(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT version FROM schema_info ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_info(version) VALUES (?)",
                    (DISCORD_STATE_SCHEMA_VERSION,),
                )
                current_version = DISCORD_STATE_SCHEMA_VERSION
            else:
                current_version = int(row["version"] or 1)

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel_id TEXT PRIMARY KEY,
                    guild_id TEXT,
                    workspace_path TEXT NOT NULL,
                    repo_id TEXT,
                    last_pause_run_id TEXT,
                    last_pause_dispatch_seq TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
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
                CREATE INDEX IF NOT EXISTS idx_discord_outbox_next_attempt
                    ON outbox(next_attempt_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_discord_outbox_created
                    ON outbox(created_at)
                """
            )
            self._ensure_channel_binding_columns(conn)
            if current_version < DISCORD_STATE_SCHEMA_VERSION:
                conn.execute(
                    "UPDATE schema_info SET version = ?",
                    (DISCORD_STATE_SCHEMA_VERSION,),
                )

    def _ensure_channel_binding_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(channel_bindings)").fetchall()
        names = {str(row["name"]) for row in rows}
        if "last_pause_run_id" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN last_pause_run_id TEXT"
            )
        if "last_pause_dispatch_seq" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN last_pause_dispatch_seq TEXT"
            )

    def _upsert_binding_sync(
        self,
        channel_id: str,
        guild_id: Optional[str],
        workspace_path: str,
        repo_id: Optional[str],
    ) -> None:
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                INSERT INTO channel_bindings (
                    channel_id,
                    guild_id,
                    workspace_path,
                    repo_id,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    guild_id=excluded.guild_id,
                    workspace_path=excluded.workspace_path,
                    repo_id=excluded.repo_id,
                    updated_at=excluded.updated_at
                """,
                (
                    channel_id,
                    guild_id,
                    workspace_path,
                    repo_id,
                    now_iso(),
                ),
            )

    def _binding_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "channel_id": str(row["channel_id"]),
            "guild_id": row["guild_id"] if isinstance(row["guild_id"], str) else None,
            "workspace_path": str(row["workspace_path"]),
            "repo_id": row["repo_id"] if isinstance(row["repo_id"], str) else None,
            "last_pause_run_id": (
                row["last_pause_run_id"]
                if isinstance(row["last_pause_run_id"], str)
                else None
            ),
            "last_pause_dispatch_seq": (
                row["last_pause_dispatch_seq"]
                if isinstance(row["last_pause_dispatch_seq"], str)
                else None
            ),
            "updated_at": str(row["updated_at"]),
        }

    def _get_binding_sync(self, channel_id: str) -> Optional[dict[str, Any]]:
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT * FROM channel_bindings WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        if row is None:
            return None
        return self._binding_from_row(row)

    def _list_bindings_sync(self) -> list[dict[str, Any]]:
        conn = self._connection_sync()
        rows = conn.execute(
            "SELECT * FROM channel_bindings ORDER BY updated_at DESC"
        ).fetchall()
        return [self._binding_from_row(row) for row in rows]

    def _mark_pause_dispatch_seen_sync(
        self,
        channel_id: str,
        run_id: str,
        dispatch_seq: str,
    ) -> None:
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                UPDATE channel_bindings
                SET last_pause_run_id = ?,
                    last_pause_dispatch_seq = ?,
                    updated_at = ?
                WHERE channel_id = ?
                """,
                (run_id, dispatch_seq, now_iso(), channel_id),
            )

    def _upsert_outbox_sync(self, record: OutboxRecord) -> OutboxRecord:
        conn = self._connection_sync()
        created_at = record.created_at or now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO outbox (
                    record_id,
                    channel_id,
                    message_id,
                    operation,
                    payload_json,
                    attempts,
                    next_attempt_at,
                    created_at,
                    last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    message_id=excluded.message_id,
                    operation=excluded.operation,
                    payload_json=excluded.payload_json,
                    attempts=excluded.attempts,
                    next_attempt_at=excluded.next_attempt_at,
                    created_at=excluded.created_at,
                    last_error=excluded.last_error
                """,
                (
                    record.record_id,
                    record.channel_id,
                    record.message_id,
                    record.operation,
                    json.dumps(record.payload_json),
                    int(record.attempts),
                    record.next_attempt_at,
                    created_at,
                    record.last_error,
                ),
            )
        row = conn.execute(
            "SELECT * FROM outbox WHERE record_id = ?", (record.record_id,)
        ).fetchone()
        if row is None:
            return record
        return self._outbox_from_row(row)

    def _outbox_from_row(self, row: sqlite3.Row) -> OutboxRecord:
        raw_payload = row["payload_json"]
        payload: dict[str, Any] = {}
        if isinstance(raw_payload, str) and raw_payload:
            try:
                data = json.loads(raw_payload)
                if isinstance(data, dict):
                    payload = data
            except json.JSONDecodeError:
                payload = {}
        return OutboxRecord(
            record_id=str(row["record_id"]),
            channel_id=str(row["channel_id"]),
            message_id=(
                row["message_id"] if isinstance(row["message_id"], str) else None
            ),
            operation=str(row["operation"]),
            payload_json=payload,
            attempts=int(row["attempts"] or 0),
            next_attempt_at=(
                str(row["next_attempt_at"])
                if isinstance(row["next_attempt_at"], str)
                else None
            ),
            created_at=str(row["created_at"]),
            last_error=(
                row["last_error"] if isinstance(row["last_error"], str) else None
            ),
        )

    def _get_outbox_sync(self, record_id: str) -> Optional[OutboxRecord]:
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT * FROM outbox WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        return self._outbox_from_row(row)

    def _list_outbox_sync(self) -> list[OutboxRecord]:
        conn = self._connection_sync()
        rows = conn.execute("SELECT * FROM outbox ORDER BY created_at ASC").fetchall()
        return [self._outbox_from_row(row) for row in rows]

    def _delete_outbox_sync(self, record_id: str) -> None:
        conn = self._connection_sync()
        with conn:
            conn.execute("DELETE FROM outbox WHERE record_id = ?", (record_id,))

    def _record_outbox_failure_sync(
        self, record_id: str, error: str, retry_after_seconds: Optional[float]
    ) -> None:
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT attempts FROM outbox WHERE record_id = ?", (record_id,)
        ).fetchone()
        if row is None:
            return
        attempts = int(row["attempts"] or 0) + 1
        next_attempt_at = None
        if retry_after_seconds is not None:
            now = datetime.now(timezone.utc)
            delay = max(float(retry_after_seconds), 0.0)
            next_attempt_at = (now + timedelta(seconds=delay)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        with conn:
            conn.execute(
                """
                UPDATE outbox
                SET attempts = ?,
                    next_attempt_at = ?,
                    last_error = ?
                WHERE record_id = ?
                """,
                (attempts, next_attempt_at, str(error)[:500], record_id),
            )
