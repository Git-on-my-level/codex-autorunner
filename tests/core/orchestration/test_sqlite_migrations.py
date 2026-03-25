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


def test_apply_orchestration_migrations_adds_publish_journal_tables_from_v7(
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
            INSERT INTO orch_schema_migrations (version, name, applied_at)
            VALUES (7, 'backfill_thread_target_metadata_and_resource_ownership', '2026-03-14T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        operation_table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'orch_publish_operations'
            """
        ).fetchone()
        attempt_table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'orch_publish_attempts'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert operation_table is not None
    assert attempt_table is not None


def test_apply_orchestration_migrations_adds_scm_event_table_from_v8(
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
            INSERT INTO orch_schema_migrations (version, name, applied_at)
            VALUES (8, 'add_publish_journal_tables', '2026-03-25T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        scm_event_table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'orch_scm_events'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert scm_event_table is not None


def test_apply_orchestration_migrations_backfills_resource_owner_columns(
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
                backend_thread_id TEXT,
                repo_id TEXT,
                workspace_root TEXT,
                display_name TEXT,
                lifecycle_status TEXT,
                runtime_status TEXT,
                status_reason TEXT,
                status_turn_id TEXT,
                last_execution_id TEXT,
                last_message_preview TEXT,
                compact_seed TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status_updated_at TEXT,
                status_terminal INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE orch_bindings (
                binding_id TEXT PRIMARY KEY,
                surface_kind TEXT NOT NULL,
                surface_key TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                target_id TEXT NOT NULL,
                agent_id TEXT,
                repo_id TEXT,
                mode TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                disabled_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id,
                agent_id,
                repo_id,
                workspace_root,
                created_at,
                updated_at
            ) VALUES ('thread-1', 'codex', 'repo-1', '/tmp/repo-1', '2026-03-14T00:00:00Z', '2026-03-14T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO orch_bindings (
                binding_id,
                surface_kind,
                surface_key,
                target_kind,
                target_id,
                agent_id,
                repo_id,
                mode,
                metadata_json,
                created_at,
                updated_at,
                disabled_at
            ) VALUES ('binding-1', 'discord', 'chan-1', 'thread', 'thread-1', 'codex', 'repo-1', 'reuse', '{}', '2026-03-14T00:00:00Z', '2026-03-14T00:00:00Z', NULL)
            """
        )
        conn.execute(
            """
            INSERT INTO orch_schema_migrations (version, name, applied_at)
            VALUES (5, 'enforce_active_binding_uniqueness', '2026-03-14T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        thread_row = conn.execute(
            """
            SELECT repo_id, resource_kind, resource_id
              FROM orch_thread_targets
             WHERE thread_target_id = 'thread-1'
            """
        ).fetchone()
        binding_row = conn.execute(
            """
            SELECT repo_id, resource_kind, resource_id
              FROM orch_bindings
             WHERE binding_id = 'binding-1'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert thread_row is not None
    assert thread_row["repo_id"] == "repo-1"
    assert thread_row["resource_kind"] == "repo"
    assert thread_row["resource_id"] == "repo-1"
    assert binding_row is not None
    assert binding_row["repo_id"] == "repo-1"
    assert binding_row["resource_kind"] == "repo"
    assert binding_row["resource_id"] == "repo-1"
