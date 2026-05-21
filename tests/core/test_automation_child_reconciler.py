from __future__ import annotations

import asyncio
from pathlib import Path

from codex_autorunner.core.automation import (
    EXECUTOR_PMA_TURN,
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
from codex_autorunner.core.automation.execution_graph import (
    automation_execution_snapshot,
)
from codex_autorunner.core.automation.models import (
    TARGET_POLICY_EXISTING_REPO,
    TRIGGER_KIND_EVENT,
)
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_queue import PmaQueue


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


def _store_with_running_pma_queue_job(hub: Path) -> AutomationStore:
    store = AutomationStore(hub)
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-pma",
            name="PMA turn",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_EXISTING_REPO,
            executor_kind=EXECUTOR_PMA_TURN,
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-pma", event_type="manual.run")
    )
    queue_item, _ = PmaQueue(hub).enqueue_sync(
        "pma:default",
        "automation-job:job-pma",
        {"message": "run automation", "client_turn_id": "job-pma"},
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-pma",
            rule_id="rule-pma",
            event_id="event-pma",
            state=JOB_RUNNING,
            target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "repo-1"},
            executor={"kind": EXECUTOR_PMA_TURN},
            available_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            dedupe_key="job-pma",
        )
    )
    store.update_running_job(
        "job-pma",
        execution_refs={
            "pma_lane_id": queue_item.lane_id,
            "pma_queue_item_id": queue_item.item_id,
        },
    )
    return store


def test_reconciler_completes_pma_queue_job_when_queue_succeeds(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_pma_queue_job(hub)
    queue = PmaQueue(hub)
    item = queue.find_active_by_idempotency_key_sync(
        "pma:default", "automation-job:job-pma"
    )
    assert item is not None
    asyncio.run(queue.complete_item(item, {"status": "ok", "detail": "done"}))

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma")
    assert result.completed == 1
    assert saved.state == JOB_SUCCEEDED
    assert saved.result_summary == "done"


def test_reconciler_fails_pma_queue_job_when_queue_result_errors(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_pma_queue_job(hub)
    queue = PmaQueue(hub)
    item = queue.find_active_by_idempotency_key_sync(
        "pma:default", "automation-job:job-pma"
    )
    assert item is not None
    asyncio.run(queue.complete_item(item, {"status": "error", "detail": "failed"}))

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma")
    assert result.failed == 1
    assert saved.state == JOB_FAILED
    assert saved.error_text == "failed"


def test_reconciler_fails_pma_job_when_spawned_thread_fails(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_pma_queue_job(hub)
    queue = PmaQueue(hub)
    item = queue.find_active_by_idempotency_key_sync(
        "pma:default", "automation-job:job-pma"
    )
    assert item is not None
    asyncio.run(
        queue.complete_item(
            item,
            {
                "status": "ok",
                "message": "Spawned and dispatched. Thread `29998b57` is running.",
                "thread_id": "backend-session-1",
            },
        )
    )
    _insert_thread_execution(
        hub,
        thread_id="29998b57-94e9-49a4-8299-0349872e4b70",
        backend_thread_id="backend-session-1",
        execution_id="exec-1",
        status="error",
        error_text="opencode_first_event_timeout: no relevant events received within 60.0s",
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma")
    assert result.failed == 1
    assert saved.state == JOB_FAILED
    assert saved.managed_thread_target_id == "29998b57-94e9-49a4-8299-0349872e4b70"
    assert saved.managed_thread_execution_id == "exec-1"
    assert saved.error_text == (
        "opencode_first_event_timeout: no relevant events received within 60.0s"
    )


def test_execution_snapshot_links_pma_spawned_thread_prefix(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_pma_queue_job(hub)
    queue = PmaQueue(hub)
    item = queue.find_active_by_idempotency_key_sync(
        "pma:default", "automation-job:job-pma"
    )
    assert item is not None
    asyncio.run(
        queue.complete_item(
            item,
            {
                "status": "ok",
                "message": "Spawned and dispatched. Thread `29998b57` is running.",
                "thread_id": "backend-session-1",
            },
        )
    )
    _insert_thread_execution(
        hub,
        thread_id="29998b57-94e9-49a4-8299-0349872e4b70",
        backend_thread_id="backend-session-1",
        execution_id="exec-1",
        status="running",
        error_text=None,
    )

    job = store.get_job("job-pma")
    assert job is not None
    snapshot = automation_execution_snapshot(job, hub_root=hub).to_dict()

    assert snapshot["primary_child_kind"] == "pma_queue"
    assert snapshot["chat_href"] == "/chats/29998b57-94e9-49a4-8299-0349872e4b70"
    assert snapshot["managed_thread"]["latest_execution"]["status"] == "running"


def _insert_thread_execution(
    hub: Path,
    *,
    thread_id: str,
    backend_thread_id: str,
    execution_id: str,
    status: str,
    error_text: str | None,
) -> None:
    with open_orchestration_sqlite(hub, durable=True) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO orch_thread_targets (
                    thread_target_id, agent_id, backend_thread_id, repo_id,
                    workspace_root, display_name, lifecycle_status, runtime_status,
                    status_reason, status_turn_id, last_execution_id,
                    last_message_preview, created_at, updated_at, status_updated_at,
                    status_terminal, resource_kind, resource_id, metadata_json,
                    scope_urn, surface_urn, backend_binding_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    "opencode",
                    backend_thread_id,
                    "repo-1",
                    "/tmp/repo-1",
                    "weekly-tech-debt-scan",
                    "active",
                    status,
                    None,
                    None,
                    execution_id,
                    None,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:01:00Z",
                    "2026-01-01T00:01:00Z",
                    1 if status in {"error", "completed"} else 0,
                    None,
                    None,
                    "{}",
                    None,
                    None,
                    "{}",
                ),
            )
            conn.execute(
                """
                INSERT INTO orch_thread_executions (
                    execution_id, thread_target_id, client_request_id, request_kind,
                    prompt_text, status, backend_turn_id, assistant_text, error_text,
                    model_id, reasoning_level, transcript_mirror_id, started_at,
                    finished_at, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    thread_id,
                    None,
                    "message",
                    "prompt",
                    status,
                    f"{backend_thread_id}:turn-1",
                    None,
                    error_text,
                    "zai-coding-plan/glm-5.1",
                    None,
                    None,
                    "2026-01-01T00:00:00Z",
                    (
                        "2026-01-01T00:01:00Z"
                        if status in {"error", "completed"}
                        else None
                    ),
                    "2026-01-01T00:00:00Z",
                    "{}",
                ),
            )
