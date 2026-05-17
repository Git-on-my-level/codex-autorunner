from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.automation import (
    EXECUTOR_TICKET_FLOW,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    AutomationEvent,
    AutomationJob,
    AutomationRule,
    AutomationStore,
)
from codex_autorunner.core.automation.child_reconciler import (
    AutomationChildRunReconciler,
)
from codex_autorunner.core.automation.models import (
    TARGET_POLICY_EXISTING_REPO,
    TRIGGER_KIND_EVENT,
)
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore


def _store_with_running_ticket_flow_job(
    hub: Path, *, repo_id: str = "repo-1", run_id: str = "run-1"
) -> AutomationStore:
    store = AutomationStore(hub)
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-1",
            name="Ticket flow",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_EXISTING_REPO,
            executor_kind=EXECUTOR_TICKET_FLOW,
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-1", event_type="manual.run")
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-1",
            rule_id="rule-1",
            event_id="event-1",
            state=JOB_RUNNING,
            target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": repo_id},
            executor={"kind": EXECUTOR_TICKET_FLOW},
            available_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            dedupe_key="job-1",
        )
    )
    store.update_running_job(
        "job-1",
        execution_refs={
            "ticket_flow_repo_id": repo_id,
            "ticket_flow_worktree_id": repo_id,
            "ticket_flow_run_id": run_id,
        },
    )
    return store


def _create_child_flow(
    repo: Path,
    *,
    run_id: str = "run-1",
    status: FlowRunStatus = FlowRunStatus.COMPLETED,
    error_message: str | None = None,
) -> None:
    with FlowStore(FlowStore.default_path(repo)) as store:
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={"workspace_root": str(repo)},
            metadata={"automation_job_id": "job-1"},
        )
        store.update_flow_run_status(
            run_id,
            status,
            finished_at="2026-01-01T00:05:00Z",
            error_message=error_message,
        )


def _write_open_pr_ticket(repo: Path) -> None:
    ticket_dir = repo / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001-open-pr.md").write_text(
        """---
agent: codex
done: true
ticket_id: tkt_open_pr
ticket_kind: open_pr
title: Open PR
pr_url: https://github.com/example/repo/pull/42
---

Done.
""",
        encoding="utf-8",
    )


def test_reconciler_completes_ticket_flow_job_and_captures_pr_url(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    store = _store_with_running_ticket_flow_job(hub)
    _create_child_flow(repo, status=FlowRunStatus.COMPLETED)
    _write_open_pr_ticket(repo)

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda repo_id: repo if repo_id == "repo-1" else None
    ).reconcile_running_jobs()

    saved = store.get_job("job-1")
    assert result.completed == 1
    assert saved.state == JOB_SUCCEEDED
    assert "pr_url=https://github.com/example/repo/pull/42" in saved.result_summary


def test_reconciler_fails_parent_when_child_ticket_flow_fails(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    store = _store_with_running_ticket_flow_job(hub)
    _create_child_flow(
        repo,
        status=FlowRunStatus.FAILED,
        error_message="ticket flow failed",
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: repo
    ).reconcile_running_jobs()

    saved = store.get_job("job-1")
    assert result.failed == 1
    assert saved.state == JOB_FAILED
    assert saved.error_text == "ticket flow failed"


def test_reconciler_pauses_parent_when_child_ticket_flow_pauses(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    store = _store_with_running_ticket_flow_job(hub)
    _create_child_flow(repo, status=FlowRunStatus.PAUSED)

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: repo
    ).reconcile_running_jobs()

    saved = store.get_job("job-1")
    assert result.paused == 1
    assert saved.state == JOB_PAUSED
    assert saved.result_summary == "ticket-flow run paused: run-1"
