from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.reconciler import reconcile_flow_run
from codex_autorunner.core.flows.store import FlowStore


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
