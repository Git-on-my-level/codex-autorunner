from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.flows.definition import FlowDefinition, StepOutcome
from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.reconciler import reconcile_flow_run
from codex_autorunner.core.flows.runtime import FlowRuntime
from codex_autorunner.core.flows.store import FlowStore


def _make_store(tmp_path: Path) -> FlowStore:
    db = tmp_path / "flows.db"
    store = FlowStore(db)
    store.initialize()
    return store


def _transition_telemetry(store: FlowStore, run_id: str) -> list[dict]:
    events = store.get_telemetry_by_type(run_id, FlowEventType.RUN_STATE_CHANGED)
    return [e.data for e in events]


def test_reconciler_noop_emits_telemetry(monkeypatch, tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = store.create_flow_run(
        run_id="run-noop",
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "running"}},
    )
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.RUNNING,
        state={"ticket_engine": {"status": "running"}},
    )

    def fake_health(repo_root, run_id):
        return SimpleNamespace(is_alive=True, status="alive", artifact_path=tmp_path)

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health", fake_health
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    reconcile_flow_run(tmp_path, current_record, store)

    telemetry = _transition_telemetry(store, record.id)
    assert any(t.get("event_type") == "reconcile_noop" for t in telemetry)

    noop_events = [t for t in telemetry if t.get("event_type") == "reconcile_noop"]
    noop = noop_events[0]
    assert noop["run_id"] == record.id
    assert noop["previous_status"] == "running"
    assert noop["resulting_status"] == "running"
    assert noop["trigger"] == "reconcile"
    assert noop["origin"] == "reconciler"


def test_reconciler_transition_emits_telemetry(monkeypatch, tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = store.create_flow_run(
        run_id="run-trans",
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "completed"}},
    )
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.RUNNING,
        state={"ticket_engine": {"status": "completed"}},
    )

    def fake_health(repo_root, run_id):
        return SimpleNamespace(is_alive=True, status="alive", artifact_path=tmp_path)

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health", fake_health
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    reconcile_flow_run(tmp_path, current_record, store)

    telemetry = _transition_telemetry(store, record.id)
    assert any(t.get("event_type") == "reconcile_transition" for t in telemetry)

    trans_events = [
        t for t in telemetry if t.get("event_type") == "reconcile_transition"
    ]
    trans = trans_events[0]
    assert trans["run_id"] == record.id
    assert trans["previous_status"] == "running"
    assert trans["resulting_status"] == "completed"
    assert trans["trigger"] == "reconcile"
    assert trans["origin"] == "reconciler"
    assert trans.get("note") is not None


def test_reconciler_dead_worker_emits_recovery_takeover(
    monkeypatch, tmp_path: Path
) -> None:
    store = _make_store(tmp_path)
    record = store.create_flow_run(
        run_id="run-dead",
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
            pid=999,
            message="worker PID not running",
            artifact_path=tmp_path,
        )

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        fake_health_dead,
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    reconcile_flow_run(tmp_path, current_record, store)

    telemetry = _transition_telemetry(store, record.id)
    assert any(t.get("event_type") == "recovery_takeover" for t in telemetry)

    recovery_events = [
        t for t in telemetry if t.get("event_type") == "recovery_takeover"
    ]
    rec = recovery_events[0]
    assert rec["run_id"] == record.id
    assert rec["previous_status"] == "running"
    assert rec["resulting_status"] == "failed"
    assert rec["trigger"] == "reconcile-recovery"
    assert rec["worker_status"] == "dead"
    assert rec["origin"] == "reconciler"

    failure_events = [
        t for t in telemetry if t.get("event_type") == "failure_projection"
    ]
    assert len(failure_events) >= 1
    fp = failure_events[0]
    assert fp["origin"] == "reconciler"
    assert fp.get("extra", {}).get("failure_reason_code") is not None or fp.get(
        "note", ""
    ).startswith("failure_reason_code=")


def test_reconciler_failure_projection_includes_reason_code(
    monkeypatch, tmp_path: Path
) -> None:
    store = _make_store(tmp_path)
    record = store.create_flow_run(
        run_id="run-fp",
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
            pid=999,
            message="worker PID not running",
            artifact_path=tmp_path,
            exit_code=137,
        )

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        fake_health_dead,
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    recovered, updated, locked = reconcile_flow_run(tmp_path, current_record, store)

    assert recovered.status == FlowRunStatus.FAILED
    assert updated is True

    telemetry = _transition_telemetry(store, record.id)
    failure_events = [
        t for t in telemetry if t.get("event_type") == "failure_projection"
    ]
    assert len(failure_events) >= 1


def test_runtime_flow_start_emits_transition_telemetry(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    async def _step(record, data):
        return StepOutcome(status=FlowRunStatus.COMPLETED, output={})

    definition = FlowDefinition(
        flow_type="test",
        initial_step="step_a",
        steps={"step_a": _step},
    )
    runtime = FlowRuntime(definition, store)
    store.create_flow_run(
        run_id="run-start",
        flow_type="test",
        input_data={},
        state={},
    )

    asyncio.run(runtime.run_flow("run-start"))

    telemetry = _transition_telemetry(store, "run-start")
    assert any(t.get("event_type") == "runtime_transition" for t in telemetry)

    start_events = [
        t
        for t in telemetry
        if t.get("event_type") == "runtime_transition"
        and t.get("trigger") == "flow_start"
    ]
    assert len(start_events) >= 1
    st = start_events[0]
    assert st["run_id"] == "run-start"
    assert st["previous_status"] == "pending"
    assert st["resulting_status"] == "running"
    assert st["origin"] == "runtime"


def test_runtime_step_completion_emits_transition_telemetry(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    async def _step(record, data):
        return StepOutcome(status=FlowRunStatus.COMPLETED, output={"result": "ok"})

    definition = FlowDefinition(
        flow_type="test",
        initial_step="step_a",
        steps={"step_a": _step},
    )
    runtime = FlowRuntime(definition, store)
    store.create_flow_run(
        run_id="run-complete",
        flow_type="test",
        input_data={},
        state={},
    )

    asyncio.run(runtime.run_flow("run-complete"))

    telemetry = _transition_telemetry(store, "run-complete")
    transitions = [t for t in telemetry if t.get("event_type") == "runtime_transition"]
    assert len(transitions) >= 2

    triggers = [t.get("trigger") for t in transitions]
    assert "flow_start" in triggers
    assert "step_complete" in triggers

    complete_events = [t for t in transitions if t.get("trigger") == "step_complete"]
    ce = complete_events[0]
    assert ce["previous_status"] == "running"
    assert ce["resulting_status"] == "completed"


def test_runtime_step_failure_emits_failure_projection(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    async def _bad_step(record, data):
        return StepOutcome(
            status=FlowRunStatus.FAILED,
            output={},
            error="something went wrong",
        )

    definition = FlowDefinition(
        flow_type="test",
        initial_step="bad_step",
        steps={"bad_step": _bad_step},
    )
    runtime = FlowRuntime(definition, store)
    store.create_flow_run(
        run_id="run-fail",
        flow_type="test",
        input_data={},
        state={},
    )

    asyncio.run(runtime.run_flow("run-fail"))

    telemetry = _transition_telemetry(store, "run-fail")

    failure_projections = [
        t for t in telemetry if t.get("event_type") == "failure_projection"
    ]
    assert len(failure_projections) >= 1
    fp = failure_projections[0]
    assert fp["origin"] == "runtime"
    assert fp["run_id"] == "run-fail"

    runtime_transitions = [
        t
        for t in telemetry
        if t.get("event_type") == "runtime_transition"
        and t.get("trigger") == "step_fail"
    ]
    assert len(runtime_transitions) >= 1
    rt = runtime_transitions[0]
    assert rt["previous_status"] == "running"
    assert rt["resulting_status"] == "failed"
    assert rt["error_message"] == "something went wrong"


def test_telemetry_distinguishes_event_types(monkeypatch, tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = store.create_flow_run(
        run_id="run-distinguish",
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "running"}},
    )
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.RUNNING,
        state={"ticket_engine": {"status": "running"}},
    )

    def fake_health(repo_root, run_id):
        return SimpleNamespace(is_alive=True, status="alive", artifact_path=tmp_path)

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health", fake_health
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None

    reconcile_flow_run(tmp_path, current_record, store)
    reconcile_flow_run(tmp_path, current_record, store)

    telemetry = _transition_telemetry(store, record.id)
    event_types = [t.get("event_type") for t in telemetry]
    assert event_types.count("reconcile_noop") == 2
    assert "reconcile_transition" not in event_types
    assert "recovery_takeover" not in event_types


def test_paused_dead_worker_emits_recovery_takeover_noop(
    monkeypatch, tmp_path: Path
) -> None:
    store = _make_store(tmp_path)
    record = store.create_flow_run(
        run_id="run-paused-dead",
        flow_type="ticket_flow",
        input_data={},
        state={"ticket_engine": {"status": "paused", "reason_code": "user_pause"}},
    )
    store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.PAUSED,
        state={"ticket_engine": {"status": "paused", "reason_code": "user_pause"}},
    )

    def fake_health_dead(repo_root, run_id):
        return SimpleNamespace(
            is_alive=False,
            status="dead",
            pid=None,
            message=None,
            artifact_path=tmp_path,
        )

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        fake_health_dead,
    )

    current_record = store.get_flow_run(record.id)
    assert current_record is not None
    reconcile_flow_run(tmp_path, current_record, store)

    telemetry = _transition_telemetry(store, record.id)
    assert any(t.get("event_type") == "recovery_takeover" for t in telemetry)

    recovery_events = [
        t for t in telemetry if t.get("event_type") == "recovery_takeover"
    ]
    rec = recovery_events[0]
    assert rec["previous_status"] == "paused"
    assert rec["resulting_status"] == "paused"
    assert rec["note"] == "paused-worker-dead-noop"
    assert rec["worker_status"] == "dead"
