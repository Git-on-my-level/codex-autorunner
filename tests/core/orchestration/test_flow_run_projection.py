import json
from pathlib import Path

from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.core.orchestration.flow_run_projection import (
    project_ticket_flow_run_records,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite


def test_ticket_flow_projection_carries_current_ticket_done_from_commit_barrier(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    record = FlowRunRecord(
        id="run-commit-barrier",
        flow_type="ticket_flow",
        status=FlowRunStatus.RUNNING,
        input_data={},
        state={
            "ticket_engine": {
                "status": "running",
                "current_ticket": ".codex-autorunner/tickets/TICKET-001.md",
                "commit": {
                    "pending": True,
                    "current_ticket_done": True,
                },
            }
        },
        metadata={},
        created_at="2026-01-01T00:00:00Z",
    )

    project_ticket_flow_run_records(
        hub_root,
        repo_root,
        "repo-1",
        [record],
        durable=False,
    )

    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        row = conn.execute(
            """
            SELECT summary_json
              FROM orch_flow_run_projections
             WHERE flow_run_id = ?
            """,
            ("run-commit-barrier",),
        ).fetchone()

    assert row is not None
    summary = json.loads(row["summary_json"])
    assert summary["ticket_engine"]["current_ticket_done"] is True


def test_ticket_flow_projection_carries_direct_current_ticket_done(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    record = FlowRunRecord(
        id="run-direct-current-ticket-done",
        flow_type="ticket_flow",
        status=FlowRunStatus.RUNNING,
        input_data={},
        state={
            "ticket_engine": {
                "status": "running",
                "current_ticket": ".codex-autorunner/tickets/TICKET-002.md",
                "current_ticket_done": True,
            }
        },
        metadata={},
        created_at="2026-01-01T00:00:00Z",
    )

    project_ticket_flow_run_records(
        hub_root,
        repo_root,
        "repo-1",
        [record],
        durable=False,
    )

    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        row = conn.execute(
            """
            SELECT summary_json
              FROM orch_flow_run_projections
             WHERE flow_run_id = ?
            """,
            ("run-direct-current-ticket-done",),
        ).fetchone()

    assert row is not None
    summary = json.loads(row["summary_json"])
    assert summary["ticket_engine"]["current_ticket_done"] is True
