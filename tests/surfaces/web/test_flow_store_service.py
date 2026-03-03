from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.surfaces.web.services import flow_store as flow_store_service


def test_flow_paths_uses_runtime_root(tmp_path) -> None:
    db_path, artifacts_root = flow_store_service.flow_paths(Path(tmp_path))
    assert db_path == Path(tmp_path) / ".codex-autorunner" / "flows.db"
    assert artifacts_root == Path(tmp_path) / ".codex-autorunner" / "flows"


def test_sync_current_ticket_paths_updates_active_run_state(tmp_path) -> None:
    repo_root = Path(tmp_path)
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    original_ticket = ".codex-autorunner/tickets/TICKET-003.md"
    moved_ticket = ".codex-autorunner/tickets/TICKET-001.md"
    run_id = "run-1"

    with FlowStore(db_path) as store:
        store.create_flow_run(
            run_id=run_id,
            flow_type="ticket_flow",
            input_data={},
            state={
                "current_ticket": original_ticket,
                "ticket_engine": {"current_ticket": original_ticket},
            },
        )
        store.update_flow_run_status(
            run_id,
            FlowRunStatus.PAUSED,
            state={
                "current_ticket": original_ticket,
                "ticket_engine": {"current_ticket": original_ticket},
            },
        )

    flow_store_service.sync_active_run_current_ticket_paths_after_reorder(
        repo_root,
        [
            (
                repo_root / ".codex-autorunner" / "tickets" / "TICKET-003.md",
                repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md",
            )
        ],
    )

    with FlowStore(db_path) as store:
        record = store.get_flow_run(run_id)

    assert record is not None
    assert record.state.get("current_ticket") == moved_ticket
    ticket_engine = record.state.get("ticket_engine")
    assert isinstance(ticket_engine, dict)
    assert ticket_engine.get("current_ticket") == moved_ticket


def test_safe_list_flow_runs_reconcile_path_calls_reconciler(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    run_id = "run-2"

    with FlowStore(db_path) as store:
        store.create_flow_run(
            run_id=run_id,
            flow_type="ticket_flow",
            input_data={},
            state={},
        )
        store.update_flow_run_status(run_id, FlowRunStatus.RUNNING, state={})

    called = {"count": 0}

    def fake_reconcile(root, record, store, logger=None):  # noqa: ANN001
        called["count"] += 1
        assert root == repo_root
        return record, False, None

    monkeypatch.setattr(flow_store_service, "reconcile_flow_run", fake_reconcile)

    records = flow_store_service.safe_list_flow_runs(
        repo_root, flow_type="ticket_flow", recover_stuck=True
    )
    assert len(records) == 1
    assert records[0].id == run_id
    assert called["count"] == 1
