from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.sqlite_utils import connect_sqlite
from ...core.state import now_iso
from ...core.text_utils import _parse_iso_timestamp
from .state_types import (
    APPROVAL_MODE_YOLO,
    STATE_VERSION,
    OutboxRecord,
    PendingApprovalRecord,
    PendingVoiceRecord,
    TelegramState,
    TelegramTopicRecord,
    _base_topic_key,
    normalize_approval_mode,
    parse_topic_key,
    topic_key,
)
from .state_types import APPROVAL_MODES as APPROVAL_MODES  # noqa: F401
from .state_types import TOPIC_ROOT as TOPIC_ROOT  # noqa: F401
from .state_types import ThreadSummary as ThreadSummary  # noqa: F401
from .state_types import normalize_agent as normalize_agent  # noqa: F401

logger = logging.getLogger("codex_autorunner.integrations.telegram.state")

STALE_SCOPED_TOPIC_DAYS = 30
MAX_SCOPED_TOPICS_PER_BASE = 5

TELEGRAM_SCHEMA_VERSION = 1


def _parse_json_payload(raw: Optional[str]) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _thread_predicate(thread_id: Optional[int]) -> tuple[str, tuple[Any, ...]]:
    if thread_id is None:
        return "thread_id IS NULL", ()
    return "thread_id = ?", (thread_id,)


class TelegramStateStore:
    """Persist Telegram transport-local topic metadata and delivery state.

    Orchestration-owned binding rows are the durable authority for bound thread
    identity; this store remains the compatibility home for Telegram-specific
    topic metadata, previews, and pending UI state.
    """

    def __init__(
        self, path: Path, *, default_approval_mode: str = APPROVAL_MODE_YOLO
    ) -> None:
        self._path = path
        self._default_approval_mode = normalize_approval_mode(default_approval_mode)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="telegram-state"
        )
        self._connection: Optional[sqlite3.Connection] = None
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._run(self._close_sync)
        self._executor.shutdown(wait=True)

    def __del__(self) -> None:
        try:
            self._close_sync()
        except Exception:  # intentional: __del__ must not raise
            logger.debug("state.__del__: close failed", exc_info=True)
        try:
            self._executor.shutdown(wait=False)
        except Exception:  # intentional: __del__ must not raise
            logger.debug("state.__del__: executor shutdown failed", exc_info=True)

    async def load(self) -> TelegramState:
        return await self._run(self._load_state_sync)

    async def save(self, state: TelegramState) -> None:
        await self._run(self._save_state_sync, state)

    async def get_topic(self, key: str) -> Optional[TelegramTopicRecord]:
        return await self._run(self._get_topic_sync, key)

    async def list_topics(self) -> dict[str, TelegramTopicRecord]:
        """Return all stored topics keyed by topic_key."""
        return await self._run(self._list_topics_sync)

    def _list_topics_sync(self) -> dict[str, TelegramTopicRecord]:
        conn = self._ensure_connection()
        cursor = conn.execute("SELECT topic_key, payload_json FROM telegram_topics")
        topics: dict[str, TelegramTopicRecord] = {}
        for key, payload_json in cursor.fetchall():
            try:
                payload = (
                    json.loads(payload_json) if isinstance(payload_json, str) else {}
                )
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("Failed to parse topic JSON for key %s: %s", key, exc)
                payload = {}
            record = TelegramTopicRecord.from_dict(
                payload, default_approval_mode=self._default_approval_mode
            )
            topics[str(key)] = record
        return topics

    async def get_topic_scope(self, key: str) -> Optional[str]:
        return await self._run(self._get_topic_scope_sync, key)

    async def set_topic_scope(self, key: str, scope: Optional[str]) -> None:
        await self._run(self._set_topic_scope_sync, key, scope)

    async def bind_topic(
        self,
        key: str,
        workspace_path: str,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> TelegramTopicRecord:
        if not isinstance(workspace_path, str) or not workspace_path:
            raise ValueError("workspace_path is required")

        def apply(record: TelegramTopicRecord) -> None:
            record.workspace_path = workspace_path
            record.workspace_id = workspace_id
            if repo_id is not None:
                record.repo_id = repo_id
            else:
                record.repo_id = None
            record.resource_kind = resource_kind
            record.resource_id = resource_id
            record.active_thread_id = None
            record.thread_ids = []
            record.thread_summaries = {}
            record.rollout_path = None
            record.pending_compact_seed = None
            record.pending_compact_seed_thread_id = None

        return await self._update_topic(key, apply)

    async def set_active_thread(
        self, key: str, thread_id: Optional[str]
    ) -> TelegramTopicRecord:
        def apply(record: TelegramTopicRecord) -> None:
            record.active_thread_id = thread_id

        return await self._update_topic(key, apply)

    async def find_active_thread(
        self, thread_id: str, *, exclude_key: Optional[str] = None
    ) -> Optional[str]:
        if not isinstance(thread_id, str) or not thread_id:
            return None
        return await self._run(self._find_active_thread_sync, thread_id, exclude_key)

    async def set_approval_mode(self, key: str, mode: str) -> TelegramTopicRecord:
        normalized = normalize_approval_mode(mode, default=self._default_approval_mode)

        def apply(record: TelegramTopicRecord) -> None:
            record.approval_mode = normalized

        return await self._update_topic(key, apply)

    async def ensure_topic(self, key: str) -> TelegramTopicRecord:
        """Create the topic record if absent and return it.

        The no-op apply callback is intentional: callers need an
        ``ensure-exists`` touch that does not modify any fields.
        """

        def apply(_record: TelegramTopicRecord) -> None:
            pass

        return await self._update_topic(key, apply)

    async def update_topic(
        self, key: str, apply: Callable[[TelegramTopicRecord], None]
    ) -> TelegramTopicRecord:
        return await self._update_topic(key, apply)

    async def upsert_pending_approval(
        self, record: PendingApprovalRecord
    ) -> PendingApprovalRecord:
        return await self._run(self._upsert_pending_approval_sync, record)

    async def clear_pending_approval(self, request_id: str) -> None:
        await self._run(self._clear_pending_approval_sync, request_id)

    async def pending_approvals_for_topic(
        self, chat_id: int, thread_id: Optional[int]
    ) -> list[PendingApprovalRecord]:
        return await self._run(
            self._pending_approvals_for_topic_sync, chat_id, thread_id
        )

    async def clear_pending_approvals_for_topic(
        self, chat_id: int, thread_id: Optional[int]
    ) -> None:
        await self._run(
            self._clear_pending_approvals_for_topic_sync, chat_id, thread_id
        )

    async def pending_approvals_for_key(self, key: str) -> list[PendingApprovalRecord]:
        return await self._run(self._pending_approvals_for_key_sync, key)

    async def clear_pending_approvals_for_key(self, key: str) -> None:
        await self._run(self._clear_pending_approvals_for_key_sync, key)

    async def enqueue_outbox(self, record: OutboxRecord) -> OutboxRecord:
        return await self._run(self._upsert_outbox_sync, record)

    async def update_outbox(self, record: OutboxRecord) -> OutboxRecord:
        return await self._run(self._upsert_outbox_sync, record)

    async def delete_outbox(self, record_id: str) -> None:
        await self._run(self._delete_outbox_sync, record_id)

    async def get_outbox(self, record_id: str) -> Optional[OutboxRecord]:
        return await self._run(self._get_outbox_sync, record_id)

    async def list_outbox(self) -> list[OutboxRecord]:
        return await self._run(self._list_outbox_sync)

    async def enqueue_pending_voice(
        self, record: PendingVoiceRecord
    ) -> PendingVoiceRecord:
        return await self._run(self._upsert_pending_voice_sync, record)

    async def update_pending_voice(
        self, record: PendingVoiceRecord
    ) -> PendingVoiceRecord:
        return await self._run(self._upsert_pending_voice_sync, record)

    async def delete_pending_voice(self, record_id: str) -> None:
        await self._run(self._delete_pending_voice_sync, record_id)

    async def get_pending_voice(self, record_id: str) -> Optional[PendingVoiceRecord]:
        return await self._run(self._get_pending_voice_sync, record_id)

    async def list_pending_voice(self) -> list[PendingVoiceRecord]:
        return await self._run(self._list_pending_voice_sync)

    async def get_last_update_id_global(self) -> Optional[int]:
        return await self._run(self._get_last_update_id_global_sync)

    async def update_last_update_id_global(self, update_id: int) -> Optional[int]:
        return await self._run(self._update_last_update_id_global_sync, update_id)

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, func, *args)

    def _ensure_connection(self) -> sqlite3.Connection:
        return self._connection_sync()

    def _connection_sync(self) -> sqlite3.Connection:
        if self._connection is None:
            conn = connect_sqlite(self._path)
            self._ensure_schema(conn)
            self._connection = conn
            self._maybe_migrate_legacy(conn)
        return self._connection

    def _close_sync(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topic_scopes (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            self._dedupe_topic_scopes(conn)
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_scopes_root
                    ON telegram_topic_scopes(chat_id)
                    WHERE thread_id IS NULL
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_scopes_thread
                    ON telegram_topic_scopes(chat_id, thread_id)
                    WHERE thread_id IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topics (
                    topic_key TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    workspace_path TEXT,
                    repo_id TEXT,
                    active_thread_id TEXT,
                    last_update_id INTEGER,
                    approval_mode TEXT,
                    last_active_at TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_topics_chat_thread
                    ON telegram_topics(chat_id, thread_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_topics_workspace
                    ON telegram_topics(workspace_path)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_topics_last_active
                    ON telegram_topics(last_active_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_pending_approvals (
                    request_id TEXT PRIMARY KEY,
                    topic_key TEXT,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_approvals_topic
                    ON telegram_pending_approvals(topic_key)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_approvals_expires
                    ON telegram_pending_approvals(expires_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_outbox (
                    record_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_attempt_at TEXT,
                    operation TEXT,
                    message_id INTEGER,
                    outbox_key TEXT,
                    payload_json TEXT NOT NULL
                )
                """
            )
            for col, col_type in [
                ("next_attempt_at", "TEXT"),
                ("operation", "TEXT"),
                ("message_id", "INTEGER"),
                ("outbox_key", "TEXT"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE telegram_outbox ADD COLUMN {col} {col_type}"
                    )
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_outbox_created
                    ON telegram_outbox(created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_outbox_key
                    ON telegram_outbox(outbox_key)
                    WHERE outbox_key IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_pending_voice (
                    record_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_attempt_at TEXT,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_voice_next_attempt
                    ON telegram_pending_voice(next_attempt_at)
                """
            )
            now = now_iso()
            self._set_meta(conn, "schema_version", str(TELEGRAM_SCHEMA_VERSION), now)
            self._set_meta(conn, "state_version", str(STATE_VERSION), now)

    def _dedupe_topic_scopes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            DELETE FROM telegram_topic_scopes
             WHERE thread_id IS NULL
               AND EXISTS (
                SELECT 1
                  FROM telegram_topic_scopes AS newer
                 WHERE newer.thread_id IS NULL
                   AND newer.chat_id = telegram_topic_scopes.chat_id
                   AND (
                    newer.updated_at > telegram_topic_scopes.updated_at
                    OR (
                        newer.updated_at = telegram_topic_scopes.updated_at
                        AND newer.rowid > telegram_topic_scopes.rowid
                    )
                   )
               )
            """
        )
        conn.execute(
            """
            DELETE FROM telegram_topic_scopes
             WHERE thread_id IS NOT NULL
               AND EXISTS (
                SELECT 1
                  FROM telegram_topic_scopes AS newer
                 WHERE newer.thread_id IS NOT NULL
                   AND newer.chat_id = telegram_topic_scopes.chat_id
                   AND newer.thread_id = telegram_topic_scopes.thread_id
                   AND (
                    newer.updated_at > telegram_topic_scopes.updated_at
                    OR (
                        newer.updated_at = telegram_topic_scopes.updated_at
                        AND newer.rowid > telegram_topic_scopes.rowid
                    )
                   )
               )
            """
        )

    def _has_persisted_rows(self, conn: sqlite3.Connection) -> bool:
        for table in (
            "telegram_topics",
            "telegram_topic_scopes",
            "telegram_pending_approvals",
            "telegram_outbox",
            "telegram_pending_voice",
        ):
            row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            if row is not None:
                return True
        if self._get_meta(conn, "last_update_id_global") is not None:
            return True
        return False

    def _load_legacy_state_json(self, path: Path) -> Optional[TelegramState]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        raw_version = payload.get("version")
        if isinstance(raw_version, int) and not isinstance(raw_version, bool):
            version = raw_version
        else:
            version = STATE_VERSION
        topics: dict[str, TelegramTopicRecord] = {}
        topics_payload = payload.get("topics")
        if isinstance(topics_payload, dict):
            for key, record_payload in topics_payload.items():
                if not isinstance(key, str) or not key:
                    continue
                record = TelegramTopicRecord.from_dict(
                    record_payload, default_approval_mode=self._default_approval_mode
                )
                if record is not None:
                    topics[key] = record
        topic_scopes: dict[str, str] = {}
        scopes_payload = payload.get("topic_scopes")
        if isinstance(scopes_payload, dict):
            for key, scope in scopes_payload.items():
                if not isinstance(key, str) or not key:
                    continue
                if isinstance(scope, str) and scope:
                    topic_scopes[key] = scope
        pending_approvals: dict[str, PendingApprovalRecord] = {}
        approvals_payload = payload.get("pending_approvals")
        if isinstance(approvals_payload, dict):
            for request_id, record_payload in approvals_payload.items():
                record = PendingApprovalRecord.from_dict(record_payload)
                if record is None:
                    continue
                key = record.request_id or request_id
                if key:
                    pending_approvals[key] = record
        outbox: dict[str, OutboxRecord] = {}
        outbox_payload = payload.get("outbox")
        if isinstance(outbox_payload, dict):
            for record_id, record_payload in outbox_payload.items():
                record = OutboxRecord.from_dict(record_payload)
                if record is None:
                    continue
                key = record.record_id or record_id
                if key:
                    outbox[key] = record
        pending_voice: dict[str, PendingVoiceRecord] = {}
        voice_payload = payload.get("pending_voice")
        if isinstance(voice_payload, dict):
            for record_id, record_payload in voice_payload.items():
                record = PendingVoiceRecord.from_dict(record_payload)
                if record is None:
                    continue
                key = record.record_id or record_id
                if key:
                    pending_voice[key] = record
        last_update_id_global = None
        raw_update_id = payload.get("last_update_id_global")
        if isinstance(raw_update_id, int) and not isinstance(raw_update_id, bool):
            last_update_id_global = raw_update_id
        return TelegramState(
            version=version,
            topics=topics,
            topic_scopes=topic_scopes,
            pending_approvals=pending_approvals,
            outbox=outbox,
            pending_voice=pending_voice,
            last_update_id_global=last_update_id_global,
        )

    def _maybe_migrate_legacy(self, conn: sqlite3.Connection) -> None:
        legacy_path = self._path.with_name("telegram_state.json")
        if not legacy_path.exists():
            return
        if self._has_persisted_rows(conn):
            return
        state = self._load_legacy_state_json(legacy_path)
        if state is None:
            return
        self._save_state_sync(state)

    def _set_meta(
        self, conn: sqlite3.Connection, key: str, value: str, updated_at: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO telegram_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, updated_at),
        )

    def _get_meta(self, conn: sqlite3.Connection, key: str) -> Optional[str]:
        row = conn.execute(
            "SELECT value FROM telegram_meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        value = row["value"]
        return value if isinstance(value, str) else None

    def _get_topic_scope_by_ids(
        self, conn: sqlite3.Connection, chat_id: int, thread_id: Optional[int]
    ) -> Optional[str]:
        clause, params = _thread_predicate(thread_id)
        row = conn.execute(
            f"SELECT scope FROM telegram_topic_scopes WHERE chat_id = ? AND {clause}",
            (chat_id, *params),
        ).fetchone()
        scope = row["scope"] if row else None
        if isinstance(scope, str) and scope:
            return scope
        return None

    def _upsert_topic(
        self,
        conn: sqlite3.Connection,
        key: str,
        record: TelegramTopicRecord,
        created_at: str,
        updated_at: str,
    ) -> None:
        try:
            chat_id, thread_id, scope = parse_topic_key(key)
        except ValueError:
            return
        approval_mode = normalize_approval_mode(
            record.approval_mode, default=self._default_approval_mode
        )
        last_update_id = record.last_update_id
        if not isinstance(last_update_id, int) or isinstance(last_update_id, bool):
            last_update_id = None
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        conn.execute(
            """
            INSERT INTO telegram_topics (
                topic_key,
                chat_id,
                thread_id,
                scope,
                workspace_path,
                repo_id,
                active_thread_id,
                last_update_id,
                approval_mode,
                last_active_at,
                payload_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(topic_key) DO UPDATE SET
                chat_id=excluded.chat_id,
                thread_id=excluded.thread_id,
                scope=excluded.scope,
                workspace_path=excluded.workspace_path,
                repo_id=excluded.repo_id,
                active_thread_id=excluded.active_thread_id,
                last_update_id=excluded.last_update_id,
                approval_mode=excluded.approval_mode,
                last_active_at=excluded.last_active_at,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                key,
                chat_id,
                thread_id,
                scope,
                record.workspace_path,
                record.repo_id,
                record.active_thread_id,
                last_update_id,
                approval_mode,
                record.last_active_at,
                payload_json,
                created_at,
                updated_at,
            ),
        )

    def _load_state_sync(self) -> TelegramState:
        conn = self._connection_sync()
        meta = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM telegram_meta")
        }
        version = STATE_VERSION
        raw_version = meta.get("state_version")
        if isinstance(raw_version, str):
            try:
                version = int(raw_version)
            except ValueError:
                version = STATE_VERSION
        last_update_id_global: Optional[int] = None
        raw_update_id = meta.get("last_update_id_global")
        if isinstance(raw_update_id, str):
            try:
                parsed = int(raw_update_id)
            except ValueError:
                parsed = None
            if parsed is not None:
                last_update_id_global = parsed
        topics: dict[str, TelegramTopicRecord] = {}
        for row in conn.execute("SELECT topic_key, payload_json FROM telegram_topics"):
            payload = _parse_json_payload(row["payload_json"])
            record = TelegramTopicRecord.from_dict(
                payload, default_approval_mode=self._default_approval_mode
            )
            topics[row["topic_key"]] = record
        topic_scopes: dict[str, str] = {}
        for row in conn.execute(
            "SELECT chat_id, thread_id, scope FROM telegram_topic_scopes"
        ):
            scope = row["scope"]
            if isinstance(scope, str) and scope:
                topic_scopes[topic_key(row["chat_id"], row["thread_id"])] = scope
        pending_approvals: dict[str, PendingApprovalRecord] = {}
        for row in conn.execute(
            "SELECT request_id, payload_json FROM telegram_pending_approvals"
        ):
            payload = _parse_json_payload(row["payload_json"])
            approval_record = PendingApprovalRecord.from_dict(payload)
            if approval_record is None:
                continue
            pending_approvals[row["request_id"]] = approval_record
        outbox: dict[str, OutboxRecord] = {}
        for row in conn.execute("SELECT record_id, payload_json FROM telegram_outbox"):
            payload = _parse_json_payload(row["payload_json"])
            outbox_record = OutboxRecord.from_dict(payload)
            if outbox_record is None:
                continue
            outbox[row["record_id"]] = outbox_record
        pending_voice: dict[str, PendingVoiceRecord] = {}
        for row in conn.execute(
            "SELECT record_id, payload_json FROM telegram_pending_voice"
        ):
            payload = _parse_json_payload(row["payload_json"])
            voice_record = PendingVoiceRecord.from_dict(payload)
            if voice_record is None:
                continue
            pending_voice[row["record_id"]] = voice_record
        return TelegramState(
            version=version,
            topics=topics,
            topic_scopes=topic_scopes,
            pending_approvals=pending_approvals,
            outbox=outbox,
            pending_voice=pending_voice,
            last_update_id_global=last_update_id_global,
        )

    def _save_state_sync(self, state: TelegramState) -> None:
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute("DELETE FROM telegram_topic_scopes")
            for key, scope in state.topic_scopes.items():
                try:
                    chat_id, thread_id, _scope = parse_topic_key(key)
                except ValueError:
                    continue
                if not isinstance(scope, str) or not scope:
                    continue
                conn.execute(
                    """
                    INSERT INTO telegram_topic_scopes (
                        chat_id,
                        thread_id,
                        scope,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (chat_id, thread_id, scope, now),
                )
            conn.execute("DELETE FROM telegram_topics")
            for key, record in state.topics.items():
                self._upsert_topic(conn, key, record, now, now)
            conn.execute("DELETE FROM telegram_pending_approvals")
            for record in state.pending_approvals.values():
                payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
                conn.execute(
                    """
                    INSERT INTO telegram_pending_approvals (
                        request_id,
                        topic_key,
                        chat_id,
                        thread_id,
                        created_at,
                        expires_at,
                        payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.request_id,
                        record.topic_key,
                        record.chat_id,
                        record.thread_id,
                        record.created_at,
                        None,
                        payload_json,
                    ),
                )
            conn.execute("DELETE FROM telegram_outbox")
            for record in state.outbox.values():
                payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
                conn.execute(
                    """
                    INSERT INTO telegram_outbox (
                        record_id,
                        chat_id,
                        thread_id,
                        created_at,
                        updated_at,
                        payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.record_id,
                        record.chat_id,
                        record.thread_id,
                        record.created_at,
                        now,
                        payload_json,
                    ),
                )
            conn.execute("DELETE FROM telegram_pending_voice")
            for record in state.pending_voice.values():
                payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
                conn.execute(
                    """
                    INSERT INTO telegram_pending_voice (
                        record_id,
                        chat_id,
                        thread_id,
                        created_at,
                        updated_at,
                        next_attempt_at,
                        payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.record_id,
                        record.chat_id,
                        record.thread_id,
                        record.created_at,
                        now,
                        record.next_attempt_at,
                        payload_json,
                    ),
                )
            self._set_meta(conn, "state_version", str(state.version), now)
            if state.last_update_id_global is not None:
                self._set_meta(
                    conn,
                    "last_update_id_global",
                    str(state.last_update_id_global),
                    now,
                )

    def _get_topic_sync(self, key: str) -> Optional[TelegramTopicRecord]:
        if not isinstance(key, str) or not key:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT payload_json FROM telegram_topics WHERE topic_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        payload = _parse_json_payload(row["payload_json"])
        return TelegramTopicRecord.from_dict(
            payload, default_approval_mode=self._default_approval_mode
        )

    def _get_topic_scope_sync(self, key: str) -> Optional[str]:
        if not isinstance(key, str) or not key:
            return None
        try:
            chat_id, thread_id, _scope = parse_topic_key(key)
        except ValueError:
            return None
        return self._get_topic_scope_by_ids(self._connection_sync(), chat_id, thread_id)

    def _set_topic_scope_sync(self, key: str, scope: Optional[str]) -> None:
        if not isinstance(key, str) or not key:
            return
        try:
            chat_id, thread_id, _scope = parse_topic_key(key)
        except ValueError:
            return
        conn = self._connection_sync()
        now = now_iso()
        clause, params = _thread_predicate(thread_id)
        with conn:
            if isinstance(scope, str) and scope:
                if thread_id is None:
                    conn.execute(
                        """
                        INSERT INTO telegram_topic_scopes (
                            chat_id,
                            thread_id,
                            scope,
                            updated_at
                        )
                        VALUES (?, NULL, ?, ?)
                        ON CONFLICT(chat_id) WHERE thread_id IS NULL
                        DO UPDATE SET
                            scope=excluded.scope,
                            updated_at=excluded.updated_at
                        """,
                        (chat_id, scope, now),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO telegram_topic_scopes (
                            chat_id,
                            thread_id,
                            scope,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(chat_id, thread_id) WHERE thread_id IS NOT NULL
                        DO UPDATE SET
                            scope=excluded.scope,
                            updated_at=excluded.updated_at
                        """,
                        (chat_id, thread_id, scope, now),
                    )
            else:
                conn.execute(
                    f"DELETE FROM telegram_topic_scopes WHERE chat_id = ? AND {clause}",
                    (chat_id, *params),
                )
            self._compact_scoped_topics(conn, key)

    async def _update_topic(
        self, key: str, apply: Callable[[TelegramTopicRecord], None]
    ) -> TelegramTopicRecord:
        return await self._run(self._update_topic_sync, key, apply)

    def _update_topic_sync(
        self, key: str, apply: Callable[[TelegramTopicRecord], None]
    ) -> TelegramTopicRecord:
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT payload_json, created_at FROM telegram_topics WHERE topic_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            record = TelegramTopicRecord(approval_mode=self._default_approval_mode)
            created_at = now_iso()
        else:
            payload = _parse_json_payload(row["payload_json"])
            record = TelegramTopicRecord.from_dict(
                payload, default_approval_mode=self._default_approval_mode
            )
            created_at = row["created_at"] if row["created_at"] else now_iso()
        apply(record)
        record.approval_mode = normalize_approval_mode(
            record.approval_mode, default=self._default_approval_mode
        )
        now = now_iso()
        record.last_active_at = now
        with conn:
            self._upsert_topic(conn, key, record, created_at, now)
        return record

    def _compact_scoped_topics(self, conn: sqlite3.Connection, base_key: str) -> None:
        base_key_normalized = _base_topic_key(base_key)
        if not base_key_normalized:
            return
        try:
            chat_id, thread_id, _scope = parse_topic_key(base_key_normalized)
        except ValueError:
            return
        scope = self._get_topic_scope_by_ids(conn, chat_id, thread_id)
        current_key = (
            topic_key(chat_id, thread_id, scope=scope)
            if isinstance(scope, str) and scope
            else base_key_normalized
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_SCOPED_TOPIC_DAYS)
        clause, params = _thread_predicate(thread_id)
        candidates: list[tuple[str, Optional[str], Optional[datetime]]] = []
        for row in conn.execute(
            f"""
            SELECT topic_key, active_thread_id, last_active_at
              FROM telegram_topics
             WHERE chat_id = ? AND {clause}
            """,
            (chat_id, *params),
        ):
            last_active = _parse_iso_timestamp(row["last_active_at"])
            candidates.append((row["topic_key"], row["active_thread_id"], last_active))
        if not candidates:
            return
        keys_to_remove: set[str] = set()
        for key, active_thread_id, last_active in candidates:
            if key == current_key or active_thread_id:
                continue
            if last_active is None or last_active < cutoff:
                keys_to_remove.add(key)
        remaining = [
            (key, active_thread_id, last_active)
            for key, active_thread_id, last_active in candidates
            if key not in keys_to_remove and key != current_key
        ]
        remaining.sort(
            key=lambda item: item[2] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        current_exists = {key for key, _active, _last in candidates}
        keep_limit = MAX_SCOPED_TOPICS_PER_BASE - (
            1 if current_key in current_exists else 0
        )
        keep_limit = max(0, keep_limit)
        for key, active_thread_id, _last_active in remaining[keep_limit:]:
            if active_thread_id:
                continue
            keys_to_remove.add(key)
        if keys_to_remove:
            conn.executemany(
                "DELETE FROM telegram_topics WHERE topic_key = ?",
                [(key,) for key in keys_to_remove],
            )

    def _find_active_thread_sync(
        self, thread_id: str, exclude_key: Optional[str]
    ) -> Optional[str]:
        conn = self._connection_sync()
        for row in conn.execute(
            """
            SELECT topic_key, chat_id, thread_id
              FROM telegram_topics
             WHERE active_thread_id = ?
            """,
            (thread_id,),
        ):
            key = row["topic_key"]
            if exclude_key and key == exclude_key:
                continue
            base_key = topic_key(row["chat_id"], row["thread_id"])
            scope = self._get_topic_scope_by_ids(conn, row["chat_id"], row["thread_id"])
            resolved_key = (
                topic_key(row["chat_id"], row["thread_id"], scope=scope)
                if isinstance(scope, str) and scope
                else base_key
            )
            if key == resolved_key:
                return key
        return None

    def _upsert_pending_approval_sync(
        self, record: PendingApprovalRecord
    ) -> PendingApprovalRecord:
        conn = self._connection_sync()
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        with conn:
            conn.execute(
                """
                INSERT INTO telegram_pending_approvals (
                    request_id,
                    topic_key,
                    chat_id,
                    thread_id,
                    created_at,
                    expires_at,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    topic_key=excluded.topic_key,
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at,
                    payload_json=excluded.payload_json
                """,
                (
                    record.request_id,
                    record.topic_key,
                    record.chat_id,
                    record.thread_id,
                    record.created_at,
                    None,
                    payload_json,
                ),
            )
        return record

    def _clear_pending_approval_sync(self, request_id: str) -> None:
        if not isinstance(request_id, str) or not request_id:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                "DELETE FROM telegram_pending_approvals WHERE request_id = ?",
                (request_id,),
            )

    def _pending_approvals_for_topic_sync(
        self, chat_id: int, thread_id: Optional[int]
    ) -> list[PendingApprovalRecord]:
        conn = self._connection_sync()
        clause, params = _thread_predicate(thread_id)
        pending: list[PendingApprovalRecord] = []
        for row in conn.execute(
            f"""
            SELECT payload_json
              FROM telegram_pending_approvals
             WHERE chat_id = ? AND {clause}
            """,
            (chat_id, *params),
        ):
            payload = _parse_json_payload(row["payload_json"])
            record = PendingApprovalRecord.from_dict(payload)
            if record is not None:
                pending.append(record)
        return pending

    def _clear_pending_approvals_for_topic_sync(
        self, chat_id: int, thread_id: Optional[int]
    ) -> None:
        conn = self._connection_sync()
        clause, params = _thread_predicate(thread_id)
        with conn:
            conn.execute(
                f"""
                DELETE FROM telegram_pending_approvals
                 WHERE chat_id = ? AND {clause}
                """,
                (chat_id, *params),
            )

    def _pending_approvals_for_key_sync(self, key: str) -> list[PendingApprovalRecord]:
        if not isinstance(key, str) or not key:
            return []
        try:
            chat_id, thread_id, scope = parse_topic_key(key)
        except (ValueError, KeyError) as exc:
            logger.debug("Failed to parse topic key '%s': %s", key, exc)
            return []
        conn = self._connection_sync()
        allow_legacy = False
        base_scope = self._get_topic_scope_by_ids(conn, chat_id, thread_id)
        if scope is None and base_scope is None:
            allow_legacy = True
        pending: list[PendingApprovalRecord] = []
        if allow_legacy:
            clause, params = _thread_predicate(thread_id)
            rows = conn.execute(
                f"""
                SELECT payload_json
                  FROM telegram_pending_approvals
                 WHERE topic_key = ?
                    OR (topic_key IS NULL AND chat_id = ? AND {clause})
                """,
                (key, chat_id, *params),
            )
        else:
            rows = conn.execute(
                """
                SELECT payload_json
                  FROM telegram_pending_approvals
                 WHERE topic_key = ?
                """,
                (key,),
            )
        for row in rows:
            payload = _parse_json_payload(row["payload_json"])
            record = PendingApprovalRecord.from_dict(payload)
            if record is not None:
                pending.append(record)
        return pending

    def _clear_pending_approvals_for_key_sync(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            return
        try:
            chat_id, thread_id, scope = parse_topic_key(key)
        except (ValueError, KeyError) as exc:
            logger.debug("Failed to parse topic key '%s': %s", key, exc)
            return
        conn = self._connection_sync()
        base_scope = self._get_topic_scope_by_ids(conn, chat_id, thread_id)
        allow_legacy = scope is None and base_scope is None
        with conn:
            if allow_legacy:
                clause, params = _thread_predicate(thread_id)
                conn.execute(
                    f"""
                    DELETE FROM telegram_pending_approvals
                     WHERE topic_key = ?
                        OR (topic_key IS NULL AND chat_id = ? AND {clause})
                    """,
                    (key, chat_id, *params),
                )
            else:
                conn.execute(
                    "DELETE FROM telegram_pending_approvals WHERE topic_key = ?",
                    (key,),
                )

    def _upsert_outbox_sync(self, record: OutboxRecord) -> OutboxRecord:
        conn = self._connection_sync()
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        updated_at = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO telegram_outbox (
                    record_id,
                    chat_id,
                    thread_id,
                    created_at,
                    updated_at,
                    next_attempt_at,
                    operation,
                    message_id,
                    outbox_key,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    next_attempt_at=excluded.next_attempt_at,
                    operation=excluded.operation,
                    message_id=excluded.message_id,
                    outbox_key=excluded.outbox_key,
                    payload_json=excluded.payload_json
                """,
                (
                    record.record_id,
                    record.chat_id,
                    record.thread_id,
                    record.created_at,
                    updated_at,
                    record.next_attempt_at,
                    record.operation,
                    record.message_id,
                    record.outbox_key,
                    payload_json,
                ),
            )
        return record

    def _delete_outbox_sync(self, record_id: str) -> None:
        if not isinstance(record_id, str) or not record_id:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                "DELETE FROM telegram_outbox WHERE record_id = ?",
                (record_id,),
            )

    def _get_outbox_sync(self, record_id: str) -> Optional[OutboxRecord]:
        if not isinstance(record_id, str) or not record_id:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT payload_json FROM telegram_outbox WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _parse_json_payload(row["payload_json"])
        return OutboxRecord.from_dict(payload)

    def _list_outbox_sync(self) -> list[OutboxRecord]:
        conn = self._connection_sync()
        records: list[OutboxRecord] = []
        for row in conn.execute(
            "SELECT payload_json FROM telegram_outbox ORDER BY created_at"
        ):
            payload = _parse_json_payload(row["payload_json"])
            record = OutboxRecord.from_dict(payload)
            if record is not None:
                records.append(record)
        return records

    def _upsert_pending_voice_sync(
        self, record: PendingVoiceRecord
    ) -> PendingVoiceRecord:
        conn = self._connection_sync()
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        updated_at = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO telegram_pending_voice (
                    record_id,
                    chat_id,
                    thread_id,
                    created_at,
                    updated_at,
                    next_attempt_at,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    next_attempt_at=excluded.next_attempt_at,
                    payload_json=excluded.payload_json
                """,
                (
                    record.record_id,
                    record.chat_id,
                    record.thread_id,
                    record.created_at,
                    updated_at,
                    record.next_attempt_at,
                    payload_json,
                ),
            )
        return record

    def _delete_pending_voice_sync(self, record_id: str) -> None:
        if not isinstance(record_id, str) or not record_id:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                "DELETE FROM telegram_pending_voice WHERE record_id = ?",
                (record_id,),
            )

    def _get_pending_voice_sync(self, record_id: str) -> Optional[PendingVoiceRecord]:
        if not isinstance(record_id, str) or not record_id:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT payload_json FROM telegram_pending_voice WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _parse_json_payload(row["payload_json"])
        return PendingVoiceRecord.from_dict(payload)

    def _list_pending_voice_sync(self) -> list[PendingVoiceRecord]:
        conn = self._connection_sync()
        records: list[PendingVoiceRecord] = []
        for row in conn.execute(
            "SELECT payload_json FROM telegram_pending_voice ORDER BY created_at"
        ):
            payload = _parse_json_payload(row["payload_json"])
            record = PendingVoiceRecord.from_dict(payload)
            if record is not None:
                records.append(record)
        return records

    def _get_last_update_id_global_sync(self) -> Optional[int]:
        conn = self._connection_sync()
        value = self._get_meta(conn, "last_update_id_global")
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed

    def _update_last_update_id_global_sync(self, update_id: int) -> Optional[int]:
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return None
        conn = self._connection_sync()
        current = self._get_last_update_id_global_sync()
        if current is None or update_id > current:
            with conn:
                self._set_meta(conn, "last_update_id_global", str(update_id), now_iso())
            return update_id
        return current
