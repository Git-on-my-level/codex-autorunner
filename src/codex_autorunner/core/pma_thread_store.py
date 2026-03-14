from __future__ import annotations

import json
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
from .orchestration.migrate_legacy_state import backfill_legacy_thread_state
from .orchestration.models import normalize_resource_owner_fields
from .orchestration.sqlite import open_orchestration_sqlite
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


def _normalize_request_kind(value: Any) -> str:
    normalized = (_coerce_text(value) or "").lower()
    if normalized == "review":
        return "review"
    return "message"


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _json_loads_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _thread_queue_lane_id(managed_thread_id: str) -> str:
    return f"thread:{managed_thread_id}"


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
                resource_kind TEXT,
                resource_id TEXT,
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
            "resource_kind",
            "ALTER TABLE pma_managed_threads ADD COLUMN resource_kind TEXT",
        ),
        (
            "resource_id",
            "ALTER TABLE pma_managed_threads ADD COLUMN resource_id TEXT",
        ),
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
            CREATE INDEX IF NOT EXISTS idx_pma_managed_threads_resource
            ON pma_managed_threads(resource_kind, resource_id)
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
    """Current PMA-backed persistence for runtime thread targets and executions.

    Orchestration services may use this as an implementation dependency during
    the migration window, but callers should not treat its row shape as the
    long-term orchestration API surface.
    """

    def __init__(self, hub_root: Path, *, durable: bool = False) -> None:
        self._hub_root = hub_root
        self._path = default_pma_threads_db_path(hub_root)
        self._durable = durable
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    def _initialize(self) -> None:
        with pma_threads_db_lock(self._path):
            with open_orchestration_sqlite(
                self._hub_root,
                durable=self._durable,
            ) as conn:
                backfill_legacy_thread_state(self._hub_root, conn)
                self._sync_legacy_mirror(conn)

    @contextmanager
    def _read_conn(self) -> Iterator[Any]:
        with open_orchestration_sqlite(
            self._hub_root,
            durable=self._durable,
        ) as conn:
            backfill_legacy_thread_state(self._hub_root, conn)
            yield conn

    @contextmanager
    def _write_conn(self) -> Iterator[Any]:
        with pma_threads_db_lock(self._path):
            with open_orchestration_sqlite(
                self._hub_root,
                durable=self._durable,
            ) as conn:
                backfill_legacy_thread_state(self._hub_root, conn)
                yield conn
                self._sync_legacy_mirror(conn)

    @staticmethod
    def _thread_row_to_record(row: Any) -> dict[str, Any]:
        resource_kind, resource_id, repo_id = normalize_resource_owner_fields(
            resource_kind=row["resource_kind"],
            resource_id=row["resource_id"],
            repo_id=row["repo_id"],
        )
        record = {
            "managed_thread_id": row["thread_target_id"],
            "agent": row["agent_id"],
            "repo_id": repo_id,
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "workspace_root": row["workspace_root"],
            "name": row["display_name"],
            "backend_thread_id": row["backend_thread_id"],
            "status": row["lifecycle_status"] or "active",
            "normalized_status": row["runtime_status"] or "idle",
            "status_reason_code": row["status_reason"],
            "status_updated_at": row["status_updated_at"] or row["updated_at"],
            "status_terminal": int(row["status_terminal"] or 0),
            "status_turn_id": row["status_turn_id"],
            "last_turn_id": row["last_execution_id"],
            "last_message_preview": row["last_message_preview"],
            "compact_seed": row["compact_seed"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return _normalize_thread_record(record)

    @staticmethod
    def _execution_row_to_record(row: Any) -> dict[str, Any]:
        return {
            "managed_turn_id": row["execution_id"],
            "managed_thread_id": row["thread_target_id"],
            "client_turn_id": row["client_request_id"],
            "request_kind": _normalize_request_kind(row["request_kind"]),
            "backend_turn_id": row["backend_turn_id"],
            "prompt": row["prompt_text"],
            "status": row["status"],
            "assistant_text": row["assistant_text"],
            "transcript_turn_id": row["transcript_mirror_id"],
            "model": row["model_id"],
            "reasoning": row["reasoning_level"],
            "error": row["error_text"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def _fetch_thread(
        self, conn: Any, managed_thread_id: str
    ) -> Optional[dict[str, Any]]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_thread_targets
             WHERE thread_target_id = ?
            """,
            (managed_thread_id,),
        ).fetchone()
        if row is None:
            return None
        return self._thread_row_to_record(row)

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
                UPDATE orch_thread_targets
                   SET runtime_status = ?,
                       status_reason = ?,
                       status_updated_at = ?,
                       status_terminal = ?,
                       status_turn_id = ?,
                       updated_at = ?
                 WHERE thread_target_id = ?
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
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
    ) -> dict[str, Any]:
        managed_thread_id = str(uuid.uuid4())
        now = now_iso()
        workspace = workspace_root
        if not workspace.is_absolute():
            raise ValueError("workspace_root must be absolute")
        normalized_resource_kind, normalized_resource_id, normalized_repo_id = (
            normalize_resource_owner_fields(
                resource_kind=resource_kind,
                resource_id=resource_id,
                repo_id=repo_id,
            )
        )

        snapshot = build_managed_thread_status_snapshot(
            reason=ManagedThreadStatusReason.THREAD_CREATED,
            changed_at=now,
        )
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_thread_targets (
                        thread_target_id,
                        agent_id,
                        backend_thread_id,
                        repo_id,
                        resource_kind,
                        resource_id,
                        workspace_root,
                        display_name,
                        lifecycle_status,
                        runtime_status,
                        status_reason,
                        status_turn_id,
                        last_execution_id,
                        last_message_preview,
                        compact_seed,
                        created_at,
                        updated_at,
                        status_updated_at,
                        status_terminal
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        managed_thread_id,
                        agent,
                        backend_thread_id,
                        normalized_repo_id,
                        normalized_resource_kind,
                        normalized_resource_id,
                        str(workspace),
                        name,
                        "active",
                        snapshot.status,
                        snapshot.reason_code,
                        snapshot.turn_id,
                        None,
                        None,
                        None,
                        now,
                        now,
                        snapshot.changed_at,
                        1 if snapshot.terminal else 0,
                    ),
                )
            created = self._fetch_thread(conn, managed_thread_id)
        if created is None:
            raise RuntimeError("Failed to create managed PMA thread")
        return created

    def get_thread(self, managed_thread_id: str) -> Optional[dict[str, Any]]:
        with self._read_conn() as conn:
            return self._fetch_thread(conn, managed_thread_id)

    def list_threads(
        self,
        *,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        normalized_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        query = """
            SELECT *
              FROM orch_thread_targets
             WHERE 1 = 1
        """
        params: list[Any] = []
        if agent is not None:
            query += " AND agent_id = ?"
            params.append(agent)
        if status is not None:
            query += " AND lifecycle_status = ?"
            params.append(status)
        if normalized_status is not None:
            query += " AND runtime_status = ?"
            params.append(normalized_status)
        normalized_resource_kind, normalized_resource_id, normalized_repo_id = (
            normalize_resource_owner_fields(
                resource_kind=resource_kind,
                resource_id=resource_id,
                repo_id=repo_id,
            )
        )
        if normalized_resource_kind is not None:
            query += " AND resource_kind = ?"
            params.append(normalized_resource_kind)
        if normalized_resource_id is not None:
            query += " AND resource_id = ?"
            params.append(normalized_resource_id)
        if normalized_repo_id is not None and normalized_resource_kind is None:
            query += " AND repo_id = ?"
            params.append(normalized_repo_id)
        query += " ORDER BY updated_at DESC, created_at DESC, thread_target_id DESC"
        query += " LIMIT ?"
        params.append(limit)

        with self._read_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._thread_row_to_record(row) for row in rows]

    def count_threads_by_repo(
        self, *, agent: Optional[str] = None, status: Optional[str] = None
    ) -> dict[str, int]:
        query = """
            SELECT TRIM(repo_id) AS repo_id, COUNT(*) AS thread_count
              FROM orch_thread_targets
             WHERE repo_id IS NOT NULL
               AND TRIM(repo_id) != ''
        """
        params: list[Any] = []
        if agent is not None:
            query += " AND agent_id = ?"
            params.append(agent)
        if status is not None:
            query += " AND lifecycle_status = ?"
            params.append(status)
        query += " GROUP BY TRIM(repo_id)"

        with self._read_conn() as conn:
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
                    UPDATE orch_thread_targets
                       SET backend_thread_id = ?,
                           updated_at = ?
                     WHERE thread_target_id = ?
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
                    UPDATE orch_thread_targets
                       SET last_execution_id = ?,
                           last_message_preview = ?,
                           updated_at = ?
                     WHERE thread_target_id = ?
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
            UPDATE orch_thread_targets
               SET compact_seed = ?,
                   updated_at = ?
        """
        params: list[Any] = [compact_seed, changed_at]
        if reset_backend_id:
            query += ", backend_thread_id = NULL"
        query += " WHERE thread_target_id = ?"
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
                    UPDATE orch_thread_targets
                       SET lifecycle_status = 'archived',
                           updated_at = ?
                     WHERE thread_target_id = ?
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
                    UPDATE orch_thread_targets
                       SET lifecycle_status = 'active',
                           updated_at = ?
                     WHERE thread_target_id = ?
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
        request_kind: str = "message",
        busy_policy: str = "reject",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_turn_id: Optional[str] = None,
        queue_payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        managed_turn_id = str(uuid.uuid4())
        started_at = now_iso()
        queue_item_id = uuid.uuid4().hex
        normalized_request_kind = _normalize_request_kind(request_kind)

        with self._write_conn() as conn:
            status_row = conn.execute(
                """
                SELECT lifecycle_status
                  FROM orch_thread_targets
                 WHERE thread_target_id = ?
                """,
                (managed_thread_id,),
            ).fetchone()
            thread_status = (
                str(status_row["lifecycle_status"])
                if status_row is not None and status_row["lifecycle_status"] is not None
                else None
            )
            if thread_status != "active":
                raise ManagedThreadNotActiveError(managed_thread_id, thread_status)
            running_exists = conn.execute(
                """
                SELECT 1
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND status = 'running'
                 LIMIT 1
                """,
                (managed_thread_id,),
            ).fetchone()
            with conn:
                execution_status = "queued" if running_exists is not None else "running"
                if execution_status == "queued" and busy_policy != "queue":
                    raise ManagedThreadAlreadyHasRunningTurnError(managed_thread_id)
                conn.execute(
                    """
                    INSERT INTO orch_thread_executions (
                        execution_id,
                        thread_target_id,
                        client_request_id,
                        request_kind,
                        prompt_text,
                        status,
                        backend_turn_id,
                        assistant_text,
                        error_text,
                        model_id,
                        reasoning_level,
                        transcript_mirror_id,
                        started_at,
                        finished_at,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        managed_turn_id,
                        managed_thread_id,
                        client_turn_id,
                        normalized_request_kind,
                        prompt,
                        execution_status,
                        None,
                        None,
                        None,
                        model,
                        reasoning,
                        None,
                        started_at,
                        None,
                        started_at,
                    ),
                )
                if execution_status == "queued":
                    conn.execute(
                        """
                        INSERT INTO orch_queue_items (
                            queue_item_id,
                            lane_id,
                            source_kind,
                            source_key,
                            dedupe_key,
                            state,
                            visible_at,
                            claimed_at,
                            completed_at,
                            payload_json,
                            created_at,
                            updated_at,
                            idempotency_key,
                            error_text,
                            dedupe_reason,
                            result_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            queue_item_id,
                            _thread_queue_lane_id(managed_thread_id),
                            "thread_execution",
                            managed_turn_id,
                            client_turn_id or managed_turn_id,
                            "queued",
                            started_at,
                            None,
                            None,
                            _json_dumps(queue_payload or {}),
                            started_at,
                            started_at,
                            client_turn_id or managed_turn_id,
                            None,
                            None,
                            _json_dumps({}),
                        ),
                    )
            if execution_status == "running":
                with conn:
                    conn.execute(
                        """
                        UPDATE orch_thread_targets
                           SET last_execution_id = ?,
                               updated_at = ?
                         WHERE thread_target_id = ?
                        """,
                        (managed_turn_id, started_at, managed_thread_id),
                    )
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
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to create managed PMA turn")
        return self._execution_row_to_record(row)

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
                SELECT thread_target_id
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
            if row is None:
                return False
            managed_thread_id = str(row["thread_target_id"])
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_thread_executions
                       SET status = ?,
                           assistant_text = ?,
                           error_text = ?,
                           backend_turn_id = ?,
                           transcript_mirror_id = ?,
                           finished_at = ?
                     WHERE execution_id = ?
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
            queue_state = "completed" if status == "ok" else "failed"
            conn.execute(
                """
                UPDATE orch_queue_items
                   SET state = ?,
                       completed_at = ?,
                       updated_at = ?,
                       error_text = ?,
                       result_json = ?
                 WHERE source_kind = 'thread_execution'
                   AND source_key = ?
                   AND state = 'running'
                """,
                (
                    queue_state,
                    finished_at,
                    finished_at,
                    error,
                    _json_dumps(
                        {
                            "status": status,
                            "assistant_text": assistant_text or "",
                            "backend_turn_id": backend_turn_id or "",
                            "transcript_turn_id": transcript_turn_id or "",
                            "error": error or "",
                        }
                    ),
                    managed_turn_id,
                ),
            )
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
                    UPDATE orch_thread_executions
                       SET backend_turn_id = ?
                     WHERE execution_id = ?
                    """,
                    (backend_turn_id, managed_turn_id),
                )

    def mark_turn_interrupted(self, managed_turn_id: str) -> bool:
        finished_at = now_iso()
        with self._write_conn() as conn:
            row = conn.execute(
                """
                SELECT thread_target_id
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
            if row is None:
                return False
            managed_thread_id = str(row["thread_target_id"])
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_thread_executions
                       SET status = 'interrupted',
                           finished_at = ?
                     WHERE execution_id = ?
                       AND status = 'running'
                    """,
                    (finished_at, managed_turn_id),
                )
            if cursor.rowcount == 0:
                return False
            conn.execute(
                """
                UPDATE orch_queue_items
                   SET state = 'failed',
                       completed_at = ?,
                       updated_at = ?,
                       error_text = COALESCE(error_text, 'interrupted'),
                       result_json = ?
                 WHERE source_kind = 'thread_execution'
                   AND source_key = ?
                   AND state = 'running'
                """,
                (
                    finished_at,
                    finished_at,
                    _json_dumps({"status": "interrupted"}),
                    managed_turn_id,
                ),
            )
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

        with self._read_conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                 ORDER BY rowid DESC
                 LIMIT ?
                """,
                (managed_thread_id, limit),
            ).fetchall()
        return [self._execution_row_to_record(row) for row in rows]

    def has_running_turn(self, managed_thread_id: str) -> bool:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT 1
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND status = 'running'
                 LIMIT 1
                """,
                (managed_thread_id,),
            ).fetchone()
        return row is not None

    def get_running_turn(self, managed_thread_id: str) -> Optional[dict[str, Any]]:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND status = 'running'
                 ORDER BY started_at DESC, execution_id DESC
                 LIMIT 1
                """,
                (managed_thread_id,),
            ).fetchone()
        if row is None:
            return None
        return self._execution_row_to_record(row)

    def get_turn(
        self, managed_thread_id: str, managed_turn_id: str
    ) -> Optional[dict[str, Any]]:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND execution_id = ?
                """,
                (managed_thread_id, managed_turn_id),
            ).fetchone()
        if row is None:
            return None
        return self._execution_row_to_record(row)

    def list_queued_turns(
        self, managed_thread_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._read_conn() as conn:
            rows = conn.execute(
                """
                SELECT e.*
                  FROM orch_thread_executions AS e
                  JOIN orch_queue_items AS q
                    ON q.source_kind = 'thread_execution'
                   AND q.source_key = e.execution_id
                 WHERE e.thread_target_id = ?
                   AND e.status = 'queued'
                   AND q.lane_id = ?
                   AND q.state IN ('pending', 'queued', 'waiting')
                 ORDER BY COALESCE(q.visible_at, q.created_at) ASC, q.rowid ASC
                 LIMIT ?
                """,
                (
                    managed_thread_id,
                    _thread_queue_lane_id(managed_thread_id),
                    limit,
                ),
            ).fetchall()
        return [self._execution_row_to_record(row) for row in rows]

    def list_pending_turn_queue_items(
        self, managed_thread_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._read_conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    q.queue_item_id,
                    q.state,
                    q.visible_at,
                    q.created_at,
                    e.execution_id,
                    e.request_kind,
                    e.prompt_text,
                    e.model_id,
                    e.reasoning_level,
                    e.client_request_id
                  FROM orch_queue_items AS q
                  JOIN orch_thread_executions AS e
                    ON e.execution_id = q.source_key
                 WHERE q.source_kind = 'thread_execution'
                   AND q.lane_id = ?
                   AND e.thread_target_id = ?
                   AND q.state IN ('pending', 'queued', 'waiting')
                 ORDER BY COALESCE(q.visible_at, q.created_at) ASC, q.rowid ASC
                 LIMIT ?
                """,
                (
                    _thread_queue_lane_id(managed_thread_id),
                    managed_thread_id,
                    limit,
                ),
            ).fetchall()
        return [
            {
                "queue_item_id": str(row["queue_item_id"]),
                "state": str(row["state"]),
                "visible_at": row["visible_at"],
                "enqueued_at": row["created_at"],
                "managed_turn_id": str(row["execution_id"]),
                "request_kind": _normalize_request_kind(row["request_kind"]),
                "prompt": str(row["prompt_text"] or ""),
                "model": row["model_id"],
                "reasoning": row["reasoning_level"],
                "client_turn_id": row["client_request_id"],
            }
            for row in rows
        ]

    def get_queue_depth(self, managed_thread_id: str) -> int:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS queue_depth
                  FROM orch_queue_items
                 WHERE source_kind = 'thread_execution'
                   AND lane_id = ?
                   AND state IN ('pending', 'queued', 'waiting')
                """,
                (_thread_queue_lane_id(managed_thread_id),),
            ).fetchone()
        return int((row["queue_depth"] if row is not None else 0) or 0)

    def cancel_queued_turns(self, managed_thread_id: str) -> int:
        cancelled_at = now_iso()
        with self._write_conn() as conn:
            rows = conn.execute(
                """
                SELECT e.execution_id
                  FROM orch_queue_items AS q
                  JOIN orch_thread_executions AS e
                    ON e.execution_id = q.source_key
                 WHERE q.source_kind = 'thread_execution'
                   AND q.lane_id = ?
                   AND e.thread_target_id = ?
                   AND e.status = 'queued'
                   AND q.state IN ('pending', 'queued', 'waiting')
                 ORDER BY COALESCE(q.visible_at, q.created_at) ASC, q.rowid ASC
                """,
                (
                    _thread_queue_lane_id(managed_thread_id),
                    managed_thread_id,
                ),
            ).fetchall()
            execution_ids = [str(row["execution_id"]) for row in rows]
            if not execution_ids:
                return 0
            placeholders = ",".join("?" for _ in execution_ids)
            with conn:
                conn.execute(
                    f"""
                    UPDATE orch_thread_executions
                       SET status = 'interrupted',
                           error_text = COALESCE(error_text, 'interrupted'),
                           finished_at = ?
                     WHERE execution_id IN ({placeholders})
                       AND status = 'queued'
                    """,
                    (cancelled_at, *execution_ids),
                )
                conn.execute(
                    f"""
                    UPDATE orch_queue_items
                       SET state = 'failed',
                           completed_at = ?,
                           updated_at = ?,
                           error_text = COALESCE(error_text, 'interrupted'),
                           result_json = ?
                     WHERE source_kind = 'thread_execution'
                       AND lane_id = ?
                       AND source_key IN ({placeholders})
                       AND state IN ('pending', 'queued', 'waiting')
                    """,
                    (
                        cancelled_at,
                        cancelled_at,
                        _json_dumps({"status": "interrupted"}),
                        _thread_queue_lane_id(managed_thread_id),
                        *execution_ids,
                    ),
                )
        return len(execution_ids)

    def claim_next_queued_turn(
        self, managed_thread_id: str
    ) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
        claimed_at = now_iso()
        with self._write_conn() as conn:
            running = conn.execute(
                """
                SELECT 1
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND status = 'running'
                 LIMIT 1
                """,
                (managed_thread_id,),
            ).fetchone()
            if running is not None:
                return None
            row = conn.execute(
                """
                SELECT
                    q.queue_item_id,
                    q.payload_json,
                    e.execution_id
                  FROM orch_queue_items AS q
                  JOIN orch_thread_executions AS e
                    ON e.execution_id = q.source_key
                 WHERE q.source_kind = 'thread_execution'
                   AND q.lane_id = ?
                   AND e.thread_target_id = ?
                   AND e.status = 'queued'
                   AND q.state IN ('pending', 'queued', 'waiting')
                 ORDER BY COALESCE(q.visible_at, q.created_at) ASC, q.rowid ASC
                 LIMIT 1
                """,
                (
                    _thread_queue_lane_id(managed_thread_id),
                    managed_thread_id,
                ),
            ).fetchone()
            if row is None:
                return None
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_queue_items
                       SET state = 'running',
                           claimed_at = ?,
                           updated_at = ?
                     WHERE queue_item_id = ?
                       AND state IN ('pending', 'queued', 'waiting')
                    """,
                    (claimed_at, claimed_at, str(row["queue_item_id"])),
                )
                if cursor.rowcount == 0:
                    return None
                conn.execute(
                    """
                    UPDATE orch_thread_executions
                       SET status = 'running',
                           started_at = ?
                     WHERE execution_id = ?
                       AND status = 'queued'
                    """,
                    (claimed_at, str(row["execution_id"])),
                )
                conn.execute(
                    """
                    UPDATE orch_thread_targets
                       SET last_execution_id = ?,
                           updated_at = ?
                     WHERE thread_target_id = ?
                    """,
                    (str(row["execution_id"]), claimed_at, managed_thread_id),
                )
            self._transition_thread_status(
                conn,
                managed_thread_id,
                reason=ManagedThreadStatusReason.TURN_STARTED,
                changed_at=claimed_at,
                turn_id=str(row["execution_id"]),
            )
            execution_row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (str(row["execution_id"]),),
            ).fetchone()
        if execution_row is None:
            return None
        return (
            self._execution_row_to_record(execution_row),
            _json_loads_object(row["payload_json"]),
        )

    def append_action(
        self,
        action_type: str,
        *,
        managed_thread_id: Optional[str] = None,
        payload_json: Optional[str] = None,
    ) -> int:
        with self._write_conn() as conn:
            row = conn.execute(
                """
                SELECT MAX(CAST(action_id AS INTEGER)) AS max_action_id
                  FROM orch_thread_actions
                 WHERE action_id GLOB '[0-9]*'
                """
            ).fetchone()
            next_id = int(row["max_action_id"] or 0) + 1
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_thread_actions (
                        action_id,
                        thread_target_id,
                        execution_id,
                        action_type,
                        payload_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(next_id),
                        managed_thread_id,
                        None,
                        action_type,
                        payload_json or "{}",
                        now_iso(),
                    ),
                )
        return next_id

    def _sync_legacy_mirror(self, conn: Any) -> None:
        with open_sqlite(self._path, durable=self._durable) as legacy_conn:
            _ensure_schema(legacy_conn)
            thread_rows = conn.execute(
                """
                SELECT *
                  FROM orch_thread_targets
                 ORDER BY created_at ASC, thread_target_id ASC
                """
            ).fetchall()
            execution_rows = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 ORDER BY started_at ASC, execution_id ASC
                """
            ).fetchall()
            action_rows = conn.execute(
                """
                SELECT *
                  FROM orch_thread_actions
                 ORDER BY CAST(action_id AS INTEGER) ASC, action_id ASC
                """
            ).fetchall()
            with legacy_conn:
                legacy_conn.execute("DELETE FROM pma_managed_actions")
                legacy_conn.execute("DELETE FROM pma_managed_turns")
                legacy_conn.execute("DELETE FROM pma_managed_threads")
                for row in thread_rows:
                    legacy = self._thread_row_to_record(row)
                    legacy_conn.execute(
                        """
                        INSERT INTO pma_managed_threads (
                            managed_thread_id,
                            agent,
                            repo_id,
                            resource_kind,
                            resource_id,
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
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            legacy["managed_thread_id"],
                            legacy["agent"],
                            legacy["repo_id"],
                            legacy["resource_kind"],
                            legacy["resource_id"],
                            legacy["workspace_root"],
                            legacy["name"],
                            legacy["backend_thread_id"],
                            legacy["status"],
                            legacy["normalized_status"],
                            legacy["status_reason_code"],
                            legacy["status_updated_at"],
                            1 if legacy["status_terminal"] else 0,
                            legacy["status_turn_id"],
                            legacy["last_turn_id"],
                            legacy["last_message_preview"],
                            legacy["compact_seed"],
                            legacy["created_at"],
                            legacy["updated_at"],
                        ),
                    )
                for row in execution_rows:
                    legacy = self._execution_row_to_record(row)
                    legacy_conn.execute(
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
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            legacy["managed_turn_id"],
                            legacy["managed_thread_id"],
                            legacy["client_turn_id"],
                            legacy["backend_turn_id"],
                            legacy["prompt"],
                            legacy["status"],
                            legacy["assistant_text"],
                            legacy["transcript_turn_id"],
                            legacy["model"],
                            legacy["reasoning"],
                            legacy["error"],
                            legacy["started_at"],
                            legacy["finished_at"],
                        ),
                    )
                for row in action_rows:
                    action_id = str(row["action_id"] or "").strip()
                    try:
                        legacy_action_id = int(action_id)
                    except ValueError:
                        continue
                    legacy_conn.execute(
                        """
                        INSERT INTO pma_managed_actions (
                            action_id,
                            managed_thread_id,
                            action_type,
                            payload_json,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            legacy_action_id,
                            row["thread_target_id"],
                            row["action_type"],
                            row["payload_json"],
                            row["created_at"],
                        ),
                    )


__all__ = [
    "ManagedThreadAlreadyHasRunningTurnError",
    "ManagedThreadNotActiveError",
    "PMA_THREADS_DB_FILENAME",
    "PmaThreadStore",
    "default_pma_threads_db_path",
    "pma_threads_db_lock",
    "pma_threads_db_lock_path",
]
