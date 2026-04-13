from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .locks import file_lock
from .orchestration.legacy_backfill_gate import (
    ensure_legacy_orchestration_backfill,
)
from .orchestration.sqlite import (
    open_orchestration_sqlite,
    prepare_orchestration_sqlite,
)
from .pma_thread_mirror import sync_legacy_mirror
from .time_utils import now_iso

PMA_THREADS_DB_FILENAME = "threads.sqlite3"
_PMA_THREAD_STORE_PREPARED_KEY = "pma_thread_store_prepare_v1"


def default_pma_threads_db_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_THREADS_DB_FILENAME


def pma_threads_db_lock_path(db_path: Path) -> Path:
    return db_path.with_suffix(db_path.suffix + ".lock")


@contextmanager
def pma_threads_db_lock(db_path: Path) -> Iterator[None]:
    with file_lock(pma_threads_db_lock_path(db_path)):
        yield


class PmaThreadStoreBootstrap:
    def __init__(
        self,
        *,
        hub_root: Path,
        db_path: Path,
        durable: bool,
        thread_row_to_record: Callable[[Any], dict[str, Any]],
        execution_row_to_record: Callable[[Any], dict[str, Any]],
        ensure_legacy_schema: Callable[[Any], None],
    ) -> None:
        self._hub_root = hub_root
        self._db_path = db_path
        self._durable = durable
        self._thread_row_to_record = thread_row_to_record
        self._execution_row_to_record = execution_row_to_record
        self._ensure_legacy_schema = ensure_legacy_schema

    def _run_legacy_mirror(self, conn: Any) -> None:
        sync_legacy_mirror(
            hub_root=self._hub_root,
            legacy_db_path=self._db_path,
            durable=self._durable,
            orchestration_conn=conn,
            thread_row_to_record=self._thread_row_to_record,
            execution_row_to_record=self._execution_row_to_record,
            ensure_legacy_schema=self._ensure_legacy_schema,
        )

    def _prepare_marker_present(self, conn: Any) -> bool:
        row = conn.execute(
            """
            SELECT 1 AS ok
              FROM orch_legacy_backfill_flags
             WHERE backfill_key = ?
             LIMIT 1
            """,
            (_PMA_THREAD_STORE_PREPARED_KEY,),
        ).fetchone()
        return row is not None

    def _mark_prepared(self, conn: Any) -> None:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO orch_legacy_backfill_flags (
                    backfill_key,
                    completed_at
                ) VALUES (?, ?)
                """,
                (_PMA_THREAD_STORE_PREPARED_KEY, now_iso()),
            )

    def prepare(self) -> None:
        with pma_threads_db_lock(self._db_path):
            prepare_orchestration_sqlite(self._hub_root, durable=self._durable)
            ensure_legacy_orchestration_backfill(
                self._hub_root,
                durable=self._durable,
            )
            with open_orchestration_sqlite(
                self._hub_root,
                durable=self._durable,
                migrate=False,
            ) as conn:
                if self._prepare_marker_present(conn):
                    self._run_legacy_mirror(conn)
                    return
                self._run_legacy_mirror(conn)
                self._mark_prepared(conn)

    def initialize(self) -> None:
        self.prepare()

    @contextmanager
    def read_conn(self) -> Iterator[Any]:
        with open_orchestration_sqlite(
            self._hub_root,
            durable=self._durable,
            migrate=False,
        ) as conn:
            yield conn

    @contextmanager
    def write_conn(self) -> Iterator[Any]:
        with pma_threads_db_lock(self._db_path):
            with open_orchestration_sqlite(
                self._hub_root,
                durable=self._durable,
                migrate=False,
            ) as conn:
                yield conn
                self._run_legacy_mirror(conn)


__all__ = [
    "PMA_THREADS_DB_FILENAME",
    "PmaThreadStoreBootstrap",
    "default_pma_threads_db_path",
    "pma_threads_db_lock",
    "pma_threads_db_lock_path",
]
