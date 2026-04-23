from __future__ import annotations

import sqlite3

import pytest

from codex_autorunner.core.sqlite_utils import (
    SqliteMigrationStep,
    apply_versioned_schema,
    connect_sqlite,
    read_schema_version,
    write_schema_version,
)


def test_connect_sqlite_readonly_opens_query_only_connection(tmp_path):
    db_path = tmp_path / "sample.sqlite3"
    with connect_sqlite(db_path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES ('alpha')")

    conn = connect_sqlite(db_path, readonly=True)
    try:
        row = conn.execute("SELECT name FROM items").fetchone()
        assert row is not None
        assert row["name"] == "alpha"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO items (name) VALUES ('beta')")
    finally:
        conn.close()


def test_apply_versioned_schema_records_history_and_run_status(tmp_path):
    db_path = tmp_path / "migrations.sqlite3"
    conn = connect_sqlite(db_path)
    try:
        with conn:
            conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY)")
            write_schema_version(conn, 0)
            apply_versioned_schema(
                conn,
                schema_name="widgets",
                target_version=2,
                steps=(
                    SqliteMigrationStep(
                        version=1,
                        name="add_name",
                        apply=lambda inner: inner.execute(
                            "ALTER TABLE widgets ADD COLUMN name TEXT"
                        ),
                    ),
                    SqliteMigrationStep(
                        version=2,
                        name="add_created_at",
                        apply=lambda inner: inner.execute(
                            "ALTER TABLE widgets ADD COLUMN created_at TEXT"
                        ),
                    ),
                ),
            )

        assert read_schema_version(conn) == 2
        migration_rows = conn.execute(
            """
            SELECT version, name
              FROM car_schema_migrations
             ORDER BY version ASC
            """
        ).fetchall()
        assert [(int(row["version"]), str(row["name"])) for row in migration_rows] == [
            (1, "add_name"),
            (2, "add_created_at"),
        ]
        run_row = conn.execute(
            """
            SELECT from_version, target_version, status
              FROM car_migration_runs
             ORDER BY started_at DESC
             LIMIT 1
            """
        ).fetchone()
        assert run_row is not None
        assert int(run_row["from_version"]) == 0
        assert int(run_row["target_version"]) == 2
        assert str(run_row["status"]) == "completed"
    finally:
        conn.close()
