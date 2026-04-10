from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, cast

from ...core.sqlite_utils import connect_sqlite
from ...core.state import now_iso
from ..chat.agents import normalize_hermes_profile

DISCORD_STATE_SCHEMA_VERSION = 10
DISCORD_INTERACTION_LEDGER_RETENTION_DAYS = 14
_UNSET = object()
_logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class InteractionLedgerRecord:
    interaction_id: str
    interaction_token: str
    interaction_kind: str
    channel_id: str
    guild_id: Optional[str]
    user_id: Optional[str]
    metadata_json: dict[str, Any]
    ack_mode: Optional[str] = None
    ack_completed_at: Optional[str] = None
    execution_status: str = "received"
    execution_started_at: Optional[str] = None
    execution_finished_at: Optional[str] = None
    execution_error: Optional[str] = None
    final_delivery_status: Optional[str] = None
    final_delivery_error: Optional[str] = None
    original_response_message_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    last_seen_at: str = ""


@dataclass(frozen=True)
class InteractionLedgerRegistration:
    record: InteractionLedgerRecord
    inserted: bool


class DiscordStateStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="discord-state"
        )
        self._connection: Optional[sqlite3.Connection] = None
        self._closed = False

    @property
    def path(self) -> Path:
        return self._db_path

    async def initialize(self) -> None:
        await self._run(self._ensure_initialized_sync)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._run(self._close_sync)
        self._executor.shutdown(wait=True)

    def __del__(self) -> None:
        # Best-effort cleanup when callers forget to await close().
        try:
            self._close_sync()
        except Exception:  # intentional: __del__ must never raise
            _logger.debug("close_sync failed in __del__", exc_info=True)
        try:
            self._executor.shutdown(wait=False)
        except Exception:  # intentional: __del__ must never raise
            _logger.debug("executor shutdown failed in __del__", exc_info=True)

    async def upsert_binding(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        workspace_path: str,
        repo_id: str | None,
        resource_kind: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        await self._run(
            self._upsert_binding_sync,
            channel_id,
            guild_id,
            workspace_path,
            repo_id,
            resource_kind,
            resource_id,
        )

    async def get_binding(self, *, channel_id: str) -> Optional[dict[str, Any]]:
        return await self._run(self._get_binding_sync, channel_id)  # type: ignore[no-any-return]

    async def list_bindings(self) -> list[dict[str, Any]]:
        return await self._run(self._list_bindings_sync)  # type: ignore[no-any-return]

    async def delete_binding(self, *, channel_id: str) -> None:
        await self._run(self._delete_binding_sync, channel_id)

    async def enqueue_outbox(self, record: OutboxRecord) -> OutboxRecord:
        return await self._run(self._upsert_outbox_sync, record)  # type: ignore[no-any-return]

    async def get_outbox(self, record_id: str) -> Optional[OutboxRecord]:
        return await self._run(self._get_outbox_sync, record_id)  # type: ignore[no-any-return]

    async def list_outbox(self) -> list[OutboxRecord]:
        return await self._run(self._list_outbox_sync)  # type: ignore[no-any-return]

    async def mark_outbox_delivered(self, record_id: str) -> None:
        await self._run(self._delete_outbox_sync, record_id)

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

    async def mark_dispatch_seen(
        self,
        *,
        channel_id: str,
        run_id: str,
        dispatch_seq: str,
    ) -> None:
        await self._run(
            self._mark_dispatch_seen_sync,
            channel_id,
            run_id,
            dispatch_seq,
        )

    async def mark_terminal_run_seen(
        self,
        *,
        channel_id: str,
        run_id: str,
    ) -> None:
        await self._run(
            self._mark_terminal_run_seen_sync,
            channel_id,
            run_id,
        )

    async def update_pma_state(
        self,
        *,
        channel_id: str,
        pma_enabled: bool,
        pma_prev_workspace_path: Optional[str] = None,
        pma_prev_repo_id: Optional[str] = None,
        pma_prev_resource_kind: Optional[str] = None,
        pma_prev_resource_id: Optional[str] = None,
    ) -> None:
        await self._run(
            self._update_pma_state_sync,
            channel_id,
            pma_enabled,
            pma_prev_workspace_path,
            pma_prev_repo_id,
            pma_prev_resource_kind,
            pma_prev_resource_id,
        )

    async def update_agent_state(
        self,
        *,
        channel_id: str,
        agent: str,
        agent_profile: Optional[str] | object = _UNSET,
        model_override: Optional[str] | object = _UNSET,
        reasoning_effort: Optional[str] | object = _UNSET,
    ) -> None:
        await self._run(
            self._update_agent_state_sync,
            channel_id,
            agent,
            agent_profile,
            model_override,
            reasoning_effort,
        )

    async def update_model_state(
        self,
        *,
        channel_id: str,
        model_override: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        clear_model: bool = False,
    ) -> None:
        await self._run(
            self._update_model_state_sync,
            channel_id,
            model_override,
            reasoning_effort,
            clear_model,
        )

    async def update_approval_mode(
        self,
        *,
        channel_id: str,
        mode: str,
    ) -> None:
        await self._run(
            self._update_approval_mode_sync,
            channel_id,
            mode,
        )

    async def set_pending_compact_seed(
        self,
        *,
        channel_id: str,
        seed_text: str,
        session_key: str,
    ) -> None:
        await self._run(
            self._set_pending_compact_seed_sync,
            channel_id,
            seed_text,
            session_key,
        )

    async def clear_pending_compact_seed(
        self,
        *,
        channel_id: str,
    ) -> None:
        await self._run(self._clear_pending_compact_seed_sync, channel_id)

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

    async def register_interaction(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        interaction_kind: str,
        channel_id: str,
        guild_id: Optional[str],
        user_id: Optional[str],
        metadata_json: dict[str, Any],
    ) -> InteractionLedgerRegistration:
        return cast(
            InteractionLedgerRegistration,
            await self._run(
                self._register_interaction_sync,
                interaction_id,
                interaction_token,
                interaction_kind,
                channel_id,
                guild_id,
                user_id,
                metadata_json,
            ),
        )

    async def get_interaction(
        self,
        interaction_id: str,
    ) -> Optional[InteractionLedgerRecord]:
        return await self._run(self._get_interaction_sync, interaction_id)  # type: ignore[no-any-return]

    async def claim_interaction_execution(self, interaction_id: str) -> bool:
        return await self._run(self._claim_interaction_execution_sync, interaction_id)  # type: ignore[no-any-return]

    async def mark_interaction_acknowledged(
        self,
        interaction_id: str,
        *,
        ack_mode: str,
        original_response_message_id: Optional[str] = None,
    ) -> None:
        await self._run(
            self._mark_interaction_acknowledged_sync,
            interaction_id,
            ack_mode,
            original_response_message_id,
        )

    async def mark_interaction_execution(
        self,
        interaction_id: str,
        *,
        execution_status: str,
        execution_error: Optional[str] = None,
    ) -> None:
        await self._run(
            self._mark_interaction_execution_sync,
            interaction_id,
            execution_status,
            execution_error,
        )

    async def record_interaction_delivery(
        self,
        interaction_id: str,
        *,
        delivery_status: str,
        delivery_error: Optional[str] = None,
        original_response_message_id: Optional[str] = None,
    ) -> None:
        await self._run(
            self._record_interaction_delivery_sync,
            interaction_id,
            delivery_status,
            delivery_error,
            original_response_message_id,
        )

    async def prune_interactions(
        self,
        *,
        retention_days: int = DISCORD_INTERACTION_LEDGER_RETENTION_DAYS,
    ) -> int:
        return await self._run(self._prune_interactions_sync, retention_days)  # type: ignore[no-any-return]

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
        self._prune_interactions_sync(DISCORD_INTERACTION_LEDGER_RETENTION_DAYS)

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
                    last_dispatch_run_id TEXT,
                    last_dispatch_seq TEXT,
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS interaction_ledger (
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
                    execution_error TEXT,
                    final_delivery_status TEXT,
                    final_delivery_error TEXT,
                    original_response_message_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_discord_interaction_ledger_last_seen
                    ON interaction_ledger(last_seen_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_discord_interaction_ledger_execution_status
                    ON interaction_ledger(execution_status)
                """
            )
            self._ensure_channel_binding_columns(conn)
            self._ensure_interaction_ledger_columns(conn)
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
        if "last_dispatch_run_id" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN last_dispatch_run_id TEXT"
            )
        if "last_dispatch_seq" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN last_dispatch_seq TEXT"
            )
        if "pma_enabled" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN pma_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "pma_prev_workspace_path" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN pma_prev_workspace_path TEXT"
            )
        if "pma_prev_repo_id" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN pma_prev_repo_id TEXT"
            )
        if "resource_kind" not in names:
            conn.execute("ALTER TABLE channel_bindings ADD COLUMN resource_kind TEXT")
        if "resource_id" not in names:
            conn.execute("ALTER TABLE channel_bindings ADD COLUMN resource_id TEXT")
        if "pma_prev_resource_kind" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN pma_prev_resource_kind TEXT"
            )
        if "pma_prev_resource_id" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN pma_prev_resource_id TEXT"
            )
        if "agent" not in names:
            conn.execute("ALTER TABLE channel_bindings ADD COLUMN agent TEXT")
        if "agent_profile" not in names:
            conn.execute("ALTER TABLE channel_bindings ADD COLUMN agent_profile TEXT")
        if "model_override" not in names:
            conn.execute("ALTER TABLE channel_bindings ADD COLUMN model_override TEXT")
        if "reasoning_effort" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN reasoning_effort TEXT"
            )
        if "approval_mode" not in names:
            conn.execute("ALTER TABLE channel_bindings ADD COLUMN approval_mode TEXT")
        if "approval_policy" not in names:
            conn.execute("ALTER TABLE channel_bindings ADD COLUMN approval_policy TEXT")
        if "sandbox_policy" not in names:
            conn.execute("ALTER TABLE channel_bindings ADD COLUMN sandbox_policy TEXT")
        if "rollout_path" not in names:
            conn.execute("ALTER TABLE channel_bindings ADD COLUMN rollout_path TEXT")
        if "last_terminal_run_id" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN last_terminal_run_id TEXT"
            )
        if "pending_compact_seed" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN pending_compact_seed TEXT"
            )
        if "pending_compact_session_key" not in names:
            conn.execute(
                "ALTER TABLE channel_bindings ADD COLUMN pending_compact_session_key TEXT"
            )

    def _ensure_interaction_ledger_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(interaction_ledger)").fetchall()
        names = {str(row["name"]) for row in rows}
        if not names:
            return
        if "execution_error" not in names:
            conn.execute(
                "ALTER TABLE interaction_ledger ADD COLUMN execution_error TEXT"
            )
        if "final_delivery_status" not in names:
            conn.execute(
                "ALTER TABLE interaction_ledger ADD COLUMN final_delivery_status TEXT"
            )
        if "final_delivery_error" not in names:
            conn.execute(
                "ALTER TABLE interaction_ledger ADD COLUMN final_delivery_error TEXT"
            )
        if "original_response_message_id" not in names:
            conn.execute(
                "ALTER TABLE interaction_ledger ADD COLUMN original_response_message_id TEXT"
            )

    def _upsert_binding_sync(
        self,
        channel_id: str,
        guild_id: Optional[str],
        workspace_path: str,
        repo_id: Optional[str],
        resource_kind: Optional[str],
        resource_id: Optional[str],
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
                    resource_kind,
                    resource_id,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    guild_id=excluded.guild_id,
                    workspace_path=excluded.workspace_path,
                    repo_id=excluded.repo_id,
                    resource_kind=excluded.resource_kind,
                    resource_id=excluded.resource_id,
                    updated_at=excluded.updated_at
                """,
                (
                    channel_id,
                    guild_id,
                    workspace_path,
                    repo_id,
                    resource_kind,
                    resource_id,
                    now_iso(),
                ),
            )

    def _binding_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        pma_enabled_raw = row["pma_enabled"] if "pma_enabled" in row.keys() else 0
        agent_raw = row["agent"] if "agent" in row.keys() else None
        agent_profile_raw = (
            row["agent_profile"] if "agent_profile" in row.keys() else None
        )
        model_override_raw = (
            row["model_override"] if "model_override" in row.keys() else None
        )
        reasoning_effort_raw = (
            row["reasoning_effort"] if "reasoning_effort" in row.keys() else None
        )
        approval_mode_raw = (
            row["approval_mode"] if "approval_mode" in row.keys() else None
        )
        approval_policy_raw = (
            row["approval_policy"] if "approval_policy" in row.keys() else None
        )
        sandbox_policy_raw = (
            row["sandbox_policy"] if "sandbox_policy" in row.keys() else None
        )
        rollout_path_raw = row["rollout_path"] if "rollout_path" in row.keys() else None
        last_terminal_run_id_raw = (
            row["last_terminal_run_id"]
            if "last_terminal_run_id" in row.keys()
            else None
        )
        pending_compact_seed_raw = (
            row["pending_compact_seed"]
            if "pending_compact_seed" in row.keys()
            else None
        )
        pending_compact_session_key_raw = (
            row["pending_compact_session_key"]
            if "pending_compact_session_key" in row.keys()
            else None
        )
        agent = agent_raw if isinstance(agent_raw, str) else None
        agent_profile = (
            normalize_hermes_profile(agent_profile_raw)
            if isinstance(agent_profile_raw, str)
            else None
        )
        if agent_profile is None and isinstance(agent, str):
            agent_profile = normalize_hermes_profile(agent)
        if agent_profile is None and isinstance(agent_profile_raw, str):
            raw_profile = agent_profile_raw.strip().lower()
            if raw_profile and agent == "hermes":
                agent_profile = raw_profile
        if agent_profile is not None:
            agent = "hermes"
        return {
            "channel_id": str(row["channel_id"]),
            "guild_id": row["guild_id"] if isinstance(row["guild_id"], str) else None,
            "workspace_path": str(row["workspace_path"]),
            "repo_id": row["repo_id"] if isinstance(row["repo_id"], str) else None,
            "last_dispatch_run_id": (
                row["last_dispatch_run_id"]
                if "last_dispatch_run_id" in row.keys()
                and isinstance(row["last_dispatch_run_id"], str)
                else None
            ),
            "last_dispatch_seq": (
                row["last_dispatch_seq"]
                if "last_dispatch_seq" in row.keys()
                and isinstance(row["last_dispatch_seq"], str)
                else None
            ),
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
            "pma_enabled": bool(pma_enabled_raw),
            "pma_prev_workspace_path": (
                row["pma_prev_workspace_path"]
                if "pma_prev_workspace_path" in row.keys()
                and isinstance(row["pma_prev_workspace_path"], str)
                else None
            ),
            "pma_prev_repo_id": (
                row["pma_prev_repo_id"]
                if "pma_prev_repo_id" in row.keys()
                and isinstance(row["pma_prev_repo_id"], str)
                else None
            ),
            "resource_kind": (
                row["resource_kind"]
                if "resource_kind" in row.keys()
                and isinstance(row["resource_kind"], str)
                else None
            ),
            "resource_id": (
                row["resource_id"]
                if "resource_id" in row.keys() and isinstance(row["resource_id"], str)
                else None
            ),
            "pma_prev_resource_kind": (
                row["pma_prev_resource_kind"]
                if "pma_prev_resource_kind" in row.keys()
                and isinstance(row["pma_prev_resource_kind"], str)
                else None
            ),
            "pma_prev_resource_id": (
                row["pma_prev_resource_id"]
                if "pma_prev_resource_id" in row.keys()
                and isinstance(row["pma_prev_resource_id"], str)
                else None
            ),
            "agent": agent,
            "agent_profile": agent_profile,
            "model_override": (
                model_override_raw if isinstance(model_override_raw, str) else None
            ),
            "reasoning_effort": (
                reasoning_effort_raw if isinstance(reasoning_effort_raw, str) else None
            ),
            "approval_mode": (
                approval_mode_raw if isinstance(approval_mode_raw, str) else None
            ),
            "approval_policy": (
                approval_policy_raw if isinstance(approval_policy_raw, str) else None
            ),
            "sandbox_policy": (
                sandbox_policy_raw if isinstance(sandbox_policy_raw, str) else None
            ),
            "rollout_path": (
                rollout_path_raw if isinstance(rollout_path_raw, str) else None
            ),
            "last_terminal_run_id": (
                last_terminal_run_id_raw
                if isinstance(last_terminal_run_id_raw, str)
                else None
            ),
            "pending_compact_seed": (
                pending_compact_seed_raw
                if isinstance(pending_compact_seed_raw, str)
                else None
            ),
            "pending_compact_session_key": (
                pending_compact_session_key_raw
                if isinstance(pending_compact_session_key_raw, str)
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

    def _delete_binding_sync(self, channel_id: str) -> None:
        conn = self._connection_sync()
        with conn:
            conn.execute(
                "DELETE FROM channel_bindings WHERE channel_id = ?",
                (channel_id,),
            )

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

    def _mark_dispatch_seen_sync(
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
                SET last_dispatch_run_id = ?,
                    last_dispatch_seq = ?,
                    updated_at = ?
                WHERE channel_id = ?
                """,
                (run_id, dispatch_seq, now_iso(), channel_id),
            )

    def _mark_terminal_run_seen_sync(
        self,
        channel_id: str,
        run_id: str,
    ) -> None:
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                UPDATE channel_bindings
                SET last_terminal_run_id = ?,
                    updated_at = ?
                WHERE channel_id = ?
                """,
                (run_id, now_iso(), channel_id),
            )

    def _update_pma_state_sync(
        self,
        channel_id: str,
        pma_enabled: bool,
        pma_prev_workspace_path: Optional[str],
        pma_prev_repo_id: Optional[str],
        pma_prev_resource_kind: Optional[str],
        pma_prev_resource_id: Optional[str],
    ) -> None:
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                UPDATE channel_bindings
                SET pma_enabled = ?,
                    pma_prev_workspace_path = ?,
                    pma_prev_repo_id = ?,
                    pma_prev_resource_kind = ?,
                    pma_prev_resource_id = ?,
                    updated_at = ?
                WHERE channel_id = ?
                """,
                (
                    1 if pma_enabled else 0,
                    pma_prev_workspace_path,
                    pma_prev_repo_id,
                    pma_prev_resource_kind,
                    pma_prev_resource_id,
                    now_iso(),
                    channel_id,
                ),
            )

    def _update_agent_state_sync(
        self,
        channel_id: str,
        agent: str,
        agent_profile: Optional[str] | object,
        model_override: Optional[str] | object,
        reasoning_effort: Optional[str] | object,
    ) -> None:
        conn = self._connection_sync()
        with conn:
            if model_override is not _UNSET or reasoning_effort is not _UNSET:
                conn.execute(
                    """
                    UPDATE channel_bindings
                    SET agent = ?,
                        agent_profile = ?,
                        model_override = ?,
                        reasoning_effort = ?,
                        updated_at = ?
                    WHERE channel_id = ?
                    """,
                    (
                        agent,
                        None if agent_profile is _UNSET else agent_profile,
                        None if model_override is _UNSET else model_override,
                        None if reasoning_effort is _UNSET else reasoning_effort,
                        now_iso(),
                        channel_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE channel_bindings
                    SET agent = ?,
                        agent_profile = ?,
                        updated_at = ?
                    WHERE channel_id = ?
                    """,
                    (
                        agent,
                        None if agent_profile is _UNSET else agent_profile,
                        now_iso(),
                        channel_id,
                    ),
                )

    def _update_model_state_sync(
        self,
        channel_id: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        clear_model: bool,
    ) -> None:
        conn = self._connection_sync()
        if clear_model:
            with conn:
                conn.execute(
                    """
                    UPDATE channel_bindings
                    SET model_override = NULL,
                        reasoning_effort = NULL,
                        updated_at = ?
                    WHERE channel_id = ?
                    """,
                    (now_iso(), channel_id),
                )
        else:
            with conn:
                conn.execute(
                    """
                    UPDATE channel_bindings
                    SET model_override = ?,
                        reasoning_effort = ?,
                        updated_at = ?
                    WHERE channel_id = ?
                    """,
                    (model_override, reasoning_effort, now_iso(), channel_id),
                )

    def _update_approval_mode_sync(
        self,
        channel_id: str,
        mode: str,
    ) -> None:
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                UPDATE channel_bindings
                SET approval_mode = ?,
                    updated_at = ?
                WHERE channel_id = ?
                """,
                (mode, now_iso(), channel_id),
            )

    def _set_pending_compact_seed_sync(
        self,
        channel_id: str,
        seed_text: str,
        session_key: str,
    ) -> None:
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                UPDATE channel_bindings
                SET pending_compact_seed = ?,
                    pending_compact_session_key = ?,
                    updated_at = ?
                WHERE channel_id = ?
                """,
                (seed_text, session_key, now_iso(), channel_id),
            )

    def _clear_pending_compact_seed_sync(self, channel_id: str) -> None:
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                UPDATE channel_bindings
                SET pending_compact_seed = NULL,
                    pending_compact_session_key = NULL,
                    updated_at = ?
                WHERE channel_id = ?
                """,
                (now_iso(), channel_id),
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

    def _register_interaction_sync(
        self,
        interaction_id: str,
        interaction_token: str,
        interaction_kind: str,
        channel_id: str,
        guild_id: Optional[str],
        user_id: Optional[str],
        metadata_json: dict[str, Any],
    ) -> InteractionLedgerRegistration:
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT * FROM interaction_ledger WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        now = now_iso()
        inserted = row is None
        metadata_blob = json.dumps(metadata_json, sort_keys=True)
        with conn:
            if inserted:
                conn.execute(
                    """
                    INSERT INTO interaction_ledger (
                        interaction_id,
                        interaction_token,
                        interaction_kind,
                        channel_id,
                        guild_id,
                        user_id,
                        metadata_json,
                        execution_status,
                        created_at,
                        updated_at,
                        last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        interaction_id,
                        interaction_token,
                        interaction_kind,
                        channel_id,
                        guild_id,
                        user_id,
                        metadata_blob,
                        "received",
                        now,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE interaction_ledger
                    SET interaction_token = ?,
                        interaction_kind = ?,
                        channel_id = ?,
                        guild_id = ?,
                        user_id = ?,
                        metadata_json = ?,
                        updated_at = ?,
                        last_seen_at = ?
                    WHERE interaction_id = ?
                    """,
                    (
                        interaction_token,
                        interaction_kind,
                        channel_id,
                        guild_id,
                        user_id,
                        metadata_blob,
                        now,
                        now,
                        interaction_id,
                    ),
                )
        stored = conn.execute(
            "SELECT * FROM interaction_ledger WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        if stored is None:
            raise RuntimeError(
                f"interaction ledger missing after registration: {interaction_id}"
            )
        return InteractionLedgerRegistration(
            record=self._interaction_from_row(stored),
            inserted=inserted,
        )

    def _interaction_from_row(self, row: sqlite3.Row) -> InteractionLedgerRecord:
        raw_metadata = row["metadata_json"]
        metadata: dict[str, Any] = {}
        if isinstance(raw_metadata, str) and raw_metadata:
            try:
                parsed = json.loads(raw_metadata)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                metadata = parsed
        return InteractionLedgerRecord(
            interaction_id=str(row["interaction_id"]),
            interaction_token=str(row["interaction_token"]),
            interaction_kind=str(row["interaction_kind"]),
            channel_id=str(row["channel_id"]),
            guild_id=row["guild_id"] if isinstance(row["guild_id"], str) else None,
            user_id=row["user_id"] if isinstance(row["user_id"], str) else None,
            metadata_json=metadata,
            ack_mode=row["ack_mode"] if isinstance(row["ack_mode"], str) else None,
            ack_completed_at=(
                row["ack_completed_at"]
                if isinstance(row["ack_completed_at"], str)
                else None
            ),
            execution_status=str(row["execution_status"]),
            execution_started_at=(
                row["execution_started_at"]
                if isinstance(row["execution_started_at"], str)
                else None
            ),
            execution_finished_at=(
                row["execution_finished_at"]
                if isinstance(row["execution_finished_at"], str)
                else None
            ),
            execution_error=(
                row["execution_error"]
                if isinstance(row["execution_error"], str)
                else None
            ),
            final_delivery_status=(
                row["final_delivery_status"]
                if isinstance(row["final_delivery_status"], str)
                else None
            ),
            final_delivery_error=(
                row["final_delivery_error"]
                if isinstance(row["final_delivery_error"], str)
                else None
            ),
            original_response_message_id=(
                row["original_response_message_id"]
                if isinstance(row["original_response_message_id"], str)
                else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_seen_at=str(row["last_seen_at"]),
        )

    def _get_interaction_sync(
        self,
        interaction_id: str,
    ) -> Optional[InteractionLedgerRecord]:
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT * FROM interaction_ledger WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        if row is None:
            return None
        return self._interaction_from_row(row)

    def _claim_interaction_execution_sync(self, interaction_id: str) -> bool:
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            cursor = conn.execute(
                """
                UPDATE interaction_ledger
                SET execution_status = 'running',
                    execution_started_at = COALESCE(execution_started_at, ?),
                    updated_at = ?,
                    last_seen_at = ?
                WHERE interaction_id = ?
                  AND execution_status IN ('received', 'acknowledged')
                """,
                (now, now, now, interaction_id),
            )
        return cursor.rowcount > 0

    def _mark_interaction_acknowledged_sync(
        self,
        interaction_id: str,
        ack_mode: str,
        original_response_message_id: Optional[str],
    ) -> None:
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute(
                """
                UPDATE interaction_ledger
                SET ack_mode = COALESCE(ack_mode, ?),
                    ack_completed_at = COALESCE(ack_completed_at, ?),
                    execution_status = CASE
                        WHEN execution_status = 'received' THEN 'acknowledged'
                        ELSE execution_status
                    END,
                    original_response_message_id = COALESCE(?, original_response_message_id),
                    updated_at = ?,
                    last_seen_at = ?
                WHERE interaction_id = ?
                """,
                (
                    ack_mode,
                    now,
                    original_response_message_id,
                    now,
                    now,
                    interaction_id,
                ),
            )

    def _mark_interaction_execution_sync(
        self,
        interaction_id: str,
        execution_status: str,
        execution_error: Optional[str],
    ) -> None:
        conn = self._connection_sync()
        now = now_iso()
        error_text = str(execution_error)[:500] if execution_error is not None else None
        if execution_status == "running":
            with conn:
                conn.execute(
                    """
                    UPDATE interaction_ledger
                    SET execution_status = ?,
                        execution_started_at = COALESCE(execution_started_at, ?),
                        execution_error = ?,
                        updated_at = ?,
                        last_seen_at = ?
                    WHERE interaction_id = ?
                    """,
                    (
                        execution_status,
                        now,
                        error_text,
                        now,
                        now,
                        interaction_id,
                    ),
                )
            return
        with conn:
            conn.execute(
                """
                UPDATE interaction_ledger
                SET execution_status = ?,
                    execution_finished_at = ?,
                    execution_error = ?,
                    updated_at = ?,
                    last_seen_at = ?
                WHERE interaction_id = ?
                """,
                (
                    execution_status,
                    now,
                    error_text,
                    now,
                    now,
                    interaction_id,
                ),
            )

    def _record_interaction_delivery_sync(
        self,
        interaction_id: str,
        delivery_status: str,
        delivery_error: Optional[str],
        original_response_message_id: Optional[str],
    ) -> None:
        conn = self._connection_sync()
        now = now_iso()
        error_text = str(delivery_error)[:500] if delivery_error is not None else None
        with conn:
            conn.execute(
                """
                UPDATE interaction_ledger
                SET final_delivery_status = ?,
                    final_delivery_error = ?,
                    original_response_message_id = COALESCE(?, original_response_message_id),
                    updated_at = ?,
                    last_seen_at = ?
                WHERE interaction_id = ?
                """,
                (
                    delivery_status,
                    error_text,
                    original_response_message_id,
                    now,
                    now,
                    interaction_id,
                ),
            )

    def _prune_interactions_sync(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        conn = self._connection_sync()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=int(retention_days))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with conn:
            cursor = conn.execute(
                "DELETE FROM interaction_ledger WHERE last_seen_at < ?",
                (cutoff,),
            )
        return int(cursor.rowcount or 0)
