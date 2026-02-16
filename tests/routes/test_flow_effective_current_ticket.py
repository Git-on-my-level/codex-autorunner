from __future__ import annotations

import uuid
from pathlib import Path

from codex_autorunner.core.flows import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.surfaces.web.routes import flows as flow_routes


def test_runs_endpoint_surfaces_effective_current_ticket(tmp_path: Path):
    """When a step is in progress, runs status should include current_ticket.

    This protects the Tickets UI from missing an early step_progress event after reload.
    """

    repo_root = Path(tmp_path)
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    store = FlowStore(db_path)
    store.initialize()

    run_id = str(uuid.uuid4())
    store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}, state={}
    )
    store.update_flow_run_status(
        run_id, FlowRunStatus.RUNNING, state={"ticket_engine": {}}
    )

    # Step has started but has not finished.
    store.create_event(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        event_type=FlowEventType.STEP_STARTED,
        data={"step_name": "ticket_turn"},
        step_id="ticket_turn",
    )
    expected_ticket = ".codex-autorunner/tickets/TICKET-001.md"
    store.create_event(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        event_type=FlowEventType.STEP_PROGRESS,
        data={"message": "Selected ticket", "current_ticket": expected_ticket},
        step_id="ticket_turn",
    )

    record = store.get_flow_run(run_id)
    assert record is not None
    assert record.status == FlowRunStatus.RUNNING
    assert (record.state or {}).get("ticket_engine", {}).get("current_ticket") is None

    resp = flow_routes._build_flow_status_response(record, repo_root, store=store)
    ticket_engine = (resp.state or {}).get("ticket_engine") or {}
    assert ticket_engine.get("current_ticket") == expected_ticket

    store.close()
