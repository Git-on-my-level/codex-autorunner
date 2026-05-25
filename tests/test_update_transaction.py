from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from codex_autorunner.core.orchestration.migrations import (
    ORCHESTRATION_SCHEMA_VERSION,
)
from codex_autorunner.core.orchestration.sqlite import (
    resolve_orchestration_sqlite_path,
)
from codex_autorunner.core.update_transaction import (
    read_orchestration_schema_info,
    restore_orchestration_db_snapshot,
    snapshot_orchestration_db,
    write_update_status_projection,
)


def test_write_update_status_projection_records_phase_and_preserves_notify(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "update_status.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "running",
                "message": "old",
                "notify_platform": "discord",
                "notify_context": {"chat_id": "channel-1"},
            }
        ),
        encoding="utf-8",
    )

    payload = write_update_status_projection(
        status_path,
        status="error",
        message="pip install failed",
        phase="pip_install_start",
        error_type="command_failed",
        exit_code=124,
        run_id="run-1",
    )

    assert payload["status"] == "error"
    assert payload["phase"] == "pip_install_start"
    assert payload["error_type"] == "command_failed"
    assert payload["exit_code"] == 124
    assert payload["update_run_id"] == "run-1"
    assert payload["notify_platform"] == "discord"
    assert payload["notify_context"] == {"chat_id": "channel-1"}
    assert json.loads(status_path.read_text(encoding="utf-8")) == payload


def test_write_update_status_projection_preserves_bounded_phase_timings(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "update_status.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "running",
                "message": "old",
                "phase_timings": [
                    {"phase": f"phase-{index}", "duration_ms": index}
                    for index in range(23)
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = write_update_status_projection(
        status_path,
        status="running",
        message="pip installed",
        extra={
            "phase_timing": {
                "phase": "pip_install",
                "status": "ok",
                "duration_ms": 1234,
            }
        },
    )

    assert len(payload["phase_timings"]) == 24
    assert payload["last_phase_timing"] == {
        "phase": "pip_install",
        "status": "ok",
        "duration_ms": 1234,
    }

    payload = write_update_status_projection(
        status_path,
        status="running",
        message="hub restarted",
        extra={
            "phase_timing": {
                "phase": "hub_restart",
                "status": "ok",
                "duration_ms": 4567,
            }
        },
    )

    assert len(payload["phase_timings"]) == 24
    assert payload["phase_timings"][0]["phase"] == "phase-1"
    assert payload["phase_timings"][-1]["phase"] == "hub_restart"


def test_orchestration_db_snapshot_restore_roundtrip(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    db_path = resolve_orchestration_sqlite_path(hub_root)
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE orch_schema_migrations (version INTEGER)")
        conn.execute("INSERT INTO orch_schema_migrations(version) VALUES (31)")
        conn.execute("CREATE TABLE marker (value TEXT)")
        conn.execute("INSERT INTO marker(value) VALUES ('before')")

    info = read_orchestration_schema_info(hub_root)
    assert info.db_exists is True
    assert info.current_schema == 31
    assert info.supported_schema == ORCHESTRATION_SCHEMA_VERSION

    snapshot = snapshot_orchestration_db(
        hub_root,
        snapshot_root=tmp_path / "snapshots",
        run_id="run-1",
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE marker SET value = 'after'")
        conn.execute("INSERT INTO orch_schema_migrations(version) VALUES (32)")

    restored = restore_orchestration_db_snapshot(Path(snapshot.snapshot_dir))

    assert restored.db_path == str(db_path)
    with sqlite3.connect(db_path) as conn:
        value = conn.execute("SELECT value FROM marker").fetchone()[0]
        version = conn.execute(
            "SELECT MAX(version) FROM orch_schema_migrations"
        ).fetchone()[0]
    assert value == "before"
    assert version == 31


def test_orchestration_db_snapshot_restore_removes_new_db_when_absent(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    db_path = resolve_orchestration_sqlite_path(hub_root)

    snapshot = snapshot_orchestration_db(
        hub_root,
        snapshot_root=tmp_path / "snapshots",
        run_id="run-absent",
    )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")

    restore_orchestration_db_snapshot(Path(snapshot.snapshot_dir))

    assert not db_path.exists()
