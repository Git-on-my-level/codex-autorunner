from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=5000;",
    "PRAGMA temp_store=MEMORY;",
)

SQLITE_PRAGMAS_DURABLE = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=FULL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=5000;",
    "PRAGMA temp_store=MEMORY;",
)


def connect_sqlite(path: Path, durable: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    pragmas = SQLITE_PRAGMAS_DURABLE if durable else SQLITE_PRAGMAS
    for pragma in pragmas:
        conn.execute(pragma)
    return conn


@contextmanager
def open_sqlite(path: Path, durable: bool = False) -> Iterator[sqlite3.Connection]:
    conn = connect_sqlite(path, durable=durable)
    try:
        yield conn
    finally:
        conn.close()
