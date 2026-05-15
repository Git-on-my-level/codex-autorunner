from __future__ import annotations

import sqlite3
from pathlib import Path

from codex_autorunner.core.orchestration import (
    ORCHESTRATION_SCHEMA_VERSION,
    apply_orchestration_migrations,
    current_orchestration_schema_version,
)
from codex_autorunner.core.orchestration import migrations as migrations_module


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


def test_apply_orchestration_migrations_adds_chat_index_projection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orchestration.sqlite3"

    with _connect(db_path) as conn:
        apply_orchestration_migrations(conn)
        table_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        index_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert "orch_chat_index_projection" in table_names
    assert "orch_chat_index_projection_meta" in table_names
    assert "idx_orch_chat_index_projection_status" in index_names
    assert "idx_orch_chat_index_projection_surface" in index_names
    assert "idx_orch_chat_index_projection_group" in index_names
    assert "idx_orch_chat_index_projection_activity" in index_names


def test_apply_orchestration_migrations_copies_legacy_backfill_flags_into_operation_flags(
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
                started_at TEXT NOT NULL,
                finished_at TEXT,
                from_version INTEGER NOT NULL,
                target_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_text TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE orch_legacy_backfill_flags (
                backfill_key TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO orch_legacy_backfill_flags (backfill_key, completed_at)
            VALUES ('legacy-flag', '2026-04-06T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO orch_schema_migrations (version, name, applied_at)
            VALUES (25, 'extend_publish_dedupe_index_for_effect_applied', '2026-04-06T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        row = conn.execute(
            """
            SELECT completed_at
              FROM orch_operation_flags
             WHERE flag_key = 'legacy-flag'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert row is not None
    assert row["completed_at"] == "2026-04-06T00:00:00Z"


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


def test_apply_orchestration_migrations_backfills_thread_projection_columns(
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
                resource_kind TEXT,
                resource_id TEXT,
                workspace_root TEXT,
                display_name TEXT,
                lifecycle_status TEXT,
                runtime_status TEXT,
                status_reason TEXT,
                status_turn_id TEXT,
                last_execution_id TEXT,
                last_message_preview TEXT,
                compact_seed TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
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
                resource_kind TEXT,
                resource_id TEXT,
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
                thread_target_id, agent_id, backend_thread_id, repo_id,
                resource_kind, resource_id, workspace_root, created_at, updated_at
            ) VALUES (
                'thread-1', 'codex', 'backend-1', 'repo-1',
                'repo', 'repo-1', '/tmp/repo', '2026-04-06T00:00:00Z',
                '2026-04-06T00:00:00Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO orch_bindings (
                binding_id, surface_kind, surface_key, target_kind, target_id,
                created_at, updated_at
            ) VALUES (
                'binding-1', 'discord', 'guild:channel:thread', 'thread',
                'thread-1', '2026-04-06T00:00:00Z', '2026-04-06T00:00:00Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO orch_schema_migrations (version, name, applied_at)
            VALUES (27, 'enforce_active_queue_item_idempotency', '2026-04-06T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        row = conn.execute(
            """
            SELECT scope_urn, surface_urn, backend_binding_json
              FROM orch_thread_targets
             WHERE thread_target_id = 'thread-1'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert row is not None
    assert row["scope_urn"] == "repo:repo-1"
    assert row["surface_urn"] == "discord:guild:channel:thread"
    assert '"backend_thread_id":"backend-1"' in row["backend_binding_json"]


def test_apply_orchestration_migrations_backfills_thread_scope_variants(
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
                resource_kind TEXT,
                resource_id TEXT,
                workspace_root TEXT,
                scope_urn TEXT,
                display_name TEXT,
                lifecycle_status TEXT,
                runtime_status TEXT,
                status_reason TEXT,
                status_turn_id TEXT,
                last_execution_id TEXT,
                last_message_preview TEXT,
                compact_seed TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status_updated_at TEXT,
                status_terminal INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        rows = [
            (
                "repo-legacy",
                "repo-1",
                None,
                None,
                "/tmp/repo-1",
                None,
            ),
            (
                "repo-resource",
                None,
                "repo",
                "repo-2",
                "/tmp/repo-2",
                None,
            ),
            (
                "worktree-preserved",
                None,
                "worktree",
                "base--feature",
                "/tmp/base--feature",
                "worktree:base/base--feature",
            ),
            (
                "workspace-only",
                None,
                None,
                None,
                "/tmp/raw workspace",
                None,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id, agent_id, repo_id, resource_kind,
                resource_id, workspace_root, scope_urn, created_at, updated_at
            ) VALUES (?, 'codex', ?, ?, ?, ?, ?, '2026-04-06T00:00:00Z', '2026-04-06T00:00:00Z')
            """,
            rows,
        )
        conn.execute(
            """
            INSERT INTO orch_schema_migrations (version, name, applied_at)
            VALUES (27, 'enforce_active_queue_item_idempotency', '2026-04-06T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        scope_rows = conn.execute(
            """
            SELECT thread_target_id, repo_id, resource_kind, resource_id, scope_urn
              FROM orch_thread_targets
             ORDER BY thread_target_id
            """
        ).fetchall()
        second_version_after = apply_orchestration_migrations(conn)
        second_scope_rows = conn.execute(
            """
            SELECT thread_target_id, repo_id, resource_kind, resource_id, scope_urn
              FROM orch_thread_targets
             ORDER BY thread_target_id
            """
        ).fetchall()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert second_version_after == ORCHESTRATION_SCHEMA_VERSION
    by_id = {row["thread_target_id"]: dict(row) for row in scope_rows}
    assert by_id["repo-legacy"] == {
        "thread_target_id": "repo-legacy",
        "repo_id": "repo-1",
        "resource_kind": "repo",
        "resource_id": "repo-1",
        "scope_urn": "repo:repo-1",
    }
    assert by_id["repo-resource"]["scope_urn"] == "repo:repo-2"
    assert by_id["worktree-preserved"]["scope_urn"] == "worktree:base/base--feature"
    assert by_id["workspace-only"]["scope_urn"] == "filesystem:/tmp/raw workspace"
    assert [dict(row) for row in second_scope_rows] == [dict(row) for row in scope_rows]


def test_ensure_column_ignores_duplicate_column_races(monkeypatch) -> None:
    class FakeConn:
        def execute(self, sql: str):
            raise sqlite3.OperationalError("duplicate column name: status_updated_at")

    monkeypatch.setattr(migrations_module, "_table_exists", lambda *_args: True)
    monkeypatch.setattr(migrations_module, "_table_columns", lambda *_args: set())

    migrations_module._ensure_column(
        FakeConn(),
        "orch_threads",
        "status_updated_at",
        "status_updated_at TEXT",
    )


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


def test_apply_orchestration_migrations_adds_chat_operation_ledger_from_v20(
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
            VALUES (20, 'refine_event_projection_execution_indexes', '2026-04-15T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'orch_chat_operations'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert table is not None


def test_apply_orchestration_migrations_adds_chat_surface_event_journal_from_v29(
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
            VALUES (29, 'purge_removed_workspace_scope_threads_and_bindings', '2026-04-15T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'orch_chat_surface_events'
            """
        ).fetchone()
        unique_index = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'index'
               AND name = 'sqlite_autoindex_orch_chat_surface_events_1'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert table is not None
    assert unique_index is not None


def test_apply_orchestration_migrations_reconciles_stale_running_executions_from_v30(
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
                lifecycle_status TEXT,
                runtime_status TEXT,
                status_updated_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE orch_thread_executions (
                execution_id TEXT PRIMARY KEY,
                thread_target_id TEXT NOT NULL,
                status TEXT NOT NULL,
                error_text TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO orch_schema_migrations (version, name, applied_at)
            VALUES (30, 'add_chat_surface_event_journal', '2026-05-11T00:00:00Z')
            """
        )
        conn.executemany(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id, lifecycle_status, runtime_status, status_updated_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    "archived-thread",
                    "archived",
                    "archived",
                    "2026-05-11T01:00:00Z",
                    "2026-05-11T01:00:00Z",
                ),
                (
                    "active-thread",
                    "active",
                    "running",
                    "2026-05-11T02:00:00Z",
                    "2026-05-11T02:00:00Z",
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO orch_thread_executions (
                execution_id, thread_target_id, status, started_at, created_at
            ) VALUES (?, ?, 'running', ?, ?)
            """,
            [
                (
                    "stale-running",
                    "archived-thread",
                    "2026-05-11T00:30:00Z",
                    "2026-05-11T00:30:00Z",
                ),
                (
                    "active-running-old",
                    "active-thread",
                    "2026-05-11T02:00:00Z",
                    "2026-05-11T02:00:00Z",
                ),
                (
                    "active-running-new",
                    "active-thread",
                    "2026-05-11T02:01:00Z",
                    "2026-05-11T02:01:00Z",
                ),
            ],
        )

        version_after = apply_orchestration_migrations(conn)
        rows = {
            row["execution_id"]: row
            for row in conn.execute(
                """
                SELECT execution_id, status, error_text, finished_at
                  FROM orch_thread_executions
                """
            ).fetchall()
        }
        unique_index = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'index'
               AND name = 'idx_orch_thread_executions_one_running'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert rows["stale-running"]["status"] == "interrupted"
    assert (
        rows["stale-running"]["error_text"]
        == "reconciled running execution after terminal thread status"
    )
    assert rows["stale-running"]["finished_at"] == "2026-05-11T01:00:00Z"
    assert rows["active-running-old"]["status"] == "interrupted"
    assert rows["active-running-new"]["status"] == "running"
    assert unique_index is not None


def test_apply_orchestration_migrations_adds_pr_binding_table_from_v9(
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
            VALUES (9, 'add_scm_event_store', '2026-03-25T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        pr_binding_table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'orch_pr_bindings'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert pr_binding_table is not None


def test_apply_orchestration_migrations_adds_reaction_state_table_from_v10(
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
            VALUES (10, 'add_pr_binding_store', '2026-03-25T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        reaction_state_table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'orch_reaction_state'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert reaction_state_table is not None


def test_apply_orchestration_migrations_adds_reaction_escalation_column_from_v11(
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
            CREATE TABLE orch_reaction_state (
                binding_id TEXT NOT NULL,
                reaction_kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                first_event_id TEXT,
                last_event_id TEXT,
                last_operation_key TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                first_emitted_at TEXT,
                last_emitted_at TEXT,
                last_delivery_failed_at TEXT,
                resolved_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                delivery_failure_count INTEGER NOT NULL DEFAULT 0,
                last_error_text TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (binding_id, reaction_kind, fingerprint)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO orch_schema_migrations (version, name, applied_at)
            VALUES (11, 'add_scm_reaction_state_store', '2026-03-25T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(orch_reaction_state)").fetchall()
        }

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert "escalated_at" in columns


def test_apply_orchestration_migrations_adds_feedback_report_table_from_v13(
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
            VALUES (13, 'add_scm_event_correlation_ids', '2026-03-26T00:00:00Z')
            """
        )

        version_after = apply_orchestration_migrations(conn)
        feedback_report_table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'orch_feedback_reports'
            """
        ).fetchone()

    assert version_after == ORCHESTRATION_SCHEMA_VERSION
    assert feedback_report_table is not None


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


def test_apply_v29_purges_removed_workspace_owner_threads_and_bindings(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orchestration.sqlite3"
    _rk = "agent_" + "workspace"

    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE orch_thread_targets (
                thread_target_id TEXT PRIMARY KEY,
                scope_urn TEXT,
                resource_kind TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE orch_bindings (
                binding_id TEXT PRIMARY KEY,
                target_kind TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            INSERT INTO orch_thread_targets (
                thread_target_id,
                scope_urn,
                resource_kind
            ) VALUES
                ('ws-scope', '{_rk}:zc-main', 'repo'),
                ('ws-kind', NULL, '{_rk}'),
                ('repo-scope', 'repo:foo', 'repo')
            """
        )
        conn.execute(
            f"""
            INSERT INTO orch_bindings (binding_id, target_kind) VALUES
                ('b-ws', '{_rk}'),
                ('b-thread', 'thread')
            """
        )
        migrations_module._apply_v29(conn)
        thread_ids = {
            str(row["thread_target_id"])
            for row in conn.execute(
                "SELECT thread_target_id FROM orch_thread_targets"
            ).fetchall()
        }
        binding_kinds = {
            str(row["target_kind"])
            for row in conn.execute("SELECT target_kind FROM orch_bindings").fetchall()
        }

    assert thread_ids == {"repo-scope"}
    assert binding_kinds == {"thread"}
