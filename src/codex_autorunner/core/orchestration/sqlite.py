from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..sqlite_utils import open_sqlite
from ..state_roots import (
    ORCHESTRATION_DB_FILENAME,
    resolve_hub_orchestration_db_path,
)
from .migrations import apply_orchestration_migrations

# Hub/chat orchestration DB: higher busy_timeout than generic SQLite defaults (#1266).
# Override via CAR_ORCHESTRATION_SQLITE_BUSY_TIMEOUT_MS (milliseconds, non-negative int).
_DEFAULT_ORCH_BUSY_TIMEOUT_MS = 30_000


def orchestration_sqlite_busy_timeout_ms() -> int:
    raw = os.environ.get("CAR_ORCHESTRATION_SQLITE_BUSY_TIMEOUT_MS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _DEFAULT_ORCH_BUSY_TIMEOUT_MS


def resolve_orchestration_sqlite_path(hub_root: Path) -> Path:
    """Return the canonical hub orchestration SQLite path."""
    return resolve_hub_orchestration_db_path(hub_root)


def initialize_orchestration_sqlite(hub_root: Path, *, durable: bool = True) -> Path:
    """Create or migrate the canonical orchestration SQLite database."""
    db_path = resolve_orchestration_sqlite_path(hub_root)
    with open_sqlite(
        db_path,
        durable=durable,
        busy_timeout_ms=orchestration_sqlite_busy_timeout_ms(),
    ) as conn:
        apply_orchestration_migrations(conn)
    return db_path


@contextmanager
def open_orchestration_sqlite(
    hub_root: Path,
    *,
    durable: bool = True,
    migrate: bool = True,
) -> Iterator[sqlite3.Connection]:
    """Open the canonical orchestration SQLite database."""
    db_path = resolve_orchestration_sqlite_path(hub_root)
    with open_sqlite(
        db_path,
        durable=durable,
        busy_timeout_ms=orchestration_sqlite_busy_timeout_ms(),
    ) as conn:
        if migrate:
            apply_orchestration_migrations(conn)
        yield conn


__all__ = [
    "ORCHESTRATION_DB_FILENAME",
    "initialize_orchestration_sqlite",
    "open_orchestration_sqlite",
    "orchestration_sqlite_busy_timeout_ms",
    "resolve_orchestration_sqlite_path",
]
