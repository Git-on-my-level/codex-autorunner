from __future__ import annotations

import uuid
from pathlib import Path

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.reconciler import reconcile_flow_run
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.flows.worker_process import FlowWorkerHealth


def _reset_state() -> None:
    pass


def _make_alive_health(tmp_path: Path) -> FlowWorkerHealth:
    return FlowWorkerHealth(
        status="alive",
        pid=12345,
        cmdline=["python", "-m", "codex_autorunner", "flow", "worker"],
        artifact_path=tmp_path / "worker.json",
        message=None,
    )


def test_recover_running_flow_when_ticket_engine_paused(tmp_path, monkeypatch):
    _reset_state()
    repo_root = Path(tmp_path)
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        lambda *_args, **_kwargs: _make_alive_health(repo_root),
    )

    store = FlowStore(repo_root / ".codex-autorunner" / "flows.db")
    store.initialize()

    state = {"ticket_engine": {"status": "paused", "foo": "bar"}}
    record = store.create_flow_run(
        run_id=str(uuid.uuid4()),
        flow_type="ticket_flow",
        input_data={},
        metadata={},
        state=state,
        current_step="ticket_turn",
    )
    store.update_flow_run_status(
        run_id=record.id, status=FlowRunStatus.RUNNING, state=state
    )
    record = store.get_flow_run(record.id)
    assert record is not None

    updated, _, _ = reconcile_flow_run(repo_root, record, store)

    assert updated.status == FlowRunStatus.PAUSED
    expected_state = {**state, "reason_summary": "Paused"}
    assert updated.state == expected_state
    persisted = store.get_flow_run(record.id)
    assert persisted is not None
    assert persisted.status == FlowRunStatus.PAUSED
    store.close()


def test_recover_running_flow_when_ticket_engine_completed(tmp_path, monkeypatch):
    _reset_state()
    repo_root = Path(tmp_path)
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        lambda *_args, **_kwargs: _make_alive_health(repo_root),
    )

    store = FlowStore(repo_root / ".codex-autorunner" / "flows.db")
    store.initialize()

    state = {"ticket_engine": {"status": "completed"}}
    record = store.create_flow_run(
        run_id=str(uuid.uuid4()),
        flow_type="ticket_flow",
        input_data={},
        metadata={},
        state=state,
        current_step="ticket_turn",
    )
    store.update_flow_run_status(
        run_id=record.id, status=FlowRunStatus.RUNNING, state=state
    )
    record = store.get_flow_run(record.id)
    assert record is not None

    updated, _, _ = reconcile_flow_run(repo_root, record, store)

    assert updated.status == FlowRunStatus.COMPLETED
    assert updated.state == state
    assert updated.finished_at is not None
    persisted = store.get_flow_run(record.id)
    assert persisted is not None
    assert persisted.status == FlowRunStatus.COMPLETED
    assert persisted.finished_at is not None
    store.close()


def test_running_flow_with_consistent_state_is_unchanged(tmp_path, monkeypatch):
    _reset_state()
    repo_root = Path(tmp_path)
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        lambda *_args, **_kwargs: _make_alive_health(repo_root),
    )

    store = FlowStore(repo_root / ".codex-autorunner" / "flows.db")
    store.initialize()

    state = {"ticket_engine": {"status": "running"}}
    record = store.create_flow_run(
        run_id=str(uuid.uuid4()),
        flow_type="ticket_flow",
        input_data={},
        metadata={},
        state=state,
        current_step="ticket_turn",
    )
    store.update_flow_run_status(
        run_id=record.id, status=FlowRunStatus.RUNNING, state=state
    )
    record = store.get_flow_run(record.id)
    assert record is not None

    updated, _, _ = reconcile_flow_run(repo_root, record, store)

    assert updated.status == FlowRunStatus.RUNNING
    assert updated.state == state
    persisted = store.get_flow_run(record.id)
    assert persisted is not None
    assert persisted.status == FlowRunStatus.RUNNING
    store.close()
