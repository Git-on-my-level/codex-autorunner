from __future__ import annotations

import sqlite3
from pathlib import Path

from codex_autorunner.core.orchestration import (
    ORCHESTRATION_DB_FILENAME,
    ORCHESTRATION_SCHEMA_VERSION,
    initialize_orchestration_sqlite,
    list_orchestration_table_definitions,
    resolve_orchestration_sqlite_path,
)
from codex_autorunner.core.state_roots import (
    resolve_hub_orchestration_db_path,
    resolve_hub_state_root,
)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def test_orchestration_sqlite_path_uses_hub_state_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    expected = resolve_hub_state_root(hub_root) / ORCHESTRATION_DB_FILENAME

    assert resolve_hub_orchestration_db_path(hub_root) == expected
    assert resolve_orchestration_sqlite_path(hub_root) == expected


def test_initialize_orchestration_sqlite_creates_canonical_tables(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    db_path = initialize_orchestration_sqlite(hub_root, durable=False)

    assert db_path == resolve_orchestration_sqlite_path(hub_root)
    assert db_path.name == ORCHESTRATION_DB_FILENAME
    assert db_path.exists()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        names = _table_names(conn)
        expected_tables = {
            table.name for table in list_orchestration_table_definitions()
        }
        assert expected_tables.issubset(names)
        version = conn.execute(
            "SELECT MAX(version) AS version FROM orch_schema_migrations"
        ).fetchone()
        assert int(version["version"] or 0) == ORCHESTRATION_SCHEMA_VERSION
        assert {
            "orch_publish_operations",
            "orch_publish_attempts",
            "orch_scm_events",
            "orch_pr_bindings",
            "orch_reaction_state",
        }.issubset(names)
        assert {
            "operation_id",
            "operation_key",
            "operation_kind",
            "state",
            "payload_json",
            "response_json",
            "next_attempt_at",
            "attempt_count",
        }.issubset(_column_names(conn, "orch_publish_operations"))
        assert {
            "attempt_id",
            "operation_id",
            "attempt_number",
            "state",
            "response_json",
            "claimed_at",
            "started_at",
            "finished_at",
        }.issubset(_column_names(conn, "orch_publish_attempts"))
        assert {
            "event_id",
            "provider",
            "event_type",
            "repo_slug",
            "repo_id",
            "pr_number",
            "delivery_id",
            "occurred_at",
            "received_at",
            "payload_json",
            "raw_payload_json",
            "created_at",
        }.issubset(_column_names(conn, "orch_scm_events"))
        assert {
            "binding_id",
            "provider",
            "repo_slug",
            "repo_id",
            "pr_number",
            "pr_state",
            "head_branch",
            "base_branch",
            "thread_target_id",
            "created_at",
            "updated_at",
            "closed_at",
        }.issubset(_column_names(conn, "orch_pr_bindings"))
        assert {
            "binding_id",
            "reaction_kind",
            "fingerprint",
            "state",
            "first_event_id",
            "last_event_id",
            "last_operation_key",
            "created_at",
            "updated_at",
            "first_emitted_at",
            "last_emitted_at",
            "last_delivery_failed_at",
            "escalated_at",
            "resolved_at",
            "attempt_count",
            "delivery_failure_count",
            "last_error_text",
            "metadata_json",
        }.issubset(_column_names(conn, "orch_reaction_state"))


def test_table_definition_roles_cover_authoritative_mirror_projection_and_ops() -> None:
    definitions = list_orchestration_table_definitions()
    roles = {definition.role for definition in definitions}

    assert roles == {"authoritative", "mirror", "projection", "ops"}
    assert "flows.db" not in " ".join(
        definition.description
        for definition in definitions
        if definition.role != "projection"
    )
