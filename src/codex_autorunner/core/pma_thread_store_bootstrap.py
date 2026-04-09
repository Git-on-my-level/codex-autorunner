from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .locks import file_lock
from .orchestration.legacy_backfill_gate import (
    backfill_legacy_thread_state,
    ensure_legacy_orchestration_backfill,
)
from .orchestration.sqlite import open_orchestration_sqlite
from .pma_thread_mirror import legacy_mirror_enabled, sync_legacy_mirror

PMA_THREADS_DB_FILENAME = "threads.sqlite3"


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
        if not legacy_mirror_enabled():
            return
        sync_legacy_mirror(
            hub_root=self._hub_root,
            legacy_db_path=self._db_path,
            durable=self._durable,
            orchestration_conn=conn,
            thread_row_to_record=self._thread_row_to_record,
            execution_row_to_record=self._execution_row_to_record,
            ensure_legacy_schema=self._ensure_legacy_schema,
        )

    def initialize(self) -> None:
        with pma_threads_db_lock(self._db_path):
            ensure_legacy_orchestration_backfill(
                self._hub_root,
                durable=self._durable,
            )
            with open_orchestration_sqlite(
                self._hub_root,
                durable=self._durable,
            ) as conn:
                backfill_legacy_thread_state(self._hub_root, conn)
                self._run_legacy_mirror(conn)

    @contextmanager
    def read_conn(self) -> Iterator[Any]:
        ensure_legacy_orchestration_backfill(
            self._hub_root,
            durable=self._durable,
        )
        with open_orchestration_sqlite(
            self._hub_root,
            durable=self._durable,
        ) as conn:
            yield conn

    @contextmanager
    def write_conn(self) -> Iterator[Any]:
        with pma_threads_db_lock(self._db_path):
            ensure_legacy_orchestration_backfill(
                self._hub_root,
                durable=self._durable,
            )
            with open_orchestration_sqlite(
                self._hub_root,
                durable=self._durable,
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
