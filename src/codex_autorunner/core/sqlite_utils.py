from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Literal, Optional, Sequence

from .time_utils import now_iso

DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 5000
DEFAULT_SCHEMA_VERSION_TABLE = "schema_info"
DEFAULT_SCHEMA_MIGRATIONS_TABLE = "car_schema_migrations"
DEFAULT_SCHEMA_MIGRATION_RUNS_TABLE = "car_migration_runs"
SqliteIsolationLevel = Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"]


@dataclass(frozen=True)
class SqliteMigrationStep:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _sqlite_pragmas(*, durable: bool, busy_timeout_ms: int) -> tuple[str, ...]:
    sync = "FULL" if durable else "NORMAL"
    return (
        "PRAGMA journal_mode=WAL;",
        f"PRAGMA synchronous={sync};",
        "PRAGMA foreign_keys=ON;",
        f"PRAGMA busy_timeout={busy_timeout_ms};",
        "PRAGMA temp_store=MEMORY;",
    )


def sqlite_pragmas(
    *, durable: bool, busy_timeout_ms: int, readonly: bool = False
) -> tuple[str, ...]:
    if readonly:
        return (
            "PRAGMA foreign_keys=ON;",
            f"PRAGMA busy_timeout={busy_timeout_ms};",
            "PRAGMA temp_store=MEMORY;",
            "PRAGMA query_only=ON;",
        )
    return _sqlite_pragmas(durable=durable, busy_timeout_ms=busy_timeout_ms)


SQLITE_PRAGMAS = sqlite_pragmas(
    durable=False,
    busy_timeout_ms=DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
)
SQLITE_PRAGMAS_DURABLE = sqlite_pragmas(
    durable=True,
    busy_timeout_ms=DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
          FROM sqlite_master
         WHERE type = 'table'
           AND name = ?
         LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows if row["name"] is not None}


def ensure_columns(
    conn: sqlite3.Connection,
    table_name: str,
    columns: Iterable[tuple[str, str]],
) -> None:
    existing = table_columns(conn, table_name)
    if not existing:
        return
    for column_name, ddl in columns:
        if column_name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")
        except sqlite3.OperationalError as exc:
            if f"duplicate column name: {column_name}".lower() not in str(exc).lower():
                raise
        existing.add(column_name)


def ensure_schema_version_table(
    conn: sqlite3.Connection,
    *,
    table_name: str = DEFAULT_SCHEMA_VERSION_TABLE,
) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            version INTEGER NOT NULL PRIMARY KEY
        )
        """
    )


def read_schema_version(
    conn: sqlite3.Connection,
    *,
    table_name: str = DEFAULT_SCHEMA_VERSION_TABLE,
) -> int | None:
    if not table_exists(conn, table_name):
        return None
    row = conn.execute(
        f"SELECT version FROM {table_name} ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def write_schema_version(
    conn: sqlite3.Connection,
    version: int,
    *,
    table_name: str = DEFAULT_SCHEMA_VERSION_TABLE,
) -> None:
    ensure_schema_version_table(conn, table_name=table_name)
    conn.execute(f"DELETE FROM {table_name}")
    conn.execute(f"INSERT INTO {table_name}(version) VALUES (?)", (int(version),))


def ensure_migration_record_tables(
    conn: sqlite3.Connection,
    *,
    history_table: str = DEFAULT_SCHEMA_MIGRATIONS_TABLE,
    runs_table: str = DEFAULT_SCHEMA_MIGRATION_RUNS_TABLE,
) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {history_table} (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {runs_table} (
            run_id TEXT PRIMARY KEY,
            from_version INTEGER NOT NULL,
            target_version INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            error_text TEXT
        )
        """
    )


def apply_versioned_schema(
    conn: sqlite3.Connection,
    *,
    schema_name: str,
    target_version: int,
    steps: Sequence[SqliteMigrationStep],
    version_table: str = DEFAULT_SCHEMA_VERSION_TABLE,
    history_table: str = DEFAULT_SCHEMA_MIGRATIONS_TABLE,
    runs_table: str = DEFAULT_SCHEMA_MIGRATION_RUNS_TABLE,
) -> int:
    ensure_schema_version_table(conn, table_name=version_table)
    ensure_migration_record_tables(
        conn,
        history_table=history_table,
        runs_table=runs_table,
    )
    current_version = read_schema_version(conn, table_name=version_table)
    if current_version is None:
        write_schema_version(conn, target_version, table_name=version_table)
        return target_version
    if current_version > target_version:
        raise RuntimeError(
            f"{schema_name} schema version {current_version} is newer than supported {target_version}"
        )
    if current_version == target_version:
        return current_version

    steps_by_version = {step.version: step for step in steps}
    run_id = f"{schema_name}-{uuid.uuid4().hex}"
    conn.execute(
        f"""
        INSERT INTO {runs_table} (
            run_id,
            from_version,
            target_version,
            started_at,
            status
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, current_version, target_version, now_iso(), "running"),
    )
    try:
        for version in range(current_version + 1, target_version + 1):
            step = steps_by_version.get(version)
            if step is None:
                raise RuntimeError(
                    f"{schema_name} is missing migration step for version {version}"
                )
            step.apply(conn)
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {history_table} (
                    version,
                    name,
                    applied_at
                ) VALUES (?, ?, ?)
                """,
                (version, step.name, now_iso()),
            )
            write_schema_version(conn, version, table_name=version_table)
        conn.execute(
            f"""
            UPDATE {runs_table}
               SET finished_at = ?,
                   status = ?
             WHERE run_id = ?
            """,
            (now_iso(), "completed", run_id),
        )
    except Exception as exc:
        conn.execute(
            f"""
            UPDATE {runs_table}
               SET finished_at = ?,
                   status = ?,
                   error_text = ?
             WHERE run_id = ?
            """,
            (now_iso(), "failed", str(exc), run_id),
        )
        raise
    return target_version


def connect_sqlite(
    path: Path,
    durable: bool = False,
    *,
    busy_timeout_ms: Optional[int] = None,
    readonly: bool = False,
    check_same_thread: bool = True,
    isolation_level: SqliteIsolationLevel | None = "DEFERRED",
) -> sqlite3.Connection:
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    timeout = (
        DEFAULT_SQLITE_BUSY_TIMEOUT_MS
        if busy_timeout_ms is None
        else max(0, busy_timeout_ms)
    )
    if readonly:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(
            uri,
            uri=True,
            check_same_thread=check_same_thread,
            isolation_level=isolation_level,
        )
    else:
        conn = sqlite3.connect(
            path,
            check_same_thread=check_same_thread,
            isolation_level=isolation_level,
        )
    conn.row_factory = sqlite3.Row
    for pragma in sqlite_pragmas(
        durable=durable,
        busy_timeout_ms=timeout,
        readonly=readonly,
    ):
        conn.execute(pragma)
    return conn


@contextmanager
def open_sqlite(
    path: Path,
    durable: bool = False,
    *,
    busy_timeout_ms: Optional[int] = None,
    readonly: bool = False,
    check_same_thread: bool = True,
    isolation_level: SqliteIsolationLevel | None = "DEFERRED",
) -> Iterator[sqlite3.Connection]:
    conn = connect_sqlite(
        path,
        durable=durable,
        busy_timeout_ms=busy_timeout_ms,
        readonly=readonly,
        check_same_thread=check_same_thread,
        isolation_level=isolation_level,
    )
    try:
        yield conn
        if not readonly:
            conn.commit()
    except (
        Exception
    ):  # intentional: rollback must cover any error from yielded caller code
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


__all__ = [
    "DEFAULT_SCHEMA_MIGRATIONS_TABLE",
    "DEFAULT_SCHEMA_MIGRATION_RUNS_TABLE",
    "DEFAULT_SCHEMA_VERSION_TABLE",
    "DEFAULT_SQLITE_BUSY_TIMEOUT_MS",
    "SQLITE_PRAGMAS",
    "SQLITE_PRAGMAS_DURABLE",
    "SqliteMigrationStep",
    "apply_versioned_schema",
    "connect_sqlite",
    "ensure_columns",
    "ensure_migration_record_tables",
    "ensure_schema_version_table",
    "open_sqlite",
    "read_schema_version",
    "sqlite_pragmas",
    "table_columns",
    "table_exists",
    "write_schema_version",
]
