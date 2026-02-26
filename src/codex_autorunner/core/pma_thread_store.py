from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .locks import file_lock
from .sqlite_utils import open_sqlite
from .time_utils import now_iso

PMA_THREADS_DB_FILENAME = "threads.sqlite3"


class ManagedThreadAlreadyHasRunningTurnError(RuntimeError):
    def __init__(self, managed_thread_id: str) -> None:
        super().__init__(
            f"Managed thread '{managed_thread_id}' already has a running turn"
        )
        self.managed_thread_id = managed_thread_id


def default_pma_threads_db_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_THREADS_DB_FILENAME


def pma_threads_db_lock_path(db_path: Path) -> Path:
    return db_path.with_suffix(db_path.suffix + ".lock")


@contextmanager
def pma_threads_db_lock(db_path: Path) -> Iterator[None]:
    with file_lock(pma_threads_db_lock_path(db_path)):
        yield


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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pma_managed_threads_status
            ON pma_managed_threads(status)
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


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


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
                        last_turn_id,
                        last_message_preview,
                        compact_seed,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        managed_thread_id,
                        agent,
                        repo_id,
                        str(workspace),
                        name,
                        backend_thread_id,
                        "active",
                        None,
                        None,
                        None,
                        now,
                        now,
                    ),
                )
            row = conn.execute(
                """
                SELECT *
                  FROM pma_managed_threads
                 WHERE managed_thread_id = ?
                """,
                (managed_thread_id,),
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to create managed PMA thread")
        return _row_to_dict(row)

    def get_thread(self, managed_thread_id: str) -> Optional[dict[str, Any]]:
        with open_sqlite(self._path, durable=self._durable) as conn:
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
        return _row_to_dict(row)

    def list_threads(
        self,
        *,
        agent: Optional[str] = None,
        status: Optional[str] = None,
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
        if repo_id is not None:
            query += " AND repo_id = ?"
            params.append(repo_id)
        query += " ORDER BY updated_at DESC, created_at DESC, managed_thread_id DESC"
        query += " LIMIT ?"
        params.append(limit)

        with open_sqlite(self._path, durable=self._durable) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(row) for row in rows]

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
        query = """
            UPDATE pma_managed_threads
               SET compact_seed = ?,
                   updated_at = ?
        """
        params: list[Any] = [compact_seed, now_iso()]
        if reset_backend_id:
            query += ", backend_thread_id = NULL"
        query += " WHERE managed_thread_id = ?"
        params.append(managed_thread_id)

        with self._write_conn() as conn:
            with conn:
                conn.execute(query, params)

    def archive_thread(self, managed_thread_id: str) -> None:
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE pma_managed_threads
                       SET status = 'archived',
                           updated_at = ?
                     WHERE managed_thread_id = ?
                    """,
                    (now_iso(), managed_thread_id),
                )

    def activate_thread(self, managed_thread_id: str) -> None:
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE pma_managed_threads
                       SET status = 'active',
                           updated_at = ?
                     WHERE managed_thread_id = ?
                    """,
                    (now_iso(), managed_thread_id),
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
                     WHERE NOT EXISTS (
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
                    ),
                )
                if cursor.rowcount == 0:
                    raise ManagedThreadAlreadyHasRunningTurnError(managed_thread_id)
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
    ) -> None:
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE pma_managed_turns
                       SET status = ?,
                           assistant_text = ?,
                           error = ?,
                           backend_turn_id = ?,
                           transcript_turn_id = ?,
                           finished_at = ?
                     WHERE managed_turn_id = ?
                    """,
                    (
                        status,
                        assistant_text,
                        error,
                        backend_turn_id,
                        transcript_turn_id,
                        now_iso(),
                        managed_turn_id,
                    ),
                )

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

    def mark_turn_interrupted(self, managed_turn_id: str) -> None:
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE pma_managed_turns
                       SET status = 'interrupted',
                           finished_at = ?
                     WHERE managed_turn_id = ?
                    """,
                    (now_iso(), managed_turn_id),
                )

    def list_turns(
        self, managed_thread_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        with open_sqlite(self._path, durable=self._durable) as conn:
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
    "PMA_THREADS_DB_FILENAME",
    "PmaThreadStore",
    "default_pma_threads_db_path",
    "pma_threads_db_lock",
    "pma_threads_db_lock_path",
]
