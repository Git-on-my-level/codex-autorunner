from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .locks import file_lock
from .managed_thread_status import (
    ManagedThreadStatusReason,
    ManagedThreadStatusSnapshot,
    backfill_managed_thread_status,
    build_managed_thread_status_snapshot,
    transition_managed_thread_status,
)
from .sqlite_utils import open_sqlite
from .time_utils import now_iso

PMA_THREADS_DB_FILENAME = "threads.sqlite3"


class ManagedThreadAlreadyHasRunningTurnError(RuntimeError):
    def __init__(self, managed_thread_id: str) -> None:
        super().__init__(
            f"Managed thread '{managed_thread_id}' already has a running turn"
        )
        self.managed_thread_id = managed_thread_id


class ManagedThreadNotActiveError(RuntimeError):
    def __init__(self, managed_thread_id: str, status: Optional[str]) -> None:
        detail = (
            f"Managed thread '{managed_thread_id}' is not active"
            if not status
            else f"Managed thread '{managed_thread_id}' is not active (status={status})"
        )
        super().__init__(detail)
        self.managed_thread_id = managed_thread_id
        self.status = status


def default_pma_threads_db_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_THREADS_DB_FILENAME


def pma_threads_db_lock_path(db_path: Path) -> Path:
    return db_path.with_suffix(db_path.suffix + ".lock")


@contextmanager
def pma_threads_db_lock(db_path: Path) -> Iterator[None]:
    with file_lock(pma_threads_db_lock_path(db_path)):
        yield


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _table_columns(conn: Any, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    columns: set[str] = set()
    for row in rows:
        name = row["name"] if "name" in row.keys() else None
        if isinstance(name, str) and name:
            columns.add(name)
    return columns


def _coerce_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _latest_turn_for_thread(
    conn: Any, managed_thread_id: str
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT *
          FROM pma_managed_turns
         WHERE managed_thread_id = ?
         ORDER BY started_at DESC, rowid DESC
         LIMIT 1
        """,
        (managed_thread_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def _normalize_thread_record(row: Any) -> dict[str, Any]:
    record = _row_to_dict(row)
    lifecycle_status = _coerce_text(record.get("status")) or "active"
    snapshot = ManagedThreadStatusSnapshot.from_mapping(record)
    record["status"] = lifecycle_status
    record["lifecycle_status"] = lifecycle_status
    record["normalized_status"] = snapshot.status
    record["status_reason_code"] = snapshot.reason_code
    record["status_reason"] = snapshot.reason_code
    record["status_updated_at"] = snapshot.changed_at
    record["status_changed_at"] = snapshot.changed_at
    record["status_terminal"] = bool(snapshot.terminal)
    record["status_turn_id"] = snapshot.turn_id
    return record


def _ensure_schema(conn: Any) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pma_managed_threads (
                managed_thread_id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                repo_id TEXT,
                workspace_root TEXT NOT NULL,
                name TEXT,
                backend_thread_id TEXT,
                status TEXT NOT NULL,
                normalized_status TEXT,
                status_reason_code TEXT,
                status_updated_at TEXT,
                status_terminal INTEGER,
                status_turn_id TEXT,
                last_turn_id TEXT,
                last_message_preview TEXT,
                compact_seed TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pma_managed_turns (
                managed_turn_id TEXT PRIMARY KEY,
                managed_thread_id TEXT NOT NULL,
                client_turn_id TEXT,
                backend_turn_id TEXT,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                assistant_text TEXT,
                transcript_turn_id TEXT,
                model TEXT,
                reasoning TEXT,
                error TEXT,
                started_at TEXT,
                finished_at TEXT,
                FOREIGN KEY (managed_thread_id)
                    REFERENCES pma_managed_threads(managed_thread_id)
                    ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pma_managed_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                managed_thread_id TEXT,
                action_type TEXT NOT NULL,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (managed_thread_id)
                    REFERENCES pma_managed_threads(managed_thread_id)
                    ON DELETE SET NULL
            )
            """
        )

    thread_columns = _table_columns(conn, "pma_managed_threads")
    for statement in (
        (
            "normalized_status",
            "ALTER TABLE pma_managed_threads ADD COLUMN normalized_status TEXT",
        ),
        (
            "status_reason_code",
            "ALTER TABLE pma_managed_threads ADD COLUMN status_reason_code TEXT",
        ),
        (
            "status_updated_at",
            "ALTER TABLE pma_managed_threads ADD COLUMN status_updated_at TEXT",
        ),
        (
            "status_terminal",
            "ALTER TABLE pma_managed_threads ADD COLUMN status_terminal INTEGER",
        ),
        (
            "status_turn_id",
            "ALTER TABLE pma_managed_threads ADD COLUMN status_turn_id TEXT",
        ),
    ):
        if statement[0] not in thread_columns:
            with conn:
                conn.execute(statement[1])
    with conn:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pma_managed_threads_status
            ON pma_managed_threads(status)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pma_managed_threads_normalized_status
            ON pma_managed_threads(normalized_status)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pma_managed_threads_agent
            ON pma_managed_threads(agent)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pma_managed_threads_repo_id
            ON pma_managed_threads(repo_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pma_managed_turns_thread_started
            ON pma_managed_turns(managed_thread_id, started_at)
            """
        )

    _backfill_missing_thread_status(conn)


def _backfill_missing_thread_status(conn: Any) -> None:
    rows = conn.execute(
        """
        SELECT *
          FROM pma_managed_threads
         WHERE normalized_status IS NULL
            OR TRIM(COALESCE(normalized_status, '')) = ''
            OR status_reason_code IS NULL
            OR TRIM(COALESCE(status_reason_code, '')) = ''
            OR status_updated_at IS NULL
            OR TRIM(COALESCE(status_updated_at, '')) = ''
            OR status_terminal IS NULL
        """
    ).fetchall()
    if not rows:
        return

    with conn:
        for row in rows:
            record = _row_to_dict(row)
            managed_thread_id = str(record["managed_thread_id"])
            latest_turn = _latest_turn_for_thread(conn, managed_thread_id)
            snapshot = backfill_managed_thread_status(
                lifecycle_status=_coerce_text(record.get("status")),
                latest_turn_status=_coerce_text((latest_turn or {}).get("status")),
                changed_at=(
                    _coerce_text(record.get("status_updated_at"))
                    or _coerce_text((latest_turn or {}).get("finished_at"))
                    or _coerce_text((latest_turn or {}).get("started_at"))
                    or _coerce_text(record.get("updated_at"))
                    or _coerce_text(record.get("created_at"))
                ),
                compacted=_coerce_text(record.get("compact_seed")) is not None,
            )
            conn.execute(
                """
                UPDATE pma_managed_threads
                   SET normalized_status = ?,
                       status_reason_code = ?,
                       status_updated_at = ?,
                       status_terminal = ?,
                       status_turn_id = COALESCE(status_turn_id, ?)
                 WHERE managed_thread_id = ?
                """,
                (
                    snapshot.status,
                    snapshot.reason_code,
                    snapshot.changed_at,
                    1 if snapshot.terminal else 0,
                    snapshot.turn_id,
                    managed_thread_id,
                ),
            )


class PmaThreadStore:
    def __init__(self, hub_root: Path, *, durable: bool = False) -> None:
        self._path = default_pma_threads_db_path(hub_root)
        self._durable = durable
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    def _initialize(self) -> None:
        with pma_threads_db_lock(self._path):
            with open_sqlite(self._path, durable=self._durable) as conn:
                _ensure_schema(conn)

    @contextmanager
    def _write_conn(self) -> Iterator[Any]:
        with pma_threads_db_lock(self._path):
            with open_sqlite(self._path, durable=self._durable) as conn:
                _ensure_schema(conn)
                yield conn

    def _fetch_thread(
        self, conn: Any, managed_thread_id: str
    ) -> Optional[dict[str, Any]]:
        row = conn.execute(
            """
            SELECT *
              FROM pma_managed_threads
             WHERE managed_thread_id = ?
            """,
            (managed_thread_id,),
        ).fetchone()
        if row is None:
            return None
        return _normalize_thread_record(row)

    def _transition_thread_status(
        self,
        conn: Any,
        managed_thread_id: str,
        *,
        reason: str | ManagedThreadStatusReason,
        changed_at: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        thread = self._fetch_thread(conn, managed_thread_id)
        if thread is None:
            return None
        current = ManagedThreadStatusSnapshot.from_mapping(thread)
        resolved_changed_at = changed_at or now_iso()
        snapshot = transition_managed_thread_status(
            current,
            reason=reason,
            changed_at=resolved_changed_at,
            turn_id=turn_id,
        )
        if snapshot == current:
            return thread
        with conn:
            conn.execute(
                """
                UPDATE pma_managed_threads
                   SET normalized_status = ?,
                       status_reason_code = ?,
                       status_updated_at = ?,
                       status_terminal = ?,
                       status_turn_id = ?,
                       updated_at = ?
                 WHERE managed_thread_id = ?
                """,
                (
                    snapshot.status,
                    snapshot.reason_code,
                    snapshot.changed_at,
                    1 if snapshot.terminal else 0,
                    snapshot.turn_id,
                    resolved_changed_at,
                    managed_thread_id,
                ),
            )
        return self._fetch_thread(conn, managed_thread_id)

    def create_thread(
        self,
        agent: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
    ) -> dict[str, Any]:
        managed_thread_id = str(uuid.uuid4())
        now = now_iso()
        workspace = workspace_root
        if not workspace.is_absolute():
            raise ValueError("workspace_root must be absolute")

        snapshot = build_managed_thread_status_snapshot(
            reason=ManagedThreadStatusReason.THREAD_CREATED,
            changed_at=now,
        )
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO pma_managed_threads (
                        managed_thread_id,
                        agent,
                        repo_id,
                        workspace_root,
                        name,
                        backend_thread_id,
                        status,
                        normalized_status,
                        status_reason_code,
                        status_updated_at,
                        status_terminal,
                        status_turn_id,
                        last_turn_id,
                        last_message_preview,
                        compact_seed,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        managed_thread_id,
                        agent,
                        repo_id,
                        str(workspace),
                        name,
                        backend_thread_id,
                        "active",
                        snapshot.status,
                        snapshot.reason_code,
                        snapshot.changed_at,
                        1 if snapshot.terminal else 0,
                        snapshot.turn_id,
                        None,
                        None,
                        None,
                        now,
                        now,
                    ),
                )
            created = self._fetch_thread(conn, managed_thread_id)
        if created is None:
            raise RuntimeError("Failed to create managed PMA thread")
        return created

    def get_thread(self, managed_thread_id: str) -> Optional[dict[str, Any]]:
        with open_sqlite(self._path, durable=self._durable) as conn:
            _ensure_schema(conn)
            return self._fetch_thread(conn, managed_thread_id)

    def list_threads(
        self,
        *,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        normalized_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        query = """
            SELECT *
              FROM pma_managed_threads
             WHERE 1 = 1
        """
        params: list[Any] = []
        if agent is not None:
            query += " AND agent = ?"
            params.append(agent)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if normalized_status is not None:
            query += " AND normalized_status = ?"
            params.append(normalized_status)
        if repo_id is not None:
            query += " AND repo_id = ?"
            params.append(repo_id)
        query += " ORDER BY updated_at DESC, created_at DESC, managed_thread_id DESC"
        query += " LIMIT ?"
        params.append(limit)

        with open_sqlite(self._path, durable=self._durable) as conn:
            _ensure_schema(conn)
            rows = conn.execute(query, params).fetchall()
        return [_normalize_thread_record(row) for row in rows]

    def count_threads_by_repo(
        self, *, agent: Optional[str] = None, status: Optional[str] = None
    ) -> dict[str, int]:
        query = """
            SELECT TRIM(repo_id) AS repo_id, COUNT(*) AS thread_count
              FROM pma_managed_threads
             WHERE repo_id IS NOT NULL
               AND TRIM(repo_id) != ''
        """
        params: list[Any] = []
        if agent is not None:
            query += " AND agent = ?"
            params.append(agent)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " GROUP BY TRIM(repo_id)"

        with open_sqlite(self._path, durable=self._durable) as conn:
            _ensure_schema(conn)
            rows = conn.execute(query, params).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            repo_id = row["repo_id"]
            if not isinstance(repo_id, str) or not repo_id:
                continue
            counts[repo_id] = int(row["thread_count"] or 0)
        return counts

    def set_thread_backend_id(
        self, managed_thread_id: str, backend_thread_id: Optional[str]
    ) -> None:
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE pma_managed_threads
                       SET backend_thread_id = ?,
                           updated_at = ?
                     WHERE managed_thread_id = ?
                    """,
                    (backend_thread_id, now_iso(), managed_thread_id),
                )

    def update_thread_after_turn(
        self,
        managed_thread_id: str,
        *,
        last_turn_id: Optional[str],
        last_message_preview: Optional[str],
    ) -> None:
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE pma_managed_threads
                       SET last_turn_id = ?,
                           last_message_preview = ?,
                           updated_at = ?
                     WHERE managed_thread_id = ?
                    """,
                    (
                        last_turn_id,
                        last_message_preview,
                        now_iso(),
                        managed_thread_id,
                    ),
                )

    def set_thread_compact_seed(
        self,
        managed_thread_id: str,
        compact_seed: Optional[str],
        *,
        reset_backend_id: bool = False,
    ) -> None:
        changed_at = now_iso()
        query = """
            UPDATE pma_managed_threads
               SET compact_seed = ?,
                   updated_at = ?
        """
        params: list[Any] = [compact_seed, changed_at]
        if reset_backend_id:
            query += ", backend_thread_id = NULL"
        query += " WHERE managed_thread_id = ?"
        params.append(managed_thread_id)

        with self._write_conn() as conn:
            with conn:
                conn.execute(query, params)
            if _coerce_text(compact_seed) is not None:
                self._transition_thread_status(
                    conn,
                    managed_thread_id,
                    reason=ManagedThreadStatusReason.THREAD_COMPACTED,
                    changed_at=changed_at,
                )

    def archive_thread(self, managed_thread_id: str) -> None:
        changed_at = now_iso()
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE pma_managed_threads
                       SET status = 'archived',
                           updated_at = ?
                     WHERE managed_thread_id = ?
                    """,
                    (changed_at, managed_thread_id),
                )
            self._transition_thread_status(
                conn,
                managed_thread_id,
                reason=ManagedThreadStatusReason.THREAD_ARCHIVED,
                changed_at=changed_at,
            )

    def activate_thread(self, managed_thread_id: str) -> None:
        changed_at = now_iso()
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE pma_managed_threads
                       SET status = 'active',
                           updated_at = ?
                     WHERE managed_thread_id = ?
                    """,
                    (changed_at, managed_thread_id),
                )
            self._transition_thread_status(
                conn,
                managed_thread_id,
                reason=ManagedThreadStatusReason.THREAD_RESUMED,
                changed_at=changed_at,
            )

    def create_turn(
        self,
        managed_thread_id: str,
        *,
        prompt: str,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_turn_id: Optional[str] = None,
    ) -> dict[str, Any]:
        managed_turn_id = str(uuid.uuid4())
        started_at = now_iso()

        with self._write_conn() as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO pma_managed_turns (
                        managed_turn_id,
                        managed_thread_id,
                        client_turn_id,
                        backend_turn_id,
                        prompt,
                        status,
                        assistant_text,
                        transcript_turn_id,
                        model,
                        reasoning,
                        error,
                        started_at,
                        finished_at
                    )
                    SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                      FROM pma_managed_threads
                     WHERE managed_thread_id = ?
                       AND status = 'active'
                       AND NOT EXISTS (
                           SELECT 1
                             FROM pma_managed_turns
                            WHERE managed_thread_id = ?
                              AND status = 'running'
                       )
                    """,
                    (
                        managed_turn_id,
                        managed_thread_id,
                        client_turn_id,
                        None,
                        prompt,
                        "running",
                        None,
                        None,
                        model,
                        reasoning,
                        None,
                        started_at,
                        None,
                        managed_thread_id,
                        managed_thread_id,
                    ),
                )
                if cursor.rowcount == 0:
                    status_row = conn.execute(
                        """
                        SELECT status
                          FROM pma_managed_threads
                         WHERE managed_thread_id = ?
                        """,
                        (managed_thread_id,),
                    ).fetchone()
                    thread_status = (
                        str(status_row["status"])
                        if status_row is not None and status_row["status"] is not None
                        else None
                    )
                    if thread_status != "active":
                        raise ManagedThreadNotActiveError(
                            managed_thread_id, thread_status
                        )
                    raise ManagedThreadAlreadyHasRunningTurnError(managed_thread_id)
            self._transition_thread_status(
                conn,
                managed_thread_id,
                reason=ManagedThreadStatusReason.TURN_STARTED,
                changed_at=started_at,
                turn_id=managed_turn_id,
            )
            row = conn.execute(
                """
                SELECT *
                  FROM pma_managed_turns
                 WHERE managed_turn_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to create managed PMA turn")
        return _row_to_dict(row)

    def mark_turn_finished(
        self,
        managed_turn_id: str,
        *,
        status: str,
        assistant_text: Optional[str] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
    ) -> bool:
        finished_at = now_iso()
        reason = (
            ManagedThreadStatusReason.MANAGED_TURN_COMPLETED
            if status == "ok"
            else ManagedThreadStatusReason.MANAGED_TURN_FAILED
        )
        with self._write_conn() as conn:
            row = conn.execute(
                """
                SELECT managed_thread_id
                  FROM pma_managed_turns
                 WHERE managed_turn_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
            if row is None:
                return False
            managed_thread_id = str(row["managed_thread_id"])
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE pma_managed_turns
                       SET status = ?,
                           assistant_text = ?,
                           error = ?,
                           backend_turn_id = ?,
                           transcript_turn_id = ?,
                           finished_at = ?
                     WHERE managed_turn_id = ?
                       AND status = 'running'
                    """,
                    (
                        status,
                        assistant_text,
                        error,
                        backend_turn_id,
                        transcript_turn_id,
                        finished_at,
                        managed_turn_id,
                    ),
                )
            if cursor.rowcount == 0:
                return False
            self._transition_thread_status(
                conn,
                managed_thread_id,
                reason=reason,
                changed_at=finished_at,
                turn_id=managed_turn_id,
            )
        return True

    def set_turn_backend_turn_id(
        self, managed_turn_id: str, backend_turn_id: Optional[str]
    ) -> None:
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE pma_managed_turns
                       SET backend_turn_id = ?
                     WHERE managed_turn_id = ?
                    """,
                    (backend_turn_id, managed_turn_id),
                )

    def mark_turn_interrupted(self, managed_turn_id: str) -> bool:
        finished_at = now_iso()
        with self._write_conn() as conn:
            row = conn.execute(
                """
                SELECT managed_thread_id
                  FROM pma_managed_turns
                 WHERE managed_turn_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
            if row is None:
                return False
            managed_thread_id = str(row["managed_thread_id"])
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE pma_managed_turns
                       SET status = 'interrupted',
                           finished_at = ?
                     WHERE managed_turn_id = ?
                       AND status = 'running'
                    """,
                    (finished_at, managed_turn_id),
                )
            if cursor.rowcount == 0:
                return False
            self._transition_thread_status(
                conn,
                managed_thread_id,
                reason=ManagedThreadStatusReason.MANAGED_TURN_INTERRUPTED,
                changed_at=finished_at,
                turn_id=managed_turn_id,
            )
        return True

    def list_turns(
        self, managed_thread_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        with open_sqlite(self._path, durable=self._durable) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                  FROM pma_managed_turns
                 WHERE managed_thread_id = ?
                 ORDER BY started_at DESC, rowid DESC
                 LIMIT ?
                """,
                (managed_thread_id, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def has_running_turn(self, managed_thread_id: str) -> bool:
        with open_sqlite(self._path, durable=self._durable) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT 1
                  FROM pma_managed_turns
                 WHERE managed_thread_id = ?
                   AND status = 'running'
                 LIMIT 1
                """,
                (managed_thread_id,),
            ).fetchone()
        return row is not None

    def get_running_turn(self, managed_thread_id: str) -> Optional[dict[str, Any]]:
        with open_sqlite(self._path, durable=self._durable) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                  FROM pma_managed_turns
                 WHERE managed_thread_id = ?
                   AND status = 'running'
                 ORDER BY started_at DESC, rowid DESC
                 LIMIT 1
                """,
                (managed_thread_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)

    def get_turn(
        self, managed_thread_id: str, managed_turn_id: str
    ) -> Optional[dict[str, Any]]:
        with open_sqlite(self._path, durable=self._durable) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                  FROM pma_managed_turns
                 WHERE managed_thread_id = ?
                   AND managed_turn_id = ?
                """,
                (managed_thread_id, managed_turn_id),
            ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)

    def append_action(
        self,
        action_type: str,
        *,
        managed_thread_id: Optional[str] = None,
        payload_json: Optional[str] = None,
    ) -> int:
        with self._write_conn() as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO pma_managed_actions (
                        managed_thread_id,
                        action_type,
                        payload_json,
                        created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (managed_thread_id, action_type, payload_json, now_iso()),
                )
        action_id = cursor.lastrowid
        if not isinstance(action_id, int):
            raise RuntimeError("Failed to append PMA action")
        return action_id


__all__ = [
    "ManagedThreadAlreadyHasRunningTurnError",
    "ManagedThreadNotActiveError",
    "PMA_THREADS_DB_FILENAME",
    "PmaThreadStore",
    "default_pma_threads_db_path",
    "pma_threads_db_lock",
    "pma_threads_db_lock_path",
]
