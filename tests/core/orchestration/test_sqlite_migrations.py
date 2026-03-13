from __future__ import annotations

import sqlite3
from pathlib import Path

from codex_autorunner.core.orchestration import (
    ORCHESTRATION_SCHEMA_VERSION,
    apply_orchestration_migrations,
    current_orchestration_schema_version,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_apply_orchestration_migrations_sets_latest_schema_version(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orchestration.sqlite3"

    with _connect(db_path) as conn:
        version = apply_orchestration_migrations(conn)
        runs = conn.execute("SELECT * FROM orch_migration_runs").fetchall()

    assert version == ORCHESTRATION_SCHEMA_VERSION
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["from_version"] == 0
    assert runs[0]["target_version"] == ORCHESTRATION_SCHEMA_VERSION


def test_apply_orchestration_migrations_upgrades_v1_database_to_latest(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orchestration.sqlite3"
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE orch_schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE orch_migration_runs (
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
        conn.execute(
            """
            CREATE TABLE orch_thread_targets (
                thread_target_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO orch_schema_migrations (version, name, applied_at)
            VALUES (1, 'create_core_orchestration_schema', '2026-03-13T00:00:00Z')
            """
        )

        version_before = current_orchestration_schema_version(conn)
        version_after = apply_orchestration_migrations(conn)
        binding_table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'orch_bindings'
            """
        ).fetchone()
        flow_projection_table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'orch_flow_run_projections'
            """
        ).fetchone()

    assert version_before == 1
    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert binding_table is not None
    assert flow_projection_table is not None


def test_apply_orchestration_migrations_is_idempotent_at_latest_version(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orchestration.sqlite3"

    with _connect(db_path) as conn:
        apply_orchestration_migrations(conn)
        first_run_count = conn.execute(
            "SELECT COUNT(*) AS count FROM orch_migration_runs"
        ).fetchone()
        version = apply_orchestration_migrations(conn)
        second_run_count = conn.execute(
            "SELECT COUNT(*) AS count FROM orch_migration_runs"
        ).fetchone()

    assert version == ORCHESTRATION_SCHEMA_VERSION
    assert int(first_run_count["count"] or 0) == 1
    assert int(second_run_count["count"] or 0) == 1
