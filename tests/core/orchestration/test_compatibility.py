from __future__ import annotations

from codex_autorunner.core.orchestration import ORCHESTRATION_SCHEMA_VERSION
from codex_autorunner.core.orchestration.compatibility import (
    ProcessCompatibilityDeclaration,
    classify_registry_declarations,
    evaluate_schema_compatibility,
)
from codex_autorunner.core.orchestration.sqlite import (
    prepare_orchestration_sqlite,
    read_orchestration_compatibility_metadata,
    refresh_orchestration_process_heartbeat,
)


def test_schema_compatibility_marks_newer_db_restart_required() -> None:
    evaluation = evaluate_schema_compatibility(
        observed_schema=ORCHESTRATION_SCHEMA_VERSION + 1,
        supported_schema=ORCHESTRATION_SCHEMA_VERSION,
        process_role="hub",
        build_id="build-1",
    )

    assert evaluation.status == "incompatible_schema"
    assert evaluation.restart_required is True
    assert evaluation.observed_schema == ORCHESTRATION_SCHEMA_VERSION + 1
    assert evaluation.supported_schema == ORCHESTRATION_SCHEMA_VERSION


def test_prepare_orchestration_sqlite_records_process_declaration(tmp_path) -> None:
    hub_root = tmp_path / "hub"

    metadata = prepare_orchestration_sqlite(
        hub_root,
        durable=False,
        process_role="hub",
        control_plane_api_version="1.0.0",
    )
    reread = read_orchestration_compatibility_metadata(hub_root)

    assert reread is not None
    assert reread.schema_generation == ORCHESTRATION_SCHEMA_VERSION
    declarations = reread.registry.declarations
    assert len(declarations) == 1
    declaration = declarations[0]
    assert declaration.role == "hub"
    assert declaration.process_id
    assert declaration.pid > 0
    assert declaration.writer_identity
    assert declaration.max_supported_schema_generation == ORCHESTRATION_SCHEMA_VERSION
    assert declaration.observed_schema_generation == metadata.schema_generation
    assert declaration.heartbeat_at
    assert declaration.expires_at


def test_refresh_orchestration_process_heartbeat_preserves_process_id(
    tmp_path,
) -> None:
    hub_root = tmp_path / "hub"
    prepared = prepare_orchestration_sqlite(
        hub_root,
        durable=False,
        process_role="hub",
        heartbeat_ttl_seconds=1,
    )
    first = prepared.registry.declarations[0]

    refreshed = refresh_orchestration_process_heartbeat(
        hub_root,
        process_role="hub",
        observed_schema_generation=prepared.schema_generation,
        heartbeat_ttl_seconds=120,
    )

    second = refreshed.registry.declarations[0]
    assert second.process_id == first.process_id
    assert second.pid == first.pid
    assert second.ttl_seconds == 120
    assert second.expires_at >= first.expires_at


def test_registry_declarations_classify_stale_and_pid_reuse() -> None:
    fresh = ProcessCompatibilityDeclaration(
        process_id="fresh",
        role="hub",
        pid=100,
        process_start_time=10.0,
        build_id="build",
        unknown_build_reason=None,
        writer_identity="writer",
        supported_control_plane_api_version="1.0.0",
        max_supported_schema_generation=1,
        observed_schema_generation=1,
        heartbeat_at="2026-05-22T00:00:00Z",
        expires_at="2026-05-22T00:10:00Z",
        ttl_seconds=600,
    )
    expired = ProcessCompatibilityDeclaration.from_mapping(
        {**fresh.to_dict(), "process_id": "expired", "expires_at": "bad timestamp"}
    )
    reused_pid = ProcessCompatibilityDeclaration.from_mapping(
        {**fresh.to_dict(), "process_id": "reused", "pid": 200}
    )

    active, stale = classify_registry_declarations(
        (fresh, expired, reused_pid),
        now_timestamp=0.0,
        pid_start_time_matches=lambda pid, _start: pid != 200,
    )

    assert [item.process_id for item in active] == ["fresh"]
    assert [item.process_id for item in stale] == ["expired", "reused"]
