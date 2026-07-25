"""Shared SQLite helpers for writing orchestration schema migrations.

These are extracted from the frozen v1-46 migration ladder in
``migrations.py`` (where they were originally private module-level
functions) so that v47+ migrations registered in ``migrations_registry.py``
can reuse the same battle-tested column/backfill helpers without duplicating
logic or reaching into ``migrations.py`` internals.

``migrations.py`` re-imports these under their original private names
(``_column_not_null``, ``_ensure_column``, ``_ensure_resource_owner_columns``)
so every v1-46 call site is unchanged; this module is the single
implementation both the frozen ladder and future migrations share.
"""

from __future__ import annotations

import sqlite3

from ..sqlite_utils import table_columns, table_exists


def column_not_null(
    conn: sqlite3.Connection, table_name: str, column_name: str
) -> bool | None:
    if not table_exists(conn, table_name):
        return None
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    for row in rows:
        if str(row["name"]) == column_name:
            return bool(row["notnull"])
    return None


def ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    ddl: str,
) -> None:
    if not table_exists(conn, table_name):
        return
    if column_name in table_columns(conn, table_name):
        return
    try:
        statement = f"ALTER TABLE {table_name} "
        statement += f"ADD COLUMN {ddl}"
        conn.execute(statement)
    except sqlite3.OperationalError as exc:
        if f"duplicate column name: {column_name}".lower() not in str(exc).lower():
            raise


def ensure_resource_owner_columns(
    conn: sqlite3.Connection,
    table_name: str,
    *,
    repo_column: str = "repo_id",
) -> None:
    if not table_exists(conn, table_name):
        return
    ensure_column(conn, table_name, "resource_kind", "resource_kind TEXT")
    ensure_column(conn, table_name, "resource_id", "resource_id TEXT")
    columns = table_columns(conn, table_name)
    if repo_column not in columns:
        return
    conn.execute(f"""
        UPDATE {table_name}
           SET resource_kind = CASE
                   WHEN NULLIF(TRIM(COALESCE(resource_kind, '')), '') IS NOT NULL
                       THEN resource_kind
                   WHEN NULLIF(TRIM(COALESCE({repo_column}, '')), '') IS NOT NULL
                       THEN 'repo'
                   ELSE resource_kind
               END,
               resource_id = CASE
                   WHEN NULLIF(TRIM(COALESCE(resource_id, '')), '') IS NOT NULL
                       THEN resource_id
                   WHEN NULLIF(TRIM(COALESCE({repo_column}, '')), '') IS NOT NULL
                       THEN {repo_column}
                   ELSE resource_id
               END
         WHERE NULLIF(TRIM(COALESCE({repo_column}, '')), '') IS NOT NULL
        """)


__all__ = [
    "column_not_null",
    "ensure_column",
    "ensure_resource_owner_columns",
]
