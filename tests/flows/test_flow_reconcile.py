from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.reconciler import reconcile_flow_run
from codex_autorunner.core.flows.store import FlowStore


def test_reconcile_pending_stop_requested_without_worker_marks_stopped(
    monkeypatch, tmp_path: Path
) -> None:
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    record = store.create_flow_run(
        run_id="run-pending-stop",
        flow_type="ticket_flow",
        input_data={},
        state={},
    )
    store.update_current_step(record.id, "ticket_turn")
    store.set_stop_requested(record.id, True)

    def fake_health_dead(repo_root, run_id):
        return SimpleNamespace(
            is_alive=False,
            status="dead",
            message="worker metadata missing",
            artifact_path=tmp_path,
        )

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        fake_health_dead,
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.STOPPED
    assert recovered.finished_at is not None
    assert recovered.current_step is None
    assert recovered.state.get("reason_code") == "user_stop"
    assert updated is True
    assert locked is False


def test_recover_paused_run_when_inner_running(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    record = store.create_flow_run(
        run_id="run-1",
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "paused", "reason": "old"}},
    )
    # Simulate an already-started run that was marked paused
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.PAUSED,
        state={"ticket_engine": {"status": "running", "reason": "old"}},
    )

    def fake_health(repo_root, run_id):
        return SimpleNamespace(is_alive=True, status="alive", artifact_path=tmp_path)

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health", fake_health
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.RUNNING
    assert updated is True
    assert locked is False
    engine = recovered.state.get("ticket_engine", {})
    assert engine.get("status") == "running"
    assert "reason" not in engine


def test_dead_worker_while_running_populates_error_message(
    monkeypatch, tmp_path: Path
) -> None:
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    record = store.create_flow_run(
        run_id="run-2",
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "running"}},
    )
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.RUNNING,
        state={"ticket_engine": {"status": "running"}},
    )

    def fake_health_dead(repo_root, run_id):
        return SimpleNamespace(
            is_alive=False,
            status="dead",
            pid=12345,
            message="worker PID not running",
            artifact_path=tmp_path,
        )

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health", fake_health_dead
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.FAILED
    assert updated is True
    assert locked is False
    assert recovered.error_message is not None
    assert "Worker died" in recovered.error_message
    assert "status=dead" in recovered.error_message
    assert "pid=12345" in recovered.error_message
    assert "reason: worker PID not running" in recovered.error_message

    # Verify a flow_failed event was emitted
    events = store.get_events_by_type(record.id, FlowEventType.FLOW_FAILED)
    assert len(events) > 0
    assert events[-1].data.get("error") == recovered.error_message


def test_dead_worker_flow_failed_event_includes_last_app_event(
    monkeypatch, tmp_path: Path
) -> None:
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    record = store.create_flow_run(
        run_id="run-2b",
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "paused"}},
    )
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.RUNNING,
        state={"ticket_engine": {"status": "paused"}},
    )
    store.create_telemetry(
        telemetry_id="app-last",
        run_id=record.id,
        event_type=FlowEventType.APP_SERVER_EVENT,
        data={
            "message": {
                "method": "outputDelta",
                "params": {"turn_id": "turn-123"},
            }
        },
    )

    def fake_health_dead(repo_root, run_id):
        return SimpleNamespace(
            is_alive=False,
            status="dead",
            pid=54321,
            message="worker PID not running",
            artifact_path=tmp_path,
            exit_code=137,
        )

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health", fake_health_dead
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.FAILED
    assert updated is True
    assert locked is False
    assert recovered.error_message is not None
    assert "exit_code=137" in recovered.error_message

    events = store.get_events_by_type(record.id, FlowEventType.FLOW_FAILED)
    assert len(events) > 0
    assert events[-1].data.get("last_app_event_method") == "outputDelta"
    assert events[-1].data.get("last_turn_id") == "turn-123"


def test_dead_worker_metadata_preserves_repo_root(monkeypatch, tmp_path: Path) -> None:
    from codex_autorunner.core.flows import worker_process

    run_id = "123e4567-e89b-12d3-a456-426614174000"
    artifacts_dir = worker_process._worker_artifacts_dir(tmp_path, run_id)

    # Simulate writing metadata with repo_root
    worker_process._write_worker_metadata(
        worker_process._worker_metadata_path(artifacts_dir),
        pid=12345,
        cmd=[
            "python",
            "-m",
            "codex_autorunner",
            "flow",
            "worker",
            "--run-id",
            run_id,
            "--repo",
            str(tmp_path),
        ],
        repo_root=tmp_path,
    )

    # Read back the metadata
    import json

    metadata = json.loads(
        worker_process._worker_metadata_path(artifacts_dir).read_text()
    )

    assert metadata.get("repo_root") == str(tmp_path.resolve())
    assert metadata.get("pid") == 12345
    assert metadata.get("spawned_at") is not None
    assert metadata.get("parent_pid") is not None


def test_resume_clears_error_message(monkeypatch, tmp_path: Path) -> None:
    """When a run is resumed after failure, error_message should be cleared."""
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    record = store.create_flow_run(
        run_id="run-4",
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "running"}},
    )
    # Simulate a previously failed run that was resumed with stale error_message
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.RUNNING,
        state={"ticket_engine": {"status": "running"}},
        error_message="Previous error: Worker died (status=dead, pid=12345)",
    )

    def fake_health_alive(repo_root, run_id):
        return SimpleNamespace(is_alive=True, status="alive", artifact_path=tmp_path)

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        fake_health_alive,
    )

    # First reconcile should clear the error_message since worker is alive
    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.RUNNING
    assert updated is True
    assert locked is False
    assert recovered.error_message is None

    # Second reconcile should be a no-op (error_message already cleared)
    second_record = store.get_flow_run(record.id)
    assert second_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, second_record, store)

    assert recovered.status == FlowRunStatus.RUNNING
    assert updated is False
    assert locked is False


def test_dead_worker_restarts_same_running_ticket_flow_run(
    monkeypatch, tmp_path: Path
) -> None:
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    run_id = str(uuid.uuid4())
    record = store.create_flow_run(
        run_id=run_id,
        flow_type="ticket_flow",
        input_data={},
        state={
            "ticket_engine": {"status": "running", "current_ticket": "TICKET-001.md"}
        },
        current_step="ticket_turn",
    )
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.RUNNING,
        state=record.state,
    )
    artifact_dir = tmp_path / ".codex-autorunner" / "flows" / run_id
    artifact_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        lambda repo_root, run_id: SimpleNamespace(
            is_alive=False,
            status="dead",
            pid=12345,
            message="worker PID not running",
            artifact_path=artifact_dir / "worker.json",
        ),
    )
    spawned: list[str] = []

    def fake_spawn(repo_root: Path, run_id: str):
        spawned.append(run_id)
        return (
            SimpleNamespace(pid=999),
            SimpleNamespace(close=lambda: None),
            SimpleNamespace(close=lambda: None),
        )

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.spawn_flow_worker",
        fake_spawn,
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.RUNNING
    assert recovered.current_step == "ticket_turn"
    assert recovered.error_message is None
    assert updated is True
    assert locked is False
    assert spawned == [run_id]
    restart = recovered.state["recovery"]["restart"]
    assert restart["count"] == 1
    assert restart["max_attempts"] == 2
    assert restart["exhausted"] is False


def test_dead_worker_spawn_failure_marks_run_failed(
    monkeypatch, tmp_path: Path
) -> None:
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    run_id = str(uuid.uuid4())
    record = store.create_flow_run(
        run_id=run_id,
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "running"}},
        current_step="ticket_turn",
    )
    store.update_flow_run_status(run_id=record.id, status=FlowRunStatus.RUNNING)
    artifact_dir = tmp_path / ".codex-autorunner" / "flows" / run_id
    artifact_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        lambda repo_root, run_id: SimpleNamespace(
            is_alive=False,
            status="dead",
            pid=12345,
            message="worker PID not running",
            artifact_path=artifact_dir / "worker.json",
        ),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.spawn_flow_worker",
        lambda repo_root, run_id: (_ for _ in ()).throw(OSError("spawn boom")),
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.FAILED
    assert updated is True
    assert locked is False
    restart = recovered.state["recovery"]["restart"]
    assert restart["count"] == 1
    assert restart["last_failure_reason"] == "spawn_failed: spawn boom"


def test_dead_worker_restart_attempt_exhaustion_does_not_spawn(
    monkeypatch, tmp_path: Path
) -> None:
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    run_id = str(uuid.uuid4())
    record = store.create_flow_run(
        run_id=run_id,
        flow_type="ticket_flow",
        input_data={},
        state={
            "ticket_engine": {"status": "running"},
            "recovery": {"restart": {"count": 2, "max_attempts": 2}},
        },
        current_step="ticket_turn",
    )
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.RUNNING,
        state=record.state,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        lambda repo_root, run_id: SimpleNamespace(
            is_alive=False,
            status="dead",
            pid=12345,
            message="worker PID not running",
            artifact_path=tmp_path / "worker.json",
        ),
    )
    spawned: list[str] = []
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.spawn_flow_worker",
        lambda repo_root, run_id: spawned.append(run_id),
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.FAILED
    assert updated is True
    assert locked is False
    assert spawned == []
    restart = recovered.state["recovery"]["restart"]
    assert restart["count"] == 2
    assert restart["exhausted"] is True


def test_user_stop_requested_dead_worker_is_not_auto_restarted(
    monkeypatch, tmp_path: Path
) -> None:
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    run_id = str(uuid.uuid4())
    record = store.create_flow_run(
        run_id=run_id,
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "running"}},
        current_step="ticket_turn",
    )
    store.update_flow_run_status(run_id=record.id, status=FlowRunStatus.RUNNING)
    store.set_stop_requested(record.id, True)
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        lambda repo_root, run_id: SimpleNamespace(
            is_alive=False,
            status="dead",
            pid=12345,
            message="worker PID not running",
            artifact_path=tmp_path / "worker.json",
        ),
    )
    spawned: list[str] = []
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.spawn_flow_worker",
        lambda repo_root, run_id: spawned.append(run_id),
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.FAILED
    assert updated is True
    assert locked is False
    assert spawned == []


def test_signal_stopped_worker_is_auto_restarted(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    run_id = str(uuid.uuid4())
    record = store.create_flow_run(
        run_id=run_id,
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "running"}},
        current_step="ticket_turn",
    )
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.STOPPING,
        state=record.state,
    )
    store.set_stop_requested(record.id, True)
    artifact_dir = tmp_path / ".codex-autorunner" / "flows" / run_id
    artifact_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        lambda repo_root, run_id: SimpleNamespace(
            is_alive=False,
            status="dead",
            pid=12345,
            message="worker PID not running",
            artifact_path=artifact_dir / "worker.json",
            shutdown_intent=False,
            signal="SIGTERM",
            exit_origin="worker_signal",
            exit_kind="external_signal",
            exit_code=-15,
        ),
    )
    spawned: list[str] = []

    def fake_spawn(repo_root: Path, run_id: str):
        spawned.append(run_id)
        return (
            SimpleNamespace(pid=999),
            SimpleNamespace(close=lambda: None),
            SimpleNamespace(close=lambda: None),
        )

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.spawn_flow_worker",
        fake_spawn,
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.RUNNING
    assert recovered.stop_requested is False
    assert recovered.error_message is None
    assert updated is True
    assert locked is False
    assert spawned == [run_id]
    restart = recovered.state["recovery"]["restart"]
    assert restart["count"] == 1
    assert restart["last_reason"] == "recoverable-worker-shutdown"
