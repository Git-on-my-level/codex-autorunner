"""Durable, cross-process one-shot gate for legacy orchestration SQLite backfill."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Final

from ..locks import FileLock, FileLockBusy
from ..time_utils import now_iso
from .migrate_legacy_state import (
    backfill_legacy_audit_entries,
    backfill_legacy_automation_state,
    backfill_legacy_pma_lifecycle_events,
    backfill_legacy_queue_state,
    backfill_legacy_reactive_state,
    backfill_legacy_thread_state,
    backfill_legacy_transcript_mirrors,
)
from .sqlite import open_orchestration_sqlite

LEGACY_ORCHESTRATION_BACKFILL_KEY: Final[str] = "orchestration_legacy_state_v1"
_LEGACY_BACKFILL_LOCK_NAME: Final[str] = "orchestration_legacy_backfill.lock"
_LOCK_POLL_INTERVAL_SEC: Final[float] = 0.05
_DEFAULT_LOCK_TIMEOUT_SEC: Final[float] = 300.0


def legacy_orchestration_backfill_complete(conn: Any) -> bool:
    row = conn.execute(
        """
        SELECT 1 AS ok
          FROM orch_legacy_backfill_flags
         WHERE backfill_key = ?
         LIMIT 1
        """,
        (LEGACY_ORCHESTRATION_BACKFILL_KEY,),
    ).fetchone()
    return row is not None


def _legacy_backfill_lock_path(hub_root: Path) -> Path:
    d = hub_root / ".codex-autorunner"
    d.mkdir(parents=True, exist_ok=True)
    return d / _LEGACY_BACKFILL_LOCK_NAME


def _acquire_legacy_backfill_lock(lock_path: Path, *, timeout_sec: float) -> FileLock:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout_sec)
    lock = FileLock(lock_path)
    while True:
        try:
            lock.acquire(blocking=False)
            return lock
        except FileLockBusy:
            if time.monotonic() >= deadline:
                raise FileLockBusy(
                    f"Timed out acquiring legacy orchestration backfill lock ({lock_path}) "
                    f"after {timeout_sec}s"
                ) from None
            time.sleep(_LOCK_POLL_INTERVAL_SEC)


def _migrate_legacy_lifecycle_event_sources(hub_root: Path) -> bool:
    from ..lifecycle_events import migrate_legacy_lifecycle_event_sources_if_needed

    return migrate_legacy_lifecycle_event_sources_if_needed(hub_root)


def ensure_legacy_orchestration_backfill(
    hub_root: Path,
    *,
    durable: bool = True,
    lock_timeout_sec: float = _DEFAULT_LOCK_TIMEOUT_SEC,
) -> None:
    """
    Run legacy JSON/SQLite → orchestration backfills at most once per hub DB (across processes).

    Uses a row in orch_legacy_backfill_flags plus an inter-process file lock so concurrent
    starters race once, then skip cheaply on subsequent opens.
    """
    with open_orchestration_sqlite(hub_root, durable=durable) as conn:
        if legacy_orchestration_backfill_complete(conn):
            return

    lock = _acquire_legacy_backfill_lock(
        _legacy_backfill_lock_path(hub_root),
        timeout_sec=lock_timeout_sec,
    )
    try:
        with open_orchestration_sqlite(hub_root, durable=durable) as conn:
            if legacy_orchestration_backfill_complete(conn):
                return
            backfill_legacy_thread_state(hub_root, conn)
            backfill_legacy_automation_state(hub_root, conn)
            backfill_legacy_queue_state(hub_root, conn)
            backfill_legacy_reactive_state(hub_root, conn)
            backfill_legacy_transcript_mirrors(hub_root, conn)
            backfill_legacy_audit_entries(hub_root, conn)
            backfill_legacy_pma_lifecycle_events(hub_root, conn)

        if not _migrate_legacy_lifecycle_event_sources(hub_root):
            return

        with open_orchestration_sqlite(hub_root, durable=durable) as conn:
            if legacy_orchestration_backfill_complete(conn):
                return
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO orch_legacy_backfill_flags (
                        backfill_key,
                        completed_at
                    ) VALUES (?, ?)
                    """,
                    (LEGACY_ORCHESTRATION_BACKFILL_KEY, now_iso()),
                )
    finally:
        lock.release()


__all__ = [
    "LEGACY_ORCHESTRATION_BACKFILL_KEY",
    "ensure_legacy_orchestration_backfill",
    "legacy_orchestration_backfill_complete",
]
