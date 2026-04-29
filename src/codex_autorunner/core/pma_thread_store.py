from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import load_hub_config
from .freshness import resolve_stale_threshold_seconds
from .managed_thread_status import (
    ManagedThreadStatusReason,
    ManagedThreadStatusSnapshot,
    backfill_managed_thread_status,
    build_managed_thread_status_snapshot,
    transition_managed_thread_status,
)
from .orchestration.models import normalize_resource_owner_fields
from .orchestration.runtime_bindings import (
    RuntimeThreadBinding,
    clear_runtime_thread_binding,
    get_runtime_thread_binding,
    set_runtime_thread_binding,
)
from .pma_thread_store_bootstrap import (
    PMA_THREADS_DB_FILENAME,
    PmaThreadStoreBootstrap,
    default_pma_threads_db_path,
    pma_threads_db_lock,
    pma_threads_db_lock_path,
)
from .pma_thread_store_lifecycle import PmaThreadStoreLifecycle, thread_queue_lane_id
from .pma_thread_store_rows import (
    PmaExecutionRecord,
    PmaThreadRecord,
)
from .pma_thread_store_rows import (
    coerce_text as _coerce_text,
)
from .pma_thread_store_rows import (
    complete_thread_execution_queue_item as _complete_thread_execution_queue_item,
)
from .pma_thread_store_rows import (
    enrich_thread_metadata_for_workspace as _enrich_thread_metadata_for_workspace,
)
from .pma_thread_store_rows import (
    fail_thread_execution_running_items as _fail_thread_execution_running_items,
)
from .pma_thread_store_rows import (
    insert_thread_execution_queue_item as _insert_thread_execution_queue_item,
)
from .pma_thread_store_rows import (
    normalize_request_kind as _normalize_request_kind,
)
from .pma_thread_store_rows import (
    row_to_dict as _row_to_dict,
)
from .pma_thread_store_rows import (
    sanitize_thread_metadata as _sanitize_thread_metadata,
)
from .pma_thread_store_rows import (
    workspace_head_branch as _workspace_head_branch,
)
from .sqlite_utils import ensure_columns
from .text_utils import _json_dumps, _json_loads_object
from .time_utils import now_iso

_BACKEND_RUNTIME_INSTANCE_ID_KEY = "backend_runtime_instance_id"
_RUNTIME_STARTED_AT_KEY = "runtime_started_at"


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


def _resolve_stale_running_threshold_seconds(
    hub_root: Path, *, override: Optional[int]
) -> int:
    if isinstance(override, int) and override > 0:
        return override
    try:
        config = load_hub_config(hub_root)
        pma_cfg = getattr(config, "pma", None)
        return resolve_stale_threshold_seconds(
            getattr(pma_cfg, "freshness_stale_threshold_seconds", None)
        )
    except Exception:
        return resolve_stale_threshold_seconds(None)


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
                metadata_json TEXT NOT NULL DEFAULT '{}',
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

    ensure_columns(
        conn,
        "pma_managed_threads",
        (
            ("resource_kind", "resource_kind TEXT"),
            ("resource_id", "resource_id TEXT"),
            ("normalized_status", "normalized_status TEXT"),
            ("status_reason_code", "status_reason_code TEXT"),
            ("status_updated_at", "status_updated_at TEXT"),
            ("status_terminal", "status_terminal INTEGER"),
            ("status_turn_id", "status_turn_id TEXT"),
            ("metadata_json", "metadata_json TEXT NOT NULL DEFAULT '{}'"),
        ),
    )
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


def _thread_row_to_record(row: Any) -> dict[str, Any]:
    return PmaThreadRecord.from_orchestration_row(row).to_dict()


def _execution_row_to_record(row: Any) -> dict[str, Any]:
    return PmaExecutionRecord.from_orchestration_row(row).to_dict()


def prepare_pma_thread_store(hub_root: Path, *, durable: bool = False) -> None:
    bootstrap = PmaThreadStoreBootstrap(
        hub_root=hub_root,
        db_path=default_pma_threads_db_path(hub_root),
        durable=durable,
        thread_row_to_record=_thread_row_to_record,
        execution_row_to_record=_execution_row_to_record,
        ensure_legacy_schema=_ensure_schema,
    )
    bootstrap.prepare()


class PmaThreadStore:
    """Current PMA-backed persistence for runtime thread targets and executions.

    Orchestration services may use this as an implementation dependency during
    the migration window, but callers should not treat its row shape as the
    long-term orchestration API surface.
    """

    def __init__(
        self,
        hub_root: Path,
        *,
        durable: bool = False,
        stale_running_threshold_seconds: Optional[int] = None,
        bootstrap_on_init: bool = True,
    ) -> None:
        self._hub_root = hub_root
        self._path = default_pma_threads_db_path(hub_root)
        self._durable = durable
        self._bootstrap_on_init = bool(bootstrap_on_init)
        self._stale_running_threshold_seconds = (
            _resolve_stale_running_threshold_seconds(
                hub_root, override=stale_running_threshold_seconds
            )
        )
        self._bootstrap = PmaThreadStoreBootstrap(
            hub_root=self._hub_root,
            db_path=self._path,
            durable=self._durable,
            thread_row_to_record=_thread_row_to_record,
            execution_row_to_record=_execution_row_to_record,
            ensure_legacy_schema=_ensure_schema,
        )
        self._lifecycle = PmaThreadStoreLifecycle(
            stale_running_threshold_seconds=self._stale_running_threshold_seconds,
            execution_row_to_record=_execution_row_to_record,
            transition_thread_status=self._transition_thread_status,
            transition_thread_status_in_transaction=(
                self._transition_thread_status_in_transaction
            ),
        )
        self._initialize()

    @classmethod
    def connect_readonly(
        cls,
        hub_root: Path,
        *,
        durable: bool = False,
        stale_running_threshold_seconds: Optional[int] = None,
    ) -> "PmaThreadStore":
        return cls(
            hub_root,
            durable=durable,
            stale_running_threshold_seconds=stale_running_threshold_seconds,
            bootstrap_on_init=False,
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def hub_root(self) -> Path:
        return self._hub_root

    def _initialize(self) -> None:
        if not self._bootstrap_on_init:
            return
        self._bootstrap.initialize()

    @contextmanager
    def _read_conn(self) -> Iterator[Any]:
        with self._bootstrap.read_conn() as conn:
            yield conn

    @contextmanager
    def _write_conn(self) -> Iterator[Any]:
        with self._bootstrap.write_conn() as conn:
            yield conn

    def get_thread_runtime_binding(
        self, managed_thread_id: str
    ) -> Optional[RuntimeThreadBinding]:
        return get_runtime_thread_binding(self._hub_root, managed_thread_id)

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
        return _thread_row_to_record(row)

    def _transition_thread_status(
        self,
        conn: Any,
        managed_thread_id: str,
        *,
        reason: str | ManagedThreadStatusReason,
        changed_at: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        with conn:
            return self._transition_thread_status_in_transaction(
                conn,
                managed_thread_id,
                reason=reason,
                changed_at=changed_at,
                turn_id=turn_id,
            )

    def _transition_thread_status_in_transaction(
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

    def _recover_stale_running_turns(
        self,
        conn: Any,
        managed_thread_id: str,
        *,
        include_status_turn_age_recovery: bool = True,
    ) -> int:
        return self._lifecycle.recover_stale_running_turns(
            conn,
            managed_thread_id,
            include_status_turn_age_recovery=include_status_turn_age_recovery,
        )

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
        metadata: Optional[dict[str, Any]] = None,
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
        normalized_backend_thread_id = _coerce_text(backend_thread_id)
        metadata_payload = _enrich_thread_metadata_for_workspace(
            metadata,
            workspace_root=workspace,
        )
        backend_runtime_instance_id = _coerce_text(
            (metadata or {}).get(_BACKEND_RUNTIME_INSTANCE_ID_KEY)
            if isinstance(metadata, dict)
            else None
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
                        metadata_json,
                        created_at,
                        updated_at,
                        status_updated_at,
                        status_terminal
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        managed_thread_id,
                        agent,
                        normalized_backend_thread_id,
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
                        _json_dumps(metadata_payload),
                        now,
                        now,
                        snapshot.changed_at,
                        1 if snapshot.terminal else 0,
                    ),
                )
            if normalized_backend_thread_id is not None:
                set_runtime_thread_binding(
                    self._hub_root,
                    managed_thread_id,
                    backend_thread_id=normalized_backend_thread_id,
                    backend_runtime_instance_id=backend_runtime_instance_id,
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
        limit: Optional[int] = 200,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit <= 0:
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
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        with self._read_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_thread_row_to_record(row) for row in rows]

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
        self,
        managed_thread_id: str,
        backend_thread_id: Optional[str],
        *,
        backend_runtime_instance_id: Optional[str] = None,
    ) -> None:
        normalized_backend_thread_id = _coerce_text(backend_thread_id)
        current_binding = get_runtime_thread_binding(self._hub_root, managed_thread_id)
        resolved_runtime_instance_id = _coerce_text(backend_runtime_instance_id)
        if (
            normalized_backend_thread_id is not None
            and resolved_runtime_instance_id is None
        ):
            resolved_runtime_instance_id = (
                current_binding.backend_runtime_instance_id
                if current_binding is not None
                else None
            )
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT backend_thread_id
                  FROM orch_thread_targets
                 WHERE thread_target_id = ?
                """,
                (managed_thread_id,),
            ).fetchone()
        current_backend_thread_id = (
            _coerce_text(row["backend_thread_id"]) if row is not None else None
        )
        binding_matches = (
            normalized_backend_thread_id is None and current_binding is None
        ) or (
            current_binding is not None
            and current_binding.backend_thread_id == normalized_backend_thread_id
            and current_binding.backend_runtime_instance_id
            == resolved_runtime_instance_id
        )
        if (
            row is not None
            and current_backend_thread_id == normalized_backend_thread_id
            and binding_matches
        ):
            return
        with self._write_conn() as conn:
            thread = self._fetch_thread(conn, managed_thread_id)
            metadata = _sanitize_thread_metadata(
                dict((thread or {}).get("metadata") or {})
            )
            with conn:
                conn.execute(
                    """
                    UPDATE orch_thread_targets
                       SET backend_thread_id = ?,
                           metadata_json = ?,
                           updated_at = ?
                     WHERE thread_target_id = ?
                    """,
                    (
                        normalized_backend_thread_id,
                        _json_dumps(metadata),
                        now_iso(),
                        managed_thread_id,
                    ),
                )
        set_runtime_thread_binding(
            self._hub_root,
            managed_thread_id,
            backend_thread_id=normalized_backend_thread_id,
            backend_runtime_instance_id=resolved_runtime_instance_id,
        )

    def update_thread_metadata(
        self,
        managed_thread_id: str,
        metadata: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        metadata_patch = _sanitize_thread_metadata(metadata)
        if not metadata_patch:
            return self.get_thread(managed_thread_id)
        changed_at = now_iso()
        with self._write_conn() as conn:
            thread = self._fetch_thread(conn, managed_thread_id)
            if thread is None:
                return None
            current_metadata = _sanitize_thread_metadata(
                dict(thread.get("metadata") or {})
            )
            updated_metadata = dict(current_metadata)
            updated_metadata.update(metadata_patch)
            if updated_metadata == current_metadata:
                return thread
            with conn:
                conn.execute(
                    """
                    UPDATE orch_thread_targets
                       SET metadata_json = ?,
                           updated_at = ?
                     WHERE thread_target_id = ?
                    """,
                    (
                        _json_dumps(updated_metadata),
                        changed_at,
                        managed_thread_id,
                    ),
                )
            return self._fetch_thread(conn, managed_thread_id)

    def refresh_thread_head_branch(
        self,
        managed_thread_id: str,
        *,
        workspace_root: Optional[Path] = None,
    ) -> Optional[str]:
        thread = self.get_thread(managed_thread_id)
        if thread is None:
            return None
        metadata = dict(thread.get("metadata") or {})
        fallback_branch = _coerce_text(metadata.get("head_branch"))
        resolved_workspace = workspace_root
        if resolved_workspace is None:
            workspace_text = _coerce_text(thread.get("workspace_root"))
            if workspace_text is not None:
                resolved_workspace = Path(workspace_text)
        if resolved_workspace is None:
            return fallback_branch
        head_branch = _workspace_head_branch(resolved_workspace)
        if head_branch is None:
            return fallback_branch
        self.update_thread_metadata(
            managed_thread_id,
            {"head_branch": head_branch},
        )
        return head_branch

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
        if reset_backend_id:
            clear_runtime_thread_binding(self._hub_root, managed_thread_id)

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
        clear_runtime_thread_binding(self._hub_root, managed_thread_id)

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
        metadata: Optional[dict[str, Any]] = None,
        queue_payload: Optional[dict[str, Any]] = None,
        force_queue: bool = False,
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
            self._recover_stale_running_turns(conn, managed_thread_id)
            with conn:
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
                execution_status = (
                    "queued" if force_queue or running_exists is not None else "running"
                )
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
                        metadata_json,
                        transcript_mirror_id,
                        started_at,
                        finished_at,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        _json_dumps(dict(metadata or {})),
                        None,
                        started_at,
                        None,
                        started_at,
                    ),
                )
                if execution_status == "queued":
                    _insert_thread_execution_queue_item(
                        conn,
                        queue_item_id=queue_item_id,
                        lane_id=thread_queue_lane_id(managed_thread_id),
                        source_key=managed_turn_id,
                        dedupe_key=client_turn_id or managed_turn_id,
                        state="queued",
                        visible_at=started_at,
                        payload_json=_json_dumps(queue_payload or {}),
                        created_at=started_at,
                        idempotency_key=client_turn_id or managed_turn_id,
                    )
                else:
                    conn.execute(
                        """
                        UPDATE orch_thread_targets
                           SET last_execution_id = ?,
                               updated_at = ?
                         WHERE thread_target_id = ?
                        """,
                        (managed_turn_id, started_at, managed_thread_id),
                    )
                    self._transition_thread_status_in_transaction(
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
        return _execution_row_to_record(row)

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
            _complete_thread_execution_queue_item(
                conn,
                source_key=managed_turn_id,
                target_state=queue_state,
                completed_at=finished_at,
                error_text=error,
                result_json=_json_dumps(
                    {
                        "status": status,
                        "assistant_text": assistant_text or "",
                        "backend_turn_id": backend_turn_id or "",
                        "transcript_turn_id": transcript_turn_id or "",
                        "error": error or "",
                    }
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
        self,
        managed_turn_id: str,
        backend_turn_id: Optional[str],
        *,
        confirmed_start: bool = True,
    ) -> None:
        normalized_backend_turn_id = _coerce_text(backend_turn_id)
        runtime_started_at = now_iso()
        with self._write_conn() as conn:
            row = conn.execute(
                """
                SELECT metadata_json
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
            metadata = (
                _json_loads_object(row["metadata_json"]) if row is not None else {}
            )
            if (
                confirmed_start
                and normalized_backend_turn_id is not None
                and _coerce_text(metadata.get(_RUNTIME_STARTED_AT_KEY)) is None
            ):
                metadata[_RUNTIME_STARTED_AT_KEY] = runtime_started_at
            with conn:
                if metadata:
                    conn.execute(
                        """
                        UPDATE orch_thread_executions
                           SET backend_turn_id = ?,
                               metadata_json = ?
                         WHERE execution_id = ?
                        """,
                        (
                            normalized_backend_turn_id,
                            _json_dumps(metadata),
                            managed_turn_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE orch_thread_executions
                           SET backend_turn_id = ?
                         WHERE execution_id = ?
                        """,
                        (normalized_backend_turn_id, managed_turn_id),
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
            _fail_thread_execution_running_items(
                conn,
                source_keys=[managed_turn_id],
                completed_at=finished_at,
                error_text="interrupted",
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
        return [_execution_row_to_record(row) for row in rows]

    def has_running_turn(self, managed_thread_id: str) -> bool:
        return self.get_running_turn(managed_thread_id) is not None

    def get_running_turn(self, managed_thread_id: str) -> Optional[dict[str, Any]]:
        # Passive status checks must stay read-only so hub control-plane probes do
        # not contend on the PMA write lock or trigger the legacy mirror path.
        # Still filter out rows that are already logically stale because thread
        # state no longer points at them; mutation paths perform the actual
        # recovery when they acquire the write lock.
        with self._read_conn() as conn:
            stale_execution_ids = set(
                self._lifecycle.find_stale_running_turn_ids(
                    conn,
                    managed_thread_id,
                    include_status_turn_age_recovery=False,
                )
            )
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND status = 'running'
                 ORDER BY started_at DESC, execution_id DESC
                """,
                (managed_thread_id,),
            ).fetchall()
        for row in rows:
            execution_id = str(row["execution_id"] or "").strip()
            if execution_id and execution_id in stale_execution_ids:
                continue
            return _execution_row_to_record(row)
        return None

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
        return _execution_row_to_record(row)

    def get_turn_by_client_turn_id(
        self, managed_thread_id: str, client_turn_id: str
    ) -> Optional[dict[str, Any]]:
        normalized_client_turn_id = _coerce_text(client_turn_id)
        if normalized_client_turn_id is None:
            return None
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND client_request_id = ?
                 ORDER BY
                       CASE status
                           WHEN 'running' THEN 0
                           WHEN 'queued' THEN 1
                           ELSE 2
                       END,
                       COALESCE(started_at, created_at) DESC,
                       execution_id DESC
                 LIMIT 1
                """,
                (managed_thread_id, normalized_client_turn_id),
            ).fetchone()
        if row is None:
            return None
        return _execution_row_to_record(row)

    def get_turn_by_client_turn_id_any_thread(
        self, client_turn_id: str
    ) -> Optional[dict[str, Any]]:
        """Return the best matching execution for this client id across all threads.

        Publish dedupe keys do not vary when a PR binding is repointed to a new
        managed thread; without a global lookup, a retried enqueue could miss a
        turn created on the replacement thread and enqueue a duplicate.
        """
        normalized_client_turn_id = _coerce_text(client_turn_id)
        if normalized_client_turn_id is None:
            return None
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE client_request_id = ?
                 ORDER BY
                       CASE status
                           WHEN 'running' THEN 0
                           WHEN 'queued' THEN 1
                           ELSE 2
                       END,
                       COALESCE(started_at, created_at) DESC,
                       execution_id DESC
                 LIMIT 1
                """,
                (normalized_client_turn_id,),
            ).fetchone()
        if row is None:
            return None
        return _execution_row_to_record(row)

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
                    thread_queue_lane_id(managed_thread_id),
                    limit,
                ),
            ).fetchall()
        return [_execution_row_to_record(row) for row in rows]

    def list_pending_turn_queue_items(
        self, managed_thread_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self._read_conn() as conn:
            return self._lifecycle.list_pending_turn_queue_items(
                conn,
                managed_thread_id,
                limit=limit,
            )

    def get_queued_turn_queue_payload(
        self, managed_thread_id: str, managed_turn_id: str
    ) -> Optional[dict[str, Any]]:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT q.payload_json
                  FROM orch_queue_items AS q
                  JOIN orch_thread_executions AS e
                    ON e.execution_id = q.source_key
                 WHERE q.source_kind = 'thread_execution'
                   AND e.thread_target_id = ?
                   AND e.execution_id = ?
                   AND e.status = 'queued'
                   AND q.lane_id = ?
                   AND q.state IN ('pending', 'queued', 'waiting')
                 ORDER BY COALESCE(q.visible_at, q.created_at) ASC, q.rowid ASC
                 LIMIT 1
                """,
                (
                    managed_thread_id,
                    managed_turn_id,
                    thread_queue_lane_id(managed_thread_id),
                ),
            ).fetchone()
        if row is None:
            return None
        return _json_loads_object(row["payload_json"])

    def update_queued_turn_request(
        self,
        managed_thread_id: str,
        managed_turn_id: str,
        *,
        prompt: str,
        queue_payload: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        updated_at = now_iso()
        with self._write_conn() as conn:
            with conn:
                execution_cursor = conn.execute(
                    """
                    UPDATE orch_thread_executions
                       SET prompt_text = ?
                     WHERE execution_id = ?
                       AND thread_target_id = ?
                       AND status = 'queued'
                    """,
                    (
                        prompt,
                        managed_turn_id,
                        managed_thread_id,
                    ),
                )
                if execution_cursor.rowcount == 0:
                    return None
                queue_cursor = conn.execute(
                    """
                    UPDATE orch_queue_items
                       SET payload_json = ?,
                           updated_at = ?
                     WHERE source_kind = 'thread_execution'
                       AND source_key = ?
                       AND lane_id = ?
                       AND state IN ('pending', 'queued', 'waiting')
                    """,
                    (
                        _json_dumps(queue_payload),
                        updated_at,
                        managed_turn_id,
                        thread_queue_lane_id(managed_thread_id),
                    ),
                )
                if queue_cursor.rowcount == 0:
                    raise RuntimeError(
                        "Queued turn execution was updated but no matching queue item "
                        "was found; refusing partial commit"
                    )
                row = conn.execute(
                    """
                    SELECT *
                      FROM orch_thread_executions
                     WHERE execution_id = ?
                    """,
                    (managed_turn_id,),
                ).fetchone()
        return _execution_row_to_record(row) if row is not None else None

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
                (thread_queue_lane_id(managed_thread_id),),
            ).fetchone()
        return int((row["queue_depth"] if row is not None else 0) or 0)

    def list_thread_ids_with_running_executions(
        self, *, limit: Optional[int] = 200
    ) -> list[str]:
        if limit is not None and limit <= 0:
            return []
        limit_clause = ""
        params: list[Any] = []
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(limit)
        with self._read_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT thread_target_id, MIN(started_at) AS earliest_started_at
                  FROM orch_thread_executions
                 WHERE status = 'running'
                 GROUP BY thread_target_id
                 ORDER BY earliest_started_at ASC, thread_target_id ASC
                {limit_clause}
                """,
                params,
            ).fetchall()
        return [
            str(row["thread_target_id"])
            for row in rows
            if isinstance(row["thread_target_id"], str) and row["thread_target_id"]
        ]

    def list_thread_ids_with_pending_queue(
        self, *, limit: Optional[int] = 200
    ) -> list[str]:
        if limit is not None and limit <= 0:
            return []
        limit_clause = ""
        params: list[Any] = []
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(limit)
        with self._read_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    e.thread_target_id,
                    MIN(COALESCE(q.visible_at, q.created_at)) AS first_visible_at
                  FROM orch_queue_items AS q
                  JOIN orch_thread_executions AS e
                    ON e.execution_id = q.source_key
                 WHERE q.source_kind = 'thread_execution'
                   AND q.state IN ('pending', 'queued', 'waiting')
                 GROUP BY e.thread_target_id
                 ORDER BY first_visible_at ASC, e.thread_target_id ASC
                {limit_clause}
                """,
                params,
            ).fetchall()
        return [
            str(row["thread_target_id"])
            for row in rows
            if isinstance(row["thread_target_id"], str) and row["thread_target_id"]
        ]

    def cancel_queued_turns(self, managed_thread_id: str) -> list[str]:
        with self._write_conn() as conn:
            return self._lifecycle.cancel_queued_turns(conn, managed_thread_id)

    def cancel_queued_turn(self, managed_thread_id: str, execution_id: str) -> bool:
        with self._write_conn() as conn:
            return self._lifecycle.cancel_queued_turn(
                conn,
                managed_thread_id,
                execution_id,
            )

    def promote_queued_turn(self, managed_thread_id: str, execution_id: str) -> bool:
        with self._write_conn() as conn:
            return self._lifecycle.promote_queued_turn(
                conn,
                managed_thread_id,
                execution_id,
            )

    def claim_next_queued_turn(
        self, managed_thread_id: str
    ) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
        with self._write_conn() as conn:
            return self._lifecycle.claim_next_queued_turn(conn, managed_thread_id)

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


__all__ = [
    "PMA_THREADS_DB_FILENAME",
    "ManagedThreadAlreadyHasRunningTurnError",
    "ManagedThreadNotActiveError",
    "PmaThreadStore",
    "default_pma_threads_db_path",
    "pma_threads_db_lock",
    "pma_threads_db_lock_path",
]
