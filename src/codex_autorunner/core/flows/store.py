from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, cast

from ..sqlite_utils import SQLITE_PRAGMAS, SQLITE_PRAGMAS_DURABLE
from ..time_utils import now_iso
from .models import (
    FlowArtifact,
    FlowEvent,
    FlowEventType,
    FlowRunRecord,
    FlowRunStatus,
)

_logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
UNSET = object()
_COMPACT_APP_SERVER_METHODS = frozenset(
    {"message.part.updated", "message.updated", "session.diff"}
)
_APP_SERVER_PREVIEW_CHARS = 2048


def _coerce_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_non_empty_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _truncate_text(value: Any, limit: int = _APP_SERVER_PREVIEW_CHARS) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if len(value) <= limit:
        return value
    return value[:limit]


def _first_non_empty_str(*values: Any) -> Optional[str]:
    for value in values:
        text = _coerce_non_empty_str(value)
        if text is not None:
            return text
    return None


def _scalar_summary(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _pick_summary_fields(
    source: Dict[str, Any], keys: tuple[str, ...]
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key in keys:
        if key not in source:
            continue
        value = _scalar_summary(source.get(key))
        if value is not None:
            summary[key] = value
    return summary


def _compact_error_summary(value: Any) -> Optional[Dict[str, Any]]:
    err = _coerce_dict(value)
    if not err:
        return None
    summary = _pick_summary_fields(
        err, ("message", "error", "additionalDetails", "details")
    )
    return summary or None


def _compact_part_state_summary(value: Any) -> Optional[Dict[str, Any]]:
    state = _coerce_dict(value)
    if not state:
        return None
    summary = _pick_summary_fields(state, ("status", "exitCode", "exit_code", "reason"))
    error_summary = _compact_error_summary(state.get("error"))
    if error_summary:
        summary["error"] = error_summary
    return summary or None


def _compact_part_summary(
    part: Dict[str, Any], preview: Optional[str]
) -> Dict[str, Any]:
    summary = _pick_summary_fields(
        part,
        (
            "id",
            "sessionID",
            "sessionId",
            "messageID",
            "messageId",
            "message_id",
            "type",
            "tool",
            "name",
            "callID",
            "path",
            "file",
            "hash",
            "reason",
            "snapshot",
        ),
    )
    for key in ("input", "command", "cmd", "script"):
        text = _truncate_text(part.get(key))
        if text:
            summary[key] = text
            break
    if preview and str(summary.get("type") or "").strip().lower() in {
        "",
        "text",
        "reasoning",
    }:
        summary["text"] = preview
    state_summary = _compact_part_state_summary(part.get("state"))
    if state_summary:
        summary["state"] = state_summary
    files = part.get("files")
    if isinstance(files, list):
        compact_files: list[Dict[str, Any] | str] = []
        for entry in files[:10]:
            if isinstance(entry, str):
                compact_files.append(entry)
            elif isinstance(entry, dict):
                compact_entry = _pick_summary_fields(
                    entry, ("path", "file", "name", "status")
                )
                if compact_entry:
                    compact_files.append(compact_entry)
        if compact_files:
            summary["files"] = compact_files
    return summary


def _compact_info_summary(info: Dict[str, Any]) -> Dict[str, Any]:
    return _pick_summary_fields(
        info,
        (
            "id",
            "sessionID",
            "sessionId",
            "role",
            "finish",
            "agent",
            "providerID",
            "modelID",
            "mode",
        ),
    )


def _extract_preview_from_message(method: str, params: Dict[str, Any]) -> Optional[str]:
    properties = _coerce_dict(params.get("properties"))
    part = _coerce_dict(properties.get("part")) or _coerce_dict(params.get("part"))
    if method == "message.part.updated":
        part_type = str(part.get("type") or "").strip().lower()
        if part_type in {"", "text", "reasoning"}:
            return _truncate_text(
                params.get("delta")
                or params.get("text")
                or params.get("output")
                or properties.get("delta")
                or part.get("text")
            )
        tool_preview = _first_non_empty_str(
            part.get("command"),
            part.get("input"),
            part.get("cmd"),
            part.get("script"),
        )
        if tool_preview:
            return _truncate_text(tool_preview)
        args = (
            _coerce_dict(part.get("args"))
            or _coerce_dict(part.get("arguments"))
            or _coerce_dict(part.get("params"))
        )
        return _truncate_text(
            args.get("command")
            or args.get("input")
            or args.get("cmd")
            or args.get("script")
        )
    if method == "message.updated":
        info = _coerce_dict(properties.get("info"))
        summary = _coerce_dict(info.get("summary"))
        return _truncate_text(
            summary.get("title") or params.get("message") or params.get("status")
        )
    if method == "session.diff":
        diff = properties.get("diff")
        if isinstance(diff, list):
            return f"{len(diff)} diff entries"
        return _truncate_text(params.get("message") or params.get("status"))
    return None


def _build_compact_app_server_params(
    method: str,
    params: Dict[str, Any],
    preview: Optional[str],
) -> Dict[str, Any]:
    summary = _pick_summary_fields(
        params,
        (
            "turn_id",
            "turnId",
            "itemId",
            "status",
            "message",
            "role",
        ),
    )
    properties = _coerce_dict(params.get("properties"))
    info = _coerce_dict(properties.get("info"))
    part = _coerce_dict(properties.get("part")) or _coerce_dict(params.get("part"))
    properties_summary: Dict[str, Any] = {}
    info_summary = _compact_info_summary(info)
    if info_summary:
        properties_summary["info"] = info_summary
    if part:
        part_summary = _compact_part_summary(part, preview)
        if part_summary:
            properties_summary["part"] = part_summary
    session_id = _first_non_empty_str(
        properties.get("sessionID"),
        properties.get("sessionId"),
    )
    if session_id:
        properties_summary["sessionID"] = session_id
    if method == "session.diff":
        diff = properties.get("diff")
        if isinstance(diff, list):
            properties_summary["diff_count"] = len(diff)
        if preview and "status" not in summary:
            summary["status"] = "diff updated"
    delta_preview = None
    if method == "message.part.updated":
        delta_preview = _truncate_text(
            params.get("delta") or params.get("text") or params.get("output")
        )
    if delta_preview:
        summary["delta"] = delta_preview
    if properties_summary:
        summary["properties"] = properties_summary
    error_summary = _compact_error_summary(params.get("error"))
    if error_summary:
        summary["error"] = error_summary
    return summary


def _compact_app_server_event_data(data: Dict[str, Any]) -> Dict[str, Any]:
    if data.get("truncated") is True:
        return dict(data)

    message = _coerce_dict(data.get("message"))
    method = _coerce_non_empty_str(message.get("method"))
    if method is None or method not in _COMPACT_APP_SERVER_METHODS:
        return dict(data)

    params = _coerce_dict(message.get("params"))
    properties = _coerce_dict(params.get("properties"))
    info = _coerce_dict(properties.get("info"))
    part = _coerce_dict(properties.get("part")) or _coerce_dict(params.get("part"))
    preview = _extract_preview_from_message(method, params)
    raw_payload = json.dumps(data, ensure_ascii=False)
    compact_data: Dict[str, Any] = {
        "method": method,
        "message": {
            "method": method,
            "params": _build_compact_app_server_params(method, params, preview),
        },
        "payload_bytes": len(raw_payload.encode("utf-8")),
        "truncated": True,
    }
    for key in ("id", "received_at", "receivedAt"):
        value = _scalar_summary(data.get(key))
        if value is not None:
            compact_data[key] = value
    turn_id = _first_non_empty_str(
        data.get("turn_id"),
        params.get("turn_id"),
        params.get("turnId"),
    )
    if turn_id:
        compact_data["turn_id"] = turn_id
    thread_id = _first_non_empty_str(
        data.get("thread_id"),
        data.get("threadId"),
        params.get("thread_id"),
        params.get("threadId"),
        info.get("sessionID"),
        info.get("sessionId"),
        part.get("sessionID"),
        part.get("sessionId"),
        properties.get("sessionID"),
        properties.get("sessionId"),
    )
    if thread_id:
        compact_data["thread_id"] = thread_id
    message_id = _first_non_empty_str(
        data.get("message_id"),
        info.get("id"),
        part.get("messageID"),
        part.get("messageId"),
        part.get("message_id"),
    )
    if message_id:
        compact_data["message_id"] = message_id
    part_id = _first_non_empty_str(data.get("part_id"), part.get("id"))
    if part_id:
        compact_data["part_id"] = part_id
    role = _first_non_empty_str(data.get("role"), info.get("role"), params.get("role"))
    if role:
        compact_data["role"] = role
    tool = _first_non_empty_str(data.get("tool"), part.get("tool"), part.get("name"))
    if tool:
        compact_data["tool"] = tool
    status = _first_non_empty_str(
        data.get("status"),
        params.get("status"),
        _coerce_dict(part.get("state")).get("status"),
        info.get("finish"),
    )
    if status:
        compact_data["status"] = status
    if preview:
        compact_data["preview"] = preview
    return compact_data


def _normalize_event_data(
    event_type: FlowEventType,
    data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    normalized = dict(data or {})
    if event_type == FlowEventType.APP_SERVER_EVENT:
        return _compact_app_server_event_data(normalized)
    return normalized


class FlowStore:
    def __init__(self, db_path: Path, durable: bool = False):
        self.db_path = db_path
        self._durable = durable
        self._local: threading.local = threading.local()

    def __enter__(self) -> FlowStore:
        self.initialize()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            # Ensure parent directory exists so sqlite can create/open file.
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                # Let sqlite raise a clearer error below if directory creation failed.
                pass
            self._local.conn = sqlite3.connect(
                self.db_path, check_same_thread=False, isolation_level=None
            )
            self._local.conn.row_factory = sqlite3.Row
            pragmas = SQLITE_PRAGMAS_DURABLE if self._durable else SQLITE_PRAGMAS
            for pragma in pragmas:
                self._local.conn.execute(pragma)
        return cast(sqlite3.Connection, self._local.conn)

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def initialize(self) -> None:
        with self.transaction() as conn:
            self._create_schema(conn)
            self._ensure_schema_version(conn)

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_info (
                version INTEGER NOT NULL PRIMARY KEY
            )
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS flow_runs (
                id TEXT PRIMARY KEY,
                flow_type TEXT NOT NULL,
                status TEXT NOT NULL,
                input_data TEXT NOT NULL,
                state TEXT NOT NULL,
                current_step TEXT,
                stop_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error_message TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS flow_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                data TEXT NOT NULL,
                step_id TEXT,
                FOREIGN KEY (run_id) REFERENCES flow_runs(id) ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS flow_artifacts (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (run_id) REFERENCES flow_runs(id) ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_flow_runs_status ON flow_runs(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_flow_events_run_id ON flow_events(run_id, seq)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_flow_artifacts_run_id ON flow_artifacts(run_id)"
        )

    def _ensure_schema_version(self, conn: sqlite3.Connection) -> None:
        result = conn.execute("SELECT version FROM schema_info").fetchone()
        if result is None:
            conn.execute(
                "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
            )
        else:
            current_version = result[0]
            if current_version < SCHEMA_VERSION:
                self._migrate_schema(conn, current_version, SCHEMA_VERSION)

    def _migrate_schema(
        self, conn: sqlite3.Connection, from_version: int, to_version: int
    ) -> None:
        _logger.info("Migrating schema from version %d to %d", from_version, to_version)
        for version in range(from_version, to_version):
            self._apply_migration(conn, version + 1)
        conn.execute("UPDATE schema_info SET version = ?", (to_version,))

    def _apply_migration(self, conn: sqlite3.Connection, version: int) -> None:
        if version == 1:
            pass
        elif version == 2:
            conn.execute("ALTER TABLE flow_events RENAME TO flow_events_old")
            conn.execute(
                """
                CREATE TABLE flow_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    data TEXT NOT NULL,
                    step_id TEXT,
                    FOREIGN KEY (run_id) REFERENCES flow_runs(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                INSERT INTO flow_events (id, run_id, event_type, timestamp, data, step_id)
                SELECT id, run_id, event_type, timestamp, data, step_id
                FROM flow_events_old
                ORDER BY timestamp ASC
                """
            )
            conn.execute("DROP TABLE flow_events_old")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_flow_events_run_id ON flow_events(run_id, seq)"
            )

    def create_flow_run(
        self,
        run_id: str,
        flow_type: str,
        input_data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
        state: Optional[Dict[str, Any]] = None,
        current_step: Optional[str] = None,
    ) -> FlowRunRecord:
        now = now_iso()
        record = FlowRunRecord(
            id=run_id,
            flow_type=flow_type,
            status=FlowRunStatus.PENDING,
            input_data=input_data,
            state=state or {},
            current_step=current_step,
            stop_requested=False,
            created_at=now,
            metadata=metadata or {},
        )

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO flow_runs (
                    id, flow_type, status, input_data, state, current_step,
                    stop_requested, created_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.flow_type,
                    record.status.value,
                    json.dumps(record.input_data),
                    json.dumps(record.state),
                    record.current_step,
                    1 if record.stop_requested else 0,
                    record.created_at,
                    json.dumps(record.metadata),
                ),
            )

        return record

    def get_flow_run(self, run_id: str) -> Optional[FlowRunRecord]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM flow_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_flow_run(row)

    def update_flow_run_status(
        self,
        run_id: str,
        status: FlowRunStatus,
        current_step: Any = UNSET,
        state: Any = UNSET,
        started_at: Any = UNSET,
        finished_at: Any = UNSET,
        error_message: Any = UNSET,
    ) -> Optional[FlowRunRecord]:
        updates = ["status = ?"]
        params: List[Any] = [status.value]

        if current_step is not UNSET:
            updates.append("current_step = ?")
            params.append(current_step)

        if state is not UNSET:
            updates.append("state = ?")
            params.append(json.dumps(state))

        if started_at is not UNSET:
            updates.append("started_at = ?")
            params.append(started_at)

        if finished_at is not UNSET:
            updates.append("finished_at = ?")
            params.append(finished_at)

        if error_message is not UNSET:
            updates.append("error_message = ?")
            params.append(error_message)

        params.append(run_id)

        with self.transaction() as conn:
            conn.execute(
                f"UPDATE flow_runs SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            row = conn.execute(
                "SELECT * FROM flow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_flow_run(row)

    def set_stop_requested(
        self, run_id: str, stop_requested: bool
    ) -> Optional[FlowRunRecord]:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE flow_runs SET stop_requested = ? WHERE id = ?",
                (1 if stop_requested else 0, run_id),
            )
            row = conn.execute(
                "SELECT * FROM flow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_flow_run(row)

    def update_current_step(
        self, run_id: str, current_step: str
    ) -> Optional[FlowRunRecord]:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE flow_runs SET current_step = ? WHERE id = ?",
                (current_step, run_id),
            )
            row = conn.execute(
                "SELECT * FROM flow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_flow_run(row)

    def list_flow_runs(
        self, flow_type: Optional[str] = None, status: Optional[FlowRunStatus] = None
    ) -> List[FlowRunRecord]:
        conn = self._get_conn()
        query = "SELECT * FROM flow_runs WHERE 1=1"
        params: List[Any] = []

        if flow_type is not None:
            query += " AND flow_type = ?"
            params.append(flow_type)

        if status is not None:
            query += " AND status = ?"
            params.append(status.value)

        query += " ORDER BY created_at DESC, rowid DESC"

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_flow_run(row) for row in rows]

    def get_latest_flow_run(
        self, flow_type: Optional[str] = None, status: Optional[FlowRunStatus] = None
    ) -> Optional[FlowRunRecord]:
        conn = self._get_conn()
        query = "SELECT * FROM flow_runs WHERE 1=1"
        params: List[Any] = []

        if flow_type is not None:
            query += " AND flow_type = ?"
            params.append(flow_type)

        if status is not None:
            query += " AND status = ?"
            params.append(status.value)

        query += " ORDER BY created_at DESC, rowid DESC LIMIT 1"

        row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        return self._row_to_flow_run(row)

    def list_paused_runs_for_supersession(
        self, flow_type: str, exclude_run_id: str
    ) -> List[FlowRunRecord]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM flow_runs
            WHERE flow_type = ?
              AND status = ?
              AND id != ?
            ORDER BY created_at DESC
            """,
            (flow_type, FlowRunStatus.PAUSED.value, exclude_run_id),
        ).fetchall()
        return [self._row_to_flow_run(row) for row in rows]

    def mark_run_superseded(
        self, run_id: str, superseded_by: str
    ) -> Optional[FlowRunRecord]:
        now = now_iso()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT metadata FROM flow_runs WHERE id = ? AND status = ?",
                (run_id, FlowRunStatus.PAUSED.value),
            ).fetchone()
            if existing is None:
                return None
            try:
                metadata = json.loads(existing["metadata"] or "{}")
            except Exception:
                metadata = {}
            metadata = dict(metadata)
            metadata["superseded_by"] = superseded_by
            metadata["superseded_at"] = now
            cursor = conn.execute(
                """
                UPDATE flow_runs
                SET status = ?, metadata = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    FlowRunStatus.SUPERSEDED.value,
                    json.dumps(metadata),
                    now,
                    run_id,
                    FlowRunStatus.PAUSED.value,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM flow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_flow_run(row)

    def create_event(
        self,
        event_id: str,
        run_id: str,
        event_type: FlowEventType,
        data: Optional[Dict[str, Any]] = None,
        step_id: Optional[str] = None,
    ) -> FlowEvent:
        timestamp = now_iso()
        normalized_data = _normalize_event_data(event_type, data)

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO flow_events (id, run_id, event_type, timestamp, data, step_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    event_type.value,
                    timestamp,
                    json.dumps(normalized_data),
                    step_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM flow_events WHERE id = ?", (event_id,)
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to persist flow event")

        return self._row_to_flow_event(row)

    def get_events(
        self,
        run_id: str,
        after_seq: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[FlowEvent]:
        conn = self._get_conn()
        query = "SELECT * FROM flow_events WHERE run_id = ?"
        params: List[Any] = [run_id]

        if after_seq is not None:
            query += " AND seq > ?"
            params.append(after_seq)

        query += " ORDER BY seq ASC"

        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_flow_event(row) for row in rows]

    def get_events_by_types(
        self,
        run_id: str,
        event_types: list[FlowEventType],
        *,
        after_seq: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[FlowEvent]:
        """Return events for a run filtered to specific event types."""
        if not event_types:
            return []
        conn = self._get_conn()
        placeholders = ", ".join("?" for _ in event_types)
        query = f"""
            SELECT *
            FROM flow_events
            WHERE run_id = ? AND event_type IN ({placeholders})
        """
        params: List[Any] = [run_id, *[t.value for t in event_types]]

        if after_seq is not None:
            query += " AND seq > ?"
            params.append(after_seq)

        query += " ORDER BY seq ASC"

        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_flow_event(row) for row in rows]

    def get_events_by_type(
        self,
        run_id: str,
        event_type: FlowEventType,
        *,
        after_seq: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[FlowEvent]:
        return self.get_events_by_types(
            run_id, [event_type], after_seq=after_seq, limit=limit
        )

    def get_last_event_meta(self, run_id: str) -> tuple[Optional[int], Optional[str]]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT seq, timestamp FROM flow_events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return None, None
        return row["seq"], row["timestamp"]

    def get_last_event_seq_by_types(
        self, run_id: str, event_types: list[FlowEventType]
    ) -> Optional[int]:
        if not event_types:
            return None
        conn = self._get_conn()
        placeholders = ", ".join("?" for _ in event_types)
        params = [run_id, *[t.value for t in event_types]]
        row = conn.execute(
            f"""
            SELECT seq
            FROM flow_events
            WHERE run_id = ? AND event_type IN ({placeholders})
            ORDER BY seq DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return None
        return cast(int, row["seq"])

    def get_last_event_by_type(
        self, run_id: str, event_type: FlowEventType
    ) -> Optional[FlowEvent]:
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT *
            FROM flow_events
            WHERE run_id = ? AND event_type = ?
            ORDER BY seq DESC
            LIMIT 1
            """,
            (run_id, event_type.value),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_flow_event(row)

    def get_latest_step_progress_current_ticket(
        self, run_id: str, *, after_seq: Optional[int] = None, limit: int = 50
    ) -> Optional[str]:
        """Return the most recent step_progress.data.current_ticket for a run.

        This is intentionally lightweight to support UI polling endpoints.
        """
        conn = self._get_conn()
        query = """
            SELECT seq, data
            FROM flow_events
            WHERE run_id = ? AND event_type = ?
        """
        params: List[Any] = [run_id, FlowEventType.STEP_PROGRESS.value]
        if after_seq is not None:
            query += " AND seq > ?"
            params.append(after_seq)
        query += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        for row in rows:
            try:
                data = json.loads(row["data"] or "{}")
            except Exception:
                data = {}
            current_ticket = data.get("current_ticket")
            if isinstance(current_ticket, str) and current_ticket.strip():
                return current_ticket.strip()
        return None

    def create_artifact(
        self,
        artifact_id: str,
        run_id: str,
        kind: str,
        path: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FlowArtifact:
        artifact = FlowArtifact(
            id=artifact_id,
            run_id=run_id,
            kind=kind,
            path=path,
            created_at=now_iso(),
            metadata=metadata or {},
        )

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO flow_artifacts (id, run_id, kind, path, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    artifact.run_id,
                    artifact.kind,
                    artifact.path,
                    artifact.created_at,
                    json.dumps(artifact.metadata),
                ),
            )

        return artifact

    def get_artifacts(self, run_id: str) -> List[FlowArtifact]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM flow_artifacts WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        ).fetchall()
        return [self._row_to_flow_artifact(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> Optional[FlowArtifact]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM flow_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_flow_artifact(row)

    def delete_flow_run(self, run_id: str) -> bool:
        """Delete a flow run and its events/artifacts (cascading)."""
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM flow_runs WHERE id = ?", (run_id,))
            return cursor.rowcount > 0

    def _row_to_flow_run(self, row: sqlite3.Row) -> FlowRunRecord:
        return FlowRunRecord(
            id=row["id"],
            flow_type=row["flow_type"],
            status=FlowRunStatus(row["status"]),
            input_data=json.loads(row["input_data"]),
            state=json.loads(row["state"]),
            current_step=row["current_step"],
            stop_requested=bool(row["stop_requested"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_message=row["error_message"],
            metadata=json.loads(row["metadata"]),
        )

    def _row_to_flow_event(self, row: sqlite3.Row) -> FlowEvent:
        return FlowEvent(
            seq=row["seq"],
            id=row["id"],
            run_id=row["run_id"],
            event_type=FlowEventType(row["event_type"]),
            timestamp=row["timestamp"],
            data=json.loads(row["data"]),
            step_id=row["step_id"],
        )

    def _row_to_flow_artifact(self, row: sqlite3.Row) -> FlowArtifact:
        return FlowArtifact(
            id=row["id"],
            run_id=row["run_id"],
            kind=row["kind"],
            path=row["path"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata"]),
        )

    def close(self) -> None:
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            del self._local.conn
