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
    prune_update_snapshots,
    read_orchestration_schema_info,
    restore_orchestration_db_snapshot,
    snapshot_orchestration_db,
    snapshot_orchestration_db_transaction,
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


def _write_update_snapshot(
    snapshot_root: Path,
    run_id: str,
    *,
    created_at: float,
    bytes_count: int,
) -> Path:
    snapshot_dir = snapshot_root / run_id / "orchestration"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "orchestration.sqlite3").write_bytes(b"x" * bytes_count)
    (snapshot_dir / "snapshot.json").write_text(
        json.dumps(
            {
                "snapshot_dir": str(snapshot_dir),
                "hub_root": "/hub",
                "db_path": "/hub/.codex-autorunner/orchestration.sqlite3",
                "db_existed": True,
                "copied_files": ["orchestration.sqlite3"],
                "current_schema": 1,
                "supported_schema": 1,
                "created_at": created_at,
            }
        ),
        encoding="utf-8",
    )
    return snapshot_root / run_id


def test_prune_update_snapshots_keeps_one_newest_snapshot(
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "update_snapshots"
    _write_update_snapshot(
        snapshot_root, "20260601-000000-1", created_at=100.0, bytes_count=9
    )
    _write_update_snapshot(
        snapshot_root, "20260602-000000-2", created_at=200.0, bytes_count=9
    )
    _write_update_snapshot(
        snapshot_root, "20260603-000000-3", created_at=300.0, bytes_count=9
    )

    result = prune_update_snapshots(
        snapshot_root,
        max_snapshots=1,
        open_file_checker=lambda _path: False,
    )

    assert sorted(path.name for path in snapshot_root.iterdir()) == [
        "20260603-000000-3",
    ]
    assert len(result.pruned) == 2


def test_prune_update_snapshots_protects_current_run(
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "update_snapshots"
    current = _write_update_snapshot(
        snapshot_root, "20260601-000000-1", created_at=100.0, bytes_count=9
    )
    _write_update_snapshot(
        snapshot_root, "20260602-000000-2", created_at=200.0, bytes_count=9
    )
    _write_update_snapshot(
        snapshot_root, "20260603-000000-3", created_at=300.0, bytes_count=9
    )

    result = prune_update_snapshots(
        snapshot_root,
        current_run_id=current.name,
        max_snapshots=1,
        open_file_checker=lambda _path: False,
    )

    assert current.exists()
    assert sorted(path.name for path in snapshot_root.iterdir()) == [
        "20260601-000000-1",
        "20260603-000000-3",
    ]
    assert result.pruned == (str(snapshot_root / "20260602-000000-2"),)


def test_prune_update_snapshots_skips_directories_with_open_files(
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "update_snapshots"
    open_snapshot = _write_update_snapshot(
        snapshot_root, "20260601-000000-1", created_at=100.0, bytes_count=1
    )
    _write_update_snapshot(
        snapshot_root, "20260602-000000-2", created_at=200.0, bytes_count=1
    )

    result = prune_update_snapshots(
        snapshot_root,
        max_snapshots=1,
        open_file_checker=lambda path: path == open_snapshot,
    )

    assert open_snapshot.exists()
    assert result.pruned == ()
    assert result.skipped_open == (str(open_snapshot),)


def test_prune_update_snapshots_requires_snapshot_metadata(
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "update_snapshots"
    unrelated_state = snapshot_root / "workspaces"
    unrelated_state.mkdir(parents=True)
    (unrelated_state / "state.sqlite3").write_text("keep", encoding="utf-8")
    malformed_snapshot = snapshot_root / "malformed"
    (malformed_snapshot / "orchestration").mkdir(parents=True)
    (malformed_snapshot / "orchestration" / "snapshot.json").write_text(
        '{"created_at": "not-a-number"}',
        encoding="utf-8",
    )
    _write_update_snapshot(
        snapshot_root, "20260601-000000-1", created_at=100.0, bytes_count=1
    )
    _write_update_snapshot(
        snapshot_root, "20260602-000000-2", created_at=200.0, bytes_count=1
    )

    result = prune_update_snapshots(
        snapshot_root,
        max_snapshots=1,
        open_file_checker=lambda _path: False,
    )

    assert unrelated_state.exists()
    assert malformed_snapshot.exists()
    assert sorted(Path(path).name for path in result.skipped_invalid) == [
        "malformed",
        "workspaces",
    ]
    assert result.pruned == (str(snapshot_root / "20260601-000000-1"),)


def test_snapshot_orchestration_db_transaction_reports_prune_result(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    db_path = resolve_orchestration_sqlite_path(hub_root)
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")

    snapshot_root = tmp_path / "update_snapshots"
    _write_update_snapshot(snapshot_root, "old-run", created_at=100.0, bytes_count=1)

    transaction = snapshot_orchestration_db_transaction(
        hub_root,
        snapshot_root=snapshot_root,
        run_id="new-run",
        max_snapshots=1,
    )

    assert Path(transaction.snapshot.snapshot_dir).exists()
    assert transaction.prune.pruned == (str(snapshot_root / "old-run"),)
