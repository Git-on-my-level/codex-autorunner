from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration import (
    ORCHESTRATION_DB_FILENAME,
    ORCHESTRATION_SCHEMA_VERSION,
    initialize_orchestration_sqlite,
    list_orchestration_table_definitions,
    resolve_orchestration_sqlite_path,
)
from codex_autorunner.core.orchestration.compatibility import (
    CompatibilityRegistry,
    ProcessCompatibilityDeclaration,
    SchemaCompatibilityError,
)
from codex_autorunner.core.orchestration.sqlite import (
    OrchestrationCompatibilityMetadata,
    OrchestrationMigrationRefused,
    join_prepared_orchestration_sqlite,
    open_orchestration_sqlite,
    prepare_hub_orchestration_db_provider,
    prepare_orchestration_sqlite,
    read_orchestration_compatibility_metadata,
    resolve_orchestration_compatibility_metadata_path,
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


def _write_metadata(
    hub_root: Path,
    *,
    schema_generation: int,
    declarations: tuple[ProcessCompatibilityDeclaration, ...],
) -> None:
    path = resolve_orchestration_compatibility_metadata_path(hub_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = OrchestrationCompatibilityMetadata(
        schema_generation=schema_generation,
        prepared_at="2026-05-22T00:00:00Z",
        db_path=str(resolve_orchestration_sqlite_path(hub_root)),
        registry=CompatibilityRegistry(
            declarations=declarations,
            updated_at="2026-05-22T00:00:00Z",
        ),
    )
    path.write_text(json.dumps(metadata.to_dict(), indent=2) + "\n", encoding="utf-8")


def _hub_declaration(
    *,
    process_id: str = "hub-old",
    pid: int = 100,
    supported_schema: int = ORCHESTRATION_SCHEMA_VERSION - 1,
    expires_at: str = "2999-01-01T00:00:00Z",
) -> ProcessCompatibilityDeclaration:
    return ProcessCompatibilityDeclaration(
        process_id=process_id,
        role="hub",
        pid=pid,
        process_start_time=10.0,
        build_id="old-hub",
        unknown_build_reason=None,
        writer_identity="host:100:hub-old",
        supported_control_plane_api_version="1.0.0",
        max_supported_schema_generation=supported_schema,
        observed_schema_generation=supported_schema,
        heartbeat_at="2026-05-22T00:00:00Z",
        expires_at=expires_at,
        ttl_seconds=120,
    )


def _mark_db_at_previous_generation(hub_root: Path) -> None:
    initialize_orchestration_sqlite(hub_root, durable=False)
    with sqlite3.connect(resolve_orchestration_sqlite_path(hub_root)) as conn:
        conn.execute(
            "DELETE FROM orch_schema_migrations WHERE version = ?",
            (ORCHESTRATION_SCHEMA_VERSION,),
        )


def _schema_generation(hub_root: Path) -> int:
    with sqlite3.connect(resolve_orchestration_sqlite_path(hub_root)) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM orch_schema_migrations"
        ).fetchone()
    return int(row[0] or 0)


def _migration_run_count(hub_root: Path) -> int:
    with sqlite3.connect(resolve_orchestration_sqlite_path(hub_root)) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
              FROM orch_migration_runs
             WHERE from_version = ?
               AND target_version = ?
            """,
            (ORCHESTRATION_SCHEMA_VERSION - 1, ORCHESTRATION_SCHEMA_VERSION),
        ).fetchone()
    return int(row[0] or 0)


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
            "orch_feedback_reports",
            "orch_chat_operations",
            "orch_chat_surface_events",
        }.issubset(names)
        assert {
            "event_id",
            "provider",
            "event_type",
            "source",
            "dedupe_key",
            "comment_id",
        }.issubset(_column_names(conn, "orch_scm_events"))
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
            "comment_id",
            "delivery_id",
            "correlation_id",
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
        assert {
            "operation_id",
            "surface_kind",
            "surface_operation_key",
            "conversation_id",
            "thread_target_id",
            "state",
            "ack_completed_at",
            "first_visible_feedback_at",
            "anchor_ref",
            "interrupt_ref",
            "delivery_state",
            "delivery_cursor_json",
            "delivery_attempt_count",
            "terminal_outcome",
            "terminal_detail",
            "metadata_json",
        }.issubset(_column_names(conn, "orch_chat_operations"))
        assert {
            "event_id",
            "idempotency_key",
            "event_type",
            "surface_kind",
            "surface_key",
            "managed_thread_id",
            "external_conversation_id",
            "repo_id",
            "resource_kind",
            "resource_id",
            "workspace_root",
            "lifecycle_status",
            "status",
            "source_kind",
            "source_id",
            "occurred_at",
            "created_at",
            "payload_json",
        }.issubset(_column_names(conn, "orch_chat_surface_events"))
        assert {
            "report_id",
            "repo_id",
            "thread_target_id",
            "report_kind",
            "title",
            "body",
            "evidence_json",
            "confidence",
            "source_kind",
            "source_id",
            "dedupe_key",
            "status",
            "created_at",
            "updated_at",
        }.issubset(_column_names(conn, "orch_feedback_reports"))

    metadata = read_orchestration_compatibility_metadata(hub_root)
    assert metadata is not None
    assert metadata.schema_generation == ORCHESTRATION_SCHEMA_VERSION
    assert metadata.db_path == str(db_path)


def test_open_without_migrate_does_not_prepare_schema(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"

    with open_orchestration_sqlite(hub_root, durable=False, migrate=False) as conn:
        names = _table_names(conn)

    assert "orch_schema_migrations" not in names
    assert read_orchestration_compatibility_metadata(hub_root) is None


def test_worker_migration_refuses_to_advance_past_live_hub_schema(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    old_schema = ORCHESTRATION_SCHEMA_VERSION - 1
    _mark_db_at_previous_generation(hub_root)
    _write_metadata(
        hub_root,
        schema_generation=old_schema,
        declarations=(_hub_declaration(supported_schema=old_schema),),
    )

    with pytest.raises(OrchestrationMigrationRefused) as raised:
        with open_orchestration_sqlite(
            hub_root,
            durable=False,
            migrate=True,
            migration_mode="worker",
            process_role="worker",
            pid_start_time_matches=lambda _pid, _start: True,
        ):
            pass

    assert _schema_generation(hub_root) == old_schema
    assert raised.value.refusal.current_schema == old_schema
    assert raised.value.refusal.target_schema == ORCHESTRATION_SCHEMA_VERSION
    assert raised.value.refusal.hub_supported_schema == old_schema
    assert raised.value.evaluation.status == "restart_required"


def test_worker_prepare_refuses_to_advance_past_live_hub_schema(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    old_schema = ORCHESTRATION_SCHEMA_VERSION - 1
    _mark_db_at_previous_generation(hub_root)
    _write_metadata(
        hub_root,
        schema_generation=old_schema,
        declarations=(_hub_declaration(supported_schema=old_schema),),
    )

    with pytest.raises(OrchestrationMigrationRefused):
        prepare_orchestration_sqlite(
            hub_root,
            durable=False,
            process_role="worker",
            migration_mode="worker",
        )

    assert _schema_generation(hub_root) == old_schema


def test_worker_join_declares_compatibility_without_migration(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    prepare_hub_orchestration_db_provider(hub_root, durable=False)

    provider = join_prepared_orchestration_sqlite(
        hub_root,
        process_role="worker",
        durable=False,
    )

    with provider.open() as conn:
        assert "orch_schema_migrations" in _table_names(conn)

    metadata = read_orchestration_compatibility_metadata(hub_root)
    assert metadata is not None
    assert any(item.role == "worker" for item in metadata.registry.declarations)


def test_worker_join_rejects_newer_hub_schema(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    prepare_hub_orchestration_db_provider(hub_root, durable=False)
    with sqlite3.connect(resolve_orchestration_sqlite_path(hub_root)) as conn:
        conn.execute(
            "INSERT INTO orch_schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (ORCHESTRATION_SCHEMA_VERSION + 1, "future", "2026-05-22T00:00:00Z"),
        )

    with pytest.raises(SchemaCompatibilityError):
        join_prepared_orchestration_sqlite(
            hub_root,
            process_role="worker",
            durable=False,
        )


def test_prepared_provider_open_never_attempts_migrations_after_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    provider = prepare_hub_orchestration_db_provider(hub_root, durable=False)

    def _fail_migrate(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("prepared hot-path opens must not run migrations")

    monkeypatch.setattr(
        "codex_autorunner.core.orchestration.sqlite.apply_orchestration_migrations",
        _fail_migrate,
    )

    with provider.open() as conn:
        assert "orch_schema_migrations" in _table_names(conn)


def test_hub_migration_can_advance_under_owned_mode(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _mark_db_at_previous_generation(hub_root)

    with open_orchestration_sqlite(
        hub_root,
        durable=False,
        migrate=True,
        migration_mode="hub",
        process_role="hub",
    ) as conn:
        assert _table_names(conn)

    assert _schema_generation(hub_root) == ORCHESTRATION_SCHEMA_VERSION


def test_worker_bootstrap_ignores_stale_or_reused_hub_declarations(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _write_metadata(
        hub_root,
        schema_generation=0,
        declarations=(
            _hub_declaration(process_id="expired", expires_at="2020-01-01T00:00:00Z"),
            _hub_declaration(process_id="reused", pid=200),
        ),
    )

    with open_orchestration_sqlite(
        hub_root,
        durable=False,
        migrate=True,
        migration_mode="worker",
        process_role="worker",
        pid_start_time_matches=lambda pid, _start: pid != 200,
    ) as conn:
        assert "orch_schema_migrations" in _table_names(conn)

    assert _schema_generation(hub_root) == ORCHESTRATION_SCHEMA_VERSION


def test_concurrent_hub_migrate_callers_serialize_schema_advancement(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _mark_db_at_previous_generation(hub_root)

    def migrate() -> int:
        with open_orchestration_sqlite(
            hub_root,
            durable=False,
            migrate=True,
            migration_mode="hub",
            process_role="hub",
        ):
            return _schema_generation(hub_root)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: migrate(), range(2)))

    assert results == (
        ORCHESTRATION_SCHEMA_VERSION,
        ORCHESTRATION_SCHEMA_VERSION,
    )
    assert _schema_generation(hub_root) == ORCHESTRATION_SCHEMA_VERSION
    assert _migration_run_count(hub_root) == 1


def test_table_definition_roles_cover_authoritative_mirror_projection_and_ops() -> None:
    definitions = list_orchestration_table_definitions()
    roles = {definition.role for definition in definitions}

    assert roles == {"authoritative", "mirror", "projection", "ops"}
    assert "flows.db" not in " ".join(
        definition.description
        for definition in definitions
        if definition.role != "projection"
    )
