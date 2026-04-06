from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 5000

SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA foreign_keys=ON;",
    f"PRAGMA busy_timeout={DEFAULT_SQLITE_BUSY_TIMEOUT_MS};",
    "PRAGMA temp_store=MEMORY;",
)

SQLITE_PRAGMAS_DURABLE = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=FULL;",
    "PRAGMA foreign_keys=ON;",
    f"PRAGMA busy_timeout={DEFAULT_SQLITE_BUSY_TIMEOUT_MS};",
    "PRAGMA temp_store=MEMORY;",
)


def _sqlite_pragmas(*, durable: bool, busy_timeout_ms: int) -> tuple[str, ...]:
    sync = "FULL" if durable else "NORMAL"
    return (
        "PRAGMA journal_mode=WAL;",
        f"PRAGMA synchronous={sync};",
        "PRAGMA foreign_keys=ON;",
        f"PRAGMA busy_timeout={busy_timeout_ms};",
        "PRAGMA temp_store=MEMORY;",
    )


def connect_sqlite(
    path: Path,
    durable: bool = False,
    *,
    busy_timeout_ms: Optional[int] = None,
) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    timeout = (
        DEFAULT_SQLITE_BUSY_TIMEOUT_MS
        if busy_timeout_ms is None
        else max(0, busy_timeout_ms)
    )
    for pragma in _sqlite_pragmas(durable=durable, busy_timeout_ms=timeout):
        conn.execute(pragma)
    return conn


@contextmanager
def open_sqlite(
    path: Path,
    durable: bool = False,
    *,
    busy_timeout_ms: Optional[int] = None,
) -> Iterator[sqlite3.Connection]:
    conn = connect_sqlite(path, durable=durable, busy_timeout_ms=busy_timeout_ms)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
