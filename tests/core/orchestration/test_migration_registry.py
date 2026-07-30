"""Guardrails for the orchestration migration ladder/registry split.

`migrations.py` freezes v1-46 forever; every new durable-state migration is
meant to be added to `migrations_registry.REGISTERED_MIGRATIONS` instead of
being appended to the frozen ladder. These tests:

- fail loudly if the frozen ladder ever grows past v46 (i.e. someone
  appended a new migration directly instead of using the registry), and
- prove that databases which upgraded through history at different paces
  (a fresh install; one stamped at pre-v37; one stamped at pre-v44) all
  converge on the exact same final schema shape and the exact same
  backfilled data for equivalent legacy rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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


def _dump_schema(conn: sqlite3.Connection) -> dict[str, object]:
    """A shape fingerprint (tables/columns/indexes) independent of contents.

    Bookkeeping-table *contents* (applied_at timestamps, run ids) are
    expected to differ across upgrade paths; this intentionally only
    compares structure, which must be identical no matter how a database
    got there.
    """
    tables = [str(row["name"]) for row in conn.execute("""
            SELECT name FROM sqlite_master
             WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
             ORDER BY name
            """).fetchall()]
    columns = {
        table: tuple(
            (
                str(row["name"]),
                str(row["type"]),
                bool(row["notnull"]),
                row["dflt_value"],
                int(row["pk"]),
            )
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
        for table in tables
    }
    indexes = sorted(str(row["name"]) for row in conn.execute("""
            SELECT name FROM sqlite_master
             WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
             ORDER BY name
            """).fetchall())
    return {"tables": tables, "columns": columns, "indexes": indexes}


def _run_frozen_ladder_up_to(
    db_path: Path, boundary_version: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advance `db_path` to exactly `boundary_version` using the *real*
    frozen migration steps (truncated), simulating a database that only
    ever saw releases up through that version."""
    truncated = tuple(
        step
        for step in migrations_module._MIGRATIONS  # noqa: SLF001
        if step.version <= boundary_version
    )
    monkeypatch.setattr(migrations_module, "_MIGRATIONS", truncated)
    monkeypatch.setattr(migrations_module, "REGISTERED_MIGRATIONS", ())
    monkeypatch.setattr(
        migrations_module, "ORCHESTRATION_SCHEMA_VERSION", boundary_version
    )
    with _connect(db_path) as conn:
        apply_orchestration_migrations(conn)
        assert current_orchestration_schema_version(conn) == boundary_version
    monkeypatch.undo()


def test_frozen_ladder_max_version_is_exactly_46() -> None:
    """New durable-state migrations must go in the registry, not here.

    If someone appends a v47 step directly to `_MIGRATIONS` instead of
    `migrations_registry.REGISTERED_MIGRATIONS`, this fails (as does the
    module-level guard raised at import time in migrations.py).
    """
    versions = tuple(
        step.version for step in migrations_module._MIGRATIONS
    )  # noqa: SLF001

    assert len(versions) == 46
    assert versions == tuple(range(1, 47))
    assert versions[-1] == 46


@pytest.mark.parametrize("boundary_version", [36, 43])
def test_forward_upgrade_from_stamped_older_version_matches_fresh_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary_version: int
) -> None:
    fresh_db = tmp_path / "fresh.sqlite3"
    with _connect(fresh_db) as conn:
        fresh_version = apply_orchestration_migrations(conn)
        fresh_schema = _dump_schema(conn)
        fresh_applied = tuple(
            int(row["version"])
            for row in conn.execute(
                "SELECT version FROM orch_schema_migrations ORDER BY version"
            ).fetchall()
        )

    stamped_db = tmp_path / f"stamped_pre_{boundary_version + 1}.sqlite3"
    _run_frozen_ladder_up_to(stamped_db, boundary_version, monkeypatch)
    with _connect(stamped_db) as conn:
        stamped_version = apply_orchestration_migrations(conn)
        stamped_schema = _dump_schema(conn)
        stamped_applied = tuple(
            int(row["version"])
            for row in conn.execute(
                "SELECT version FROM orch_schema_migrations ORDER BY version"
            ).fetchall()
        )

    assert fresh_version == ORCHESTRATION_SCHEMA_VERSION
    assert stamped_version == ORCHESTRATION_SCHEMA_VERSION
    assert fresh_applied == tuple(range(1, ORCHESTRATION_SCHEMA_VERSION + 1))
    assert stamped_applied == fresh_applied
    assert stamped_schema == fresh_schema


def test_forward_upgrade_backfills_legacy_seed_row_identically_from_pre37_and_pre44(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_id = "repo-legacy-shared"
    backfilled_rows: dict[int, dict[str, object]] = {}

    for boundary_version in (36, 43):
        db_path = tmp_path / f"stamped_data_{boundary_version}.sqlite3"
        thread_target_id = f"thread-{boundary_version}"

        # Phase 1: create only the v1 shape and insert a v1-era row, exactly
        # as a real database from that era would have looked.
        _run_frozen_ladder_up_to(db_path, 1, monkeypatch)
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO orch_thread_targets (
                    thread_target_id, agent_id, backend_thread_id, repo_id,
                    workspace_root, display_name, lifecycle_status,
                    runtime_status, status_reason, status_turn_id,
                    last_execution_id, last_message_preview, compact_seed,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_target_id,
                    "codex",
                    None,
                    repo_id,
                    None,
                    "Legacy",
                    "active",
                    "idle",
                    None,
                    None,
                    None,
                    None,
                    None,
                    "2020-01-01T00:00:00Z",
                    "2020-01-01T00:00:00Z",
                ),
            )

        # Phase 2: progress the real ladder up to the historical checkpoint
        # this database is meant to simulate (pre-v37 or pre-v44).
        _run_frozen_ladder_up_to(db_path, boundary_version, monkeypatch)

        # Phase 3: finish the upgrade to the current target, unpatched.
        with _connect(db_path) as conn:
            version_after = apply_orchestration_migrations(conn)
            row = conn.execute(
                """
                SELECT scope_urn, resource_kind, resource_id, metadata_json,
                       backend_binding_json, status_terminal
                  FROM orch_thread_targets
                 WHERE thread_target_id = ?
                """,
                (thread_target_id,),
            ).fetchone()

        assert version_after == ORCHESTRATION_SCHEMA_VERSION
        assert row is not None
        backfilled_rows[boundary_version] = dict(row)

    pre37_row = backfilled_rows[36]
    pre44_row = backfilled_rows[43]

    assert pre37_row == pre44_row
    assert pre37_row["scope_urn"] == f"repo:{repo_id}"
    assert pre37_row["resource_kind"] == "repo"
    assert pre37_row["resource_id"] == repo_id
    assert pre37_row["metadata_json"] == "{}"
    assert pre37_row["backend_binding_json"] == "{}"
    assert pre37_row["status_terminal"] == 0
