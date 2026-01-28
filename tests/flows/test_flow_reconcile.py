from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.routes.flows import _maybe_recover_stuck_flow


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
        "codex_autorunner.routes.flows.check_worker_health", fake_health
    )

    recovered = _maybe_recover_stuck_flow(
        tmp_path, store.get_flow_run(record.id), store
    )

    assert recovered.status == FlowRunStatus.RUNNING
    engine = recovered.state.get("ticket_engine", {})
    assert engine.get("status") == "running"
    assert "reason" not in engine
