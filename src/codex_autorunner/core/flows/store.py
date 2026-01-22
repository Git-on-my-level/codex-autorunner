import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, cast

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


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FlowStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local: threading.local = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                self.db_path, check_same_thread=False, isolation_level=None
            )
            self._local.conn.row_factory = sqlite3.Row
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

        query += " ORDER BY created_at DESC"

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_flow_run(row) for row in rows]

    def create_event(
        self,
        event_id: str,
        run_id: str,
        event_type: FlowEventType,
        data: Optional[Dict[str, Any]] = None,
        step_id: Optional[str] = None,
    ) -> FlowEvent:
        timestamp = now_iso()

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
                    json.dumps(data or {}),
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
