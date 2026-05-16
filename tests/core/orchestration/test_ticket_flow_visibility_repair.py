from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import ticket_flow_thread_metadata
from codex_autorunner.core.orchestration.ticket_flow_visibility_repair import (
    diagnose_ticket_flow_projection_gaps,
    repair_ticket_flow_chat_visibility,
)
from codex_autorunner.core.state_roots import resolve_repo_flows_db_path


def _write_ticket(repo_root: Path, name: str, *, ticket_id: str, done: bool) -> str:
    rel = f".codex-autorunner/tickets/{name}"
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f'title: "{name}"',
                'agent: "codex"',
                f"done: {'true' if done else 'false'}",
                f'ticket_id: "{ticket_id}"',
                "---",
                "",
                "## Goal",
                "- Test ticket.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return rel


def _seed_completed_run(
    repo_root: Path,
    *,
    run_id: str,
    ticket_path: str,
    state: dict | None = None,
) -> None:
    db_path = resolve_repo_flows_db_path(repo_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.create_flow_run(run_id, "ticket_flow", input_data={})
        store.create_event(
            event_id=f"{run_id}-selected",
            run_id=run_id,
            event_type=FlowEventType.STEP_PROGRESS,
            step_id="ticket_turn",
            data={"message": "Selected ticket", "current_ticket": ticket_path},
        )
        store.create_event(
            event_id=f"{run_id}-delta",
            run_id=run_id,
            event_type=FlowEventType.AGENT_STREAM_DELTA,
            step_id="ticket_turn",
            data={"delta": "done", "turn_id": "turn-1"},
        )
        store.create_event(
            event_id=f"{run_id}-completed",
            run_id=run_id,
            event_type=FlowEventType.STEP_COMPLETED,
            step_id="ticket_turn",
            data={"step_id": "ticket_turn", "next_steps": []},
        )
        store.update_flow_run_status(
            run_id,
            FlowRunStatus.COMPLETED,
            state=state or {"ticket_engine": {"last_agent_id": "codex"}},
            started_at="2026-05-15T12:00:00Z",
            finished_at="2026-05-15T12:05:00Z",
        )


def test_repair_ticket_flow_visibility_recovers_completed_run(hub_env) -> None:
    run_id = "e603d3ea-45f0-4e1b-a2ae-b82e40c5566d"
    ticket_path = _write_ticket(
        hub_env.repo_root,
        "TICKET-001.md",
        ticket_id="ticket-1",
        done=True,
    )
    _seed_completed_run(hub_env.repo_root, run_id=run_id, ticket_path=ticket_path)

    dry = repair_ticket_flow_chat_visibility(
        repo_root=hub_env.repo_root,
        hub_root=hub_env.hub_root,
        repo_id=hub_env.repo_id,
        run_id=run_id,
        dry_run=True,
    )
    assert dry.repaired == 0
    assert dry.actions[0].action == "would_repair"

    report = repair_ticket_flow_chat_visibility(
        repo_root=hub_env.repo_root,
        hub_root=hub_env.hub_root,
        repo_id=hub_env.repo_id,
        run_id=run_id,
    )

    assert report.repaired == 1
    store = ManagedThreadStore(hub_env.hub_root, durable=True)
    thread = store.get_thread(report.actions[0].managed_thread_id or "")
    assert thread is not None
    assert thread["repo_id"] == hub_env.repo_id
    assert thread["resource_kind"] == "repo"
    assert thread["resource_id"] == hub_env.repo_id
    metadata = thread["metadata"]
    assert metadata["flow_run_id"] == run_id
    assert metadata["ticket_id"] == "ticket-1"
    assert metadata["repair_provenance"]["backfilled"] is True
    assert metadata["repair_provenance"]["source"] == "flow_events"
    turns = store.list_turns(str(thread["managed_thread_id"]))
    assert turns[0]["status"] == "ok"

    again = repair_ticket_flow_chat_visibility(
        repo_root=hub_env.repo_root,
        hub_root=hub_env.hub_root,
        repo_id=hub_env.repo_id,
        run_id=run_id,
    )
    assert again.repaired == 0
    assert again.already_linked == 1
    assert again.actions[0].managed_thread_id == thread["managed_thread_id"]


def test_diagnostics_flag_completed_turn_without_canonical_link(hub_env) -> None:
    run_id = "55b8aa99-ea89-4628-a7f7-9f4166534b6f"
    ticket_path = _write_ticket(
        hub_env.repo_root,
        "TICKET-001A.md",
        ticket_id="ticket-gap",
        done=True,
    )
    _seed_completed_run(hub_env.repo_root, run_id=run_id, ticket_path=ticket_path)

    gaps = diagnose_ticket_flow_projection_gaps(
        repo_root=hub_env.repo_root,
        hub_root=hub_env.hub_root,
        repo_id=hub_env.repo_id,
        run_id=run_id,
    )

    assert len(gaps) == 1
    assert gaps[0].run_id == run_id
    assert gaps[0].ticket_id == "ticket-gap"
    assert gaps[0].expected_link_key == f"ticket_flow:{run_id}:ticket-gap"
    assert "without a canonical orchestration managed-thread link" in gaps[0].reason

    repair_ticket_flow_chat_visibility(
        repo_root=hub_env.repo_root,
        hub_root=hub_env.hub_root,
        repo_id=hub_env.repo_id,
        run_id=run_id,
    )

    assert (
        diagnose_ticket_flow_projection_gaps(
            repo_root=hub_env.repo_root,
            hub_root=hub_env.hub_root,
            repo_id=hub_env.repo_id,
            run_id=run_id,
        )
        == ()
    )


def test_repair_ticket_flow_visibility_reports_incomplete_evidence(hub_env) -> None:
    ticket_path = _write_ticket(
        hub_env.repo_root,
        "TICKET-002.md",
        ticket_id="ticket-2",
        done=False,
    )
    _seed_completed_run(hub_env.repo_root, run_id="run-2", ticket_path=ticket_path)

    report = repair_ticket_flow_chat_visibility(
        repo_root=hub_env.repo_root,
        hub_root=hub_env.hub_root,
        repo_id=hub_env.repo_id,
        run_id="run-2",
    )

    assert report.repaired == 0
    assert report.diagnostics[0].status == "unrecoverable"
    assert "missing completed ticket-turn evidence" in report.diagnostics[0].reason


def test_repair_ticket_flow_visibility_skips_already_linked_run(hub_env) -> None:
    ticket_path = _write_ticket(
        hub_env.repo_root,
        "TICKET-003.md",
        ticket_id="ticket-3",
        done=True,
    )
    _seed_completed_run(hub_env.repo_root, run_id="run-3", ticket_path=ticket_path)
    store = ManagedThreadStore(hub_env.hub_root, durable=True)
    existing = store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id=hub_env.repo_id,
        resource_kind="ticket",
        resource_id="ticket-3",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-3",
            ticket_id="ticket-3",
            workspace_root=str(hub_env.repo_root),
            repo_id=hub_env.repo_id,
            ticket_path=ticket_path,
        ),
    )

    report = repair_ticket_flow_chat_visibility(
        repo_root=hub_env.repo_root,
        hub_root=hub_env.hub_root,
        repo_id=hub_env.repo_id,
        run_id="run-3",
    )

    assert report.repaired == 0
    assert report.already_linked == 1
    assert report.actions[0].managed_thread_id == existing["managed_thread_id"]


def test_repair_ticket_flow_visibility_is_scoped_to_workspace_and_hub(
    hub_env, tmp_path
) -> None:
    ticket_path = _write_ticket(
        hub_env.repo_root,
        "TICKET-004.md",
        ticket_id="ticket-4",
        done=True,
    )
    _seed_completed_run(hub_env.repo_root, run_id="run-4", ticket_path=ticket_path)
    store = ManagedThreadStore(hub_env.hub_root, durable=True)
    other_root = tmp_path / "other-worktree"
    other_root.mkdir()
    other = store.create_thread(
        "codex",
        other_root,
        repo_id=hub_env.repo_id,
        resource_kind="ticket",
        resource_id="ticket-4",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-4",
            ticket_id="ticket-4",
            workspace_root=str(other_root),
            repo_id=hub_env.repo_id,
            ticket_path=ticket_path,
        ),
    )

    report = repair_ticket_flow_chat_visibility(
        repo_root=hub_env.repo_root,
        hub_root=hub_env.hub_root,
        repo_id=hub_env.repo_id,
        run_id="run-4",
    )

    assert report.repaired == 1
    assert report.actions[0].managed_thread_id != other["managed_thread_id"]
    rows = ManagedThreadStore(hub_env.hub_root, durable=True).list_threads(
        agent="codex",
        repo_id=hub_env.repo_id,
        limit=20,
    )
    repaired = [
        row
        for row in rows
        if (row.get("metadata") or {}).get("flow_run_id") == "run-4"
        and row.get("workspace_root") == str(hub_env.repo_root)
    ]
    assert len(repaired) == 1
