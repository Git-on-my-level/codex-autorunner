from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.automation import (
    EXECUTOR_AGENT_TASK_TURN,
    EXECUTOR_TICKET_FLOW,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
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
    automation_execution_snapshots_by_job_id,
)
from codex_autorunner.core.automation.models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    AUTOMATION_CHILD_KIND_PMA_OPERATOR,
    AUTOMATION_CHILD_KIND_TICKET_FLOW,
    EXECUTOR_PMA_OPERATOR_TURN,
    LEGACY_EXECUTOR_PMA_TURN,
    TARGET_POLICY_EXISTING_REPO,
    TRIGGER_KIND_EVENT,
    AutomationChildExecutionEdge,
    AutomationRuntimeContract,
)
from codex_autorunner.core.diagnostics.automation import (
    AUTOMATION_CHILD_EDGE_MISSING,
    AUTOMATION_PARENT_STATE_STALE,
    AUTOMATION_RUNTIME_MISMATCH,
    collect_automation_architecture_diagnostics,
)
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite


def _store_with_running_ticket_flow_job(
    hub: Path, *, repo_id: str = "repo-1", run_id: str = "run-1"
) -> AutomationStore:
    store = AutomationStore(hub)
    store.upsert_rule(
        AutomationRule.hydrate_persisted(
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
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-1",
            child_kind=AUTOMATION_CHILD_KIND_TICKET_FLOW,
            child_id=run_id,
            requested_runtime=AutomationRuntimeContract(
                workspace_scope={"repo_id": repo_id}
            ),
            actual_runtime=None,
            terminal_mapping={
                "succeeded": "succeeded",
                "failed": "failed",
                "interrupted": JOB_PAUSED,
                "cancelled": "cancelled",
            },
        )
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


def test_reconciler_closes_parent_from_durable_managed_thread_edge(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_managed_thread_job(hub)
    store.update_running_job(
        "job-pma",
        execution_refs={"managed_thread_target_id": None},
    )
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-pma",
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="exec-1",
            requested_runtime=AutomationRuntimeContract(
                agent="opencode",
                model="zai-coding-plan/glm-5.1",
            ),
            actual_runtime=None,
        )
    )
    _insert_thread_execution(
        hub,
        thread_id="29998b57-94e9-49a4-8299-0349872e4b70",
        backend_thread_id="backend-session-1",
        execution_id="exec-1",
        status="ok",
        error_text=None,
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma")
    edges = store.list_child_execution_edges("job-pma")
    assert result.completed == 1
    assert saved.state == JOB_SUCCEEDED
    assert not hasattr(saved, "managed_thread_execution_id")
    assert edges[0].terminal_state == "succeeded"
    assert edges[0].actual_runtime.model == "zai-coding-plan/glm-5.1"


def test_reducer_fails_authoritative_agent_task_runtime_mismatch(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = AutomationStore(hub)
    store.upsert_rule(
        AutomationRule.hydrate_persisted(
            rule_id="rule-agent-task",
            name="Direct agent task",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_EXISTING_REPO,
            executor_kind=EXECUTOR_AGENT_TASK_TURN,
            executor={
                "kind": EXECUTOR_AGENT_TASK_TURN,
                "requested_runtime": {
                    "agent": "opencode",
                    "model": "zai-coding-plan/glm-5.1",
                },
            },
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-agent-task", event_type="manual.run")
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-agent-task",
            rule_id="rule-agent-task",
            event_id="event-agent-task",
            state=JOB_RUNNING,
            target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "repo-1"},
            executor={
                "kind": EXECUTOR_AGENT_TASK_TURN,
                "requested_runtime": {
                    "agent": "opencode",
                    "model": "zai-coding-plan/glm-5.1",
                },
            },
            available_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            dedupe_key="job-agent-task",
        )
    )
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-agent-task",
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="exec-agent-task",
            requested_runtime={"agent": "opencode", "model": "zai-coding-plan/glm-5.1"},
            actual_runtime={"agent": "codex", "model": "gpt-5.5"},
            terminal_state="succeeded",
            terminal_observed_at="2026-01-01T00:05:00Z",
        )
    )

    reduced = store.reduce_parent_job_from_children("job-agent-task")

    assert reduced is not None
    assert reduced.state == JOB_FAILED
    assert "runtime mismatch" in (reduced.error_text or "")


def test_reducer_fails_terminal_agent_task_without_actual_runtime(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = AutomationStore(hub)
    store.upsert_rule(
        AutomationRule.hydrate_persisted(
            rule_id="rule-agent-task-missing-actual",
            name="Agent task missing actual",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_EXISTING_REPO,
            executor_kind=EXECUTOR_AGENT_TASK_TURN,
        )
    )
    store.record_event(
        AutomationEvent.create(
            event_id="event-agent-task-missing-actual", event_type="manual.run"
        )
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-agent-task-missing-actual",
            rule_id="rule-agent-task-missing-actual",
            event_id="event-agent-task-missing-actual",
            state=JOB_RUNNING,
            target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "repo-1"},
            executor={
                "kind": EXECUTOR_AGENT_TASK_TURN,
                "requested_runtime": {
                    "agent": "opencode",
                    "model": "zai-coding-plan/glm-5.1",
                },
            },
            available_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            dedupe_key="job-agent-task-missing-actual",
        )
    )
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-agent-task-missing-actual",
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="exec-agent-task-missing-actual",
            requested_runtime={"agent": "opencode", "model": "zai-coding-plan/glm-5.1"},
            actual_runtime=None,
            terminal_state="succeeded",
            terminal_observed_at="2026-01-01T00:05:00Z",
        )
    )

    reduced = store.reduce_parent_job_from_children("job-agent-task-missing-actual")

    assert reduced is not None
    assert reduced.state == JOB_FAILED
    assert "runtime mismatch" in (reduced.error_text or "")


def test_automation_architecture_diagnostics_cover_graph_invariants(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = AutomationStore(hub)
    store.upsert_rule(
        AutomationRule.hydrate_persisted(
            rule_id="rule-diagnostics",
            name="Diagnostics",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_EXISTING_REPO,
            executor_kind=EXECUTOR_AGENT_TASK_TURN,
            executor={"kind": EXECUTOR_AGENT_TASK_TURN},
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-diagnostics", event_type="manual.run")
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-stale",
            rule_id="rule-diagnostics",
            event_id="event-diagnostics",
            state=JOB_RUNNING,
            target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "repo-1"},
            executor={"kind": EXECUTOR_AGENT_TASK_TURN},
            available_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            dedupe_key="job-stale",
        )
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-missing-edge",
            rule_id="rule-diagnostics",
            event_id="event-diagnostics",
            state=JOB_RUNNING,
            target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "repo-1"},
            executor={"kind": EXECUTOR_AGENT_TASK_TURN},
            available_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            dedupe_key="job-missing-edge",
        )
    )
    store.update_running_job(
        "job-missing-edge",
        execution_refs={"managed_thread_execution_id": "exec-missing-edge"},
    )
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-stale",
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="exec-stale",
            requested_runtime={"agent": "opencode", "model": "zai-coding-plan/glm-5.1"},
            actual_runtime={"agent": "codex", "model": "gpt-5.5"},
            terminal_state="succeeded",
            terminal_observed_at="2026-01-01T00:05:00Z",
        )
    )

    diagnostics = collect_automation_architecture_diagnostics(hub)

    codes = {item.code for item in diagnostics}
    assert AUTOMATION_PARENT_STATE_STALE in codes
    assert AUTOMATION_RUNTIME_MISMATCH in codes
    assert AUTOMATION_CHILD_EDGE_MISSING in codes


def test_reconciler_closes_parent_from_durable_ticket_flow_edge(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    store = _store_with_running_ticket_flow_job(hub)
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-1",
            child_kind=AUTOMATION_CHILD_KIND_TICKET_FLOW,
            child_id="run-1",
            requested_runtime=AutomationRuntimeContract(
                workspace_scope={"repo_id": "repo-1"}
            ),
            actual_runtime=AutomationRuntimeContract(
                workspace_scope={"repo_id": "repo-1"}
            ),
            terminal_mapping={
                "succeeded": JOB_SUCCEEDED,
                "failed": JOB_FAILED,
                "interrupted": JOB_PAUSED,
                "cancelled": "cancelled",
            },
        )
    )
    _create_child_flow(repo, status=FlowRunStatus.COMPLETED)

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda repo_id: repo if repo_id == "repo-1" else None
    ).reconcile_running_jobs()

    saved = store.get_job("job-1")
    edge = store.list_child_execution_edges("job-1")[0]
    assert result.completed == 1
    assert saved.state == JOB_SUCCEEDED
    assert edge.terminal_state == "succeeded"


def test_reconciler_closes_authoritative_pma_operator_without_worker_child(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = AutomationStore(hub)
    store.upsert_rule(
        AutomationRule.hydrate_persisted(
            rule_id="rule-pma-operator",
            name="PMA operator",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_EXISTING_REPO,
            executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-pma-operator", event_type="manual.run")
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-pma-operator",
            rule_id="rule-pma-operator",
            event_id="event-pma-operator",
            state=JOB_RUNNING,
            target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "repo-1"},
            executor={"kind": EXECUTOR_PMA_OPERATOR_TURN},
            available_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            dedupe_key="job-pma-operator",
        )
    )
    store.update_running_job(
        "job-pma-operator",
        execution_refs={"pma_lane_id": "pma:default", "pma_queue_item_id": "item-1"},
    )
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-pma-operator",
            child_kind=AUTOMATION_CHILD_KIND_PMA_OPERATOR,
            child_id="item-1",
            requested_runtime=AutomationRuntimeContract(agent="codex"),
            actual_runtime=AutomationRuntimeContract(agent="codex"),
            authoritative_for_parent_completion=True,
        )
    )
    _insert_pma_queue_item(
        hub,
        item_id="item-1",
        lane_id="pma:default",
        state="completed",
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma-operator")
    edge = store.list_child_execution_edges("job-pma-operator")[0]
    assert result.completed == 1
    assert saved.state == JOB_SUCCEEDED
    assert saved.result_summary == "PMA operator completed: item-1"
    assert edge.terminal_state == "succeeded"


def test_reconciler_rejects_pma_queue_without_durable_child_edge(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = AutomationStore(hub)
    store.upsert_rule(
        AutomationRule.hydrate_persisted(
            rule_id="rule-legacy-pma",
            name="Legacy PMA",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_EXISTING_REPO,
            executor_kind=LEGACY_EXECUTOR_PMA_TURN,
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-legacy-pma", event_type="manual.run")
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-legacy-pma",
            rule_id="rule-legacy-pma",
            event_id="event-legacy-pma",
            state=JOB_RUNNING,
            target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "repo-1"},
            executor={"kind": LEGACY_EXECUTOR_PMA_TURN},
            available_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            dedupe_key="job-legacy-pma",
        )
    )
    store.update_running_job(
        "job-legacy-pma",
        execution_refs={
            "pma_lane_id": "pma:default",
            "pma_queue_item_id": "item-legacy",
        },
    )
    _insert_pma_queue_item(
        hub,
        item_id="item-legacy",
        lane_id="pma:default",
        state="completed",
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-legacy-pma")
    assert result.failed == 1
    assert saved.state == JOB_FAILED
    assert "explicit automation executor migration" in (saved.error_text or "")
    assert store.list_child_execution_edges("job-legacy-pma") == []


def _store_with_running_managed_thread_job(hub: Path) -> AutomationStore:
    store = AutomationStore(hub)
    store.upsert_rule(
        AutomationRule.hydrate_persisted(
            rule_id="rule-pma",
            name="managed thread turn",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_EXISTING_REPO,
            executor_kind=LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-pma", event_type="manual.run")
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-pma",
            rule_id="rule-pma",
            event_id="event-pma",
            state=JOB_RUNNING,
            target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "repo-1"},
            executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
            available_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            dedupe_key="job-pma",
        )
    )
    store.update_running_job(
        "job-pma",
        execution_refs={
            "managed_thread_target_id": "29998b57-94e9-49a4-8299-0349872e4b70",
            "managed_thread_execution_id": "exec-1",
        },
    )
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-pma",
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="exec-1",
            requested_runtime=AutomationRuntimeContract(
                agent="opencode",
                model="zai-coding-plan/glm-5.1",
            ),
            actual_runtime=None,
        )
    )
    return store


def test_reconciler_completes_managed_thread_job_when_turn_succeeds(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_managed_thread_job(hub)
    _insert_thread_execution(
        hub,
        thread_id="29998b57-94e9-49a4-8299-0349872e4b70",
        backend_thread_id="backend-session-1",
        execution_id="exec-1",
        status="completed",
        error_text=None,
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma")
    assert result.completed == 1
    assert saved.state == JOB_SUCCEEDED
    assert saved.result_summary == "Managed automation thread completed: exec-1"


def test_reconciler_completes_managed_thread_job_when_turn_status_is_ok(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_managed_thread_job(hub)
    _insert_thread_execution(
        hub,
        thread_id="29998b57-94e9-49a4-8299-0349872e4b70",
        backend_thread_id="backend-session-1",
        execution_id="exec-1",
        status="ok",
        error_text=None,
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma")
    assert result.completed == 1
    assert saved.state == JOB_SUCCEEDED
    assert saved.result_summary == "Managed automation thread completed: exec-1"


def test_reconciler_uses_exact_managed_thread_execution_id(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    thread_id = "29998b57-94e9-49a4-8299-0349872e4b70"
    store = _store_with_running_managed_thread_job(hub)
    _insert_thread_execution(
        hub,
        thread_id=thread_id,
        backend_thread_id="backend-session-1",
        execution_id="exec-1",
        status="ok",
        error_text=None,
    )
    _insert_thread_execution(
        hub,
        thread_id=thread_id,
        backend_thread_id="backend-session-1",
        execution_id="exec-2",
        status="error",
        error_text="newer unrelated turn failed",
        started_at="2026-01-01T00:05:00Z",
        finished_at="2026-01-01T00:06:00Z",
        insert_thread_target=False,
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma")
    assert result.completed == 1
    assert result.failed == 0
    assert saved.state == JOB_SUCCEEDED
    assert store.list_child_execution_edges("job-pma")[0].child_id == "exec-1"


def test_reconciler_fails_managed_thread_job_when_turn_fails(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_managed_thread_job(hub)
    _insert_thread_execution(
        hub,
        thread_id="29998b57-94e9-49a4-8299-0349872e4b70",
        backend_thread_id="backend-session-1",
        execution_id="exec-1",
        status="error",
        error_text="failed",
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma")
    assert result.failed == 1
    assert saved.state == JOB_FAILED
    assert saved.error_text == "failed"


def test_reconciler_fails_managed_thread_job_when_child_thread_fails(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_managed_thread_job(hub)
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
    edge = store.list_child_execution_edges("job-pma")[0]
    assert result.failed == 1
    assert saved.state == JOB_FAILED
    assert edge.child_id == "exec-1"
    assert edge.terminal_event_id == "exec-1"
    assert saved.error_text == (
        "opencode_first_event_timeout: no relevant events received within 60.0s"
    )


def test_reconciler_cancels_managed_thread_job_when_child_thread_is_interrupted(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_managed_thread_job(hub)
    _insert_thread_execution(
        hub,
        thread_id="29998b57-94e9-49a4-8299-0349872e4b70",
        backend_thread_id="backend-session-1",
        execution_id="exec-1",
        status="interrupted",
        error_text=None,
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma")
    edge = store.list_child_execution_edges("job-pma")[0]
    assert result.cancelled == 1
    assert saved.state == "cancelled"
    assert edge.child_id == "exec-1"
    assert edge.terminal_event_id == "exec-1"


def test_reconciler_uses_managed_thread_refs_without_transport_state(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_managed_thread_job(hub)
    _insert_thread_execution(
        hub,
        thread_id="29998b57-94e9-49a4-8299-0349872e4b70",
        backend_thread_id="backend-session-1",
        execution_id="exec-1",
        status="error",
        error_text="reattach failed",
    )

    result = AutomationChildRunReconciler(
        store, resolve_repo_path=lambda _repo_id: None, hub_root=hub
    ).reconcile_running_jobs()

    saved = store.get_job("job-pma")
    assert result.failed == 1
    assert saved.state == JOB_FAILED
    assert saved.error_text == "reattach failed"


def test_execution_snapshot_links_managed_thread_child(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_managed_thread_job(hub)
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

    assert snapshot["primary_child_kind"] == "managed_thread"
    assert snapshot["chat_href"] == "/chats/29998b57-94e9-49a4-8299-0349872e4b70"
    assert snapshot["managed_thread"]["latest_execution"]["status"] == "running"


def test_execution_snapshots_batch_links_managed_thread_child(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    store = _store_with_running_managed_thread_job(hub)
    _insert_thread_execution(
        hub,
        thread_id="29998b57-94e9-49a4-8299-0349872e4b70",
        backend_thread_id="backend-session-1",
        execution_id="exec-1",
        status="running",
        error_text=None,
    )

    jobs = store.list_jobs(rule_id="rule-pma", limit=25)
    snapshots = automation_execution_snapshots_by_job_id(jobs, hub_root=hub)
    snapshot = snapshots["job-pma"].to_dict()

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
    started_at: str = "2026-01-01T00:00:00Z",
    finished_at: str | None = None,
    insert_thread_target: bool = True,
    transcript_model_id: str | None = "zai-coding-plan/glm-5.1",
) -> None:
    transcript_mirror_id = f"transcript-{execution_id}" if transcript_model_id else None
    with open_orchestration_sqlite(hub, durable=True) as conn:
        with conn:
            if insert_thread_target:
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
                        started_at,
                        finished_at or "2026-01-01T00:01:00Z",
                        finished_at or "2026-01-01T00:01:00Z",
                        1 if status in {"error", "completed", "ok"} else 0,
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
                    transcript_mirror_id,
                    started_at,
                    (
                        finished_at
                        or (
                            "2026-01-01T00:01:00Z"
                            if status in {"error", "completed", "ok"}
                            else None
                        )
                    ),
                    started_at,
                    "{}",
                ),
            )
            if transcript_mirror_id is not None:
                conn.execute(
                    """
                    INSERT INTO orch_transcript_mirrors (
                        transcript_mirror_id, target_kind, target_id, execution_id,
                        message_role, text_content, text_preview, repo_id, agent_id,
                        model_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transcript_mirror_id,
                        "thread",
                        thread_id,
                        execution_id,
                        "assistant",
                        "result",
                        "result",
                        "repo-1",
                        "opencode",
                        transcript_model_id,
                        started_at,
                        finished_at or "2026-01-01T00:01:00Z",
                    ),
                )


def _insert_pma_queue_item(
    hub: Path,
    *,
    item_id: str,
    lane_id: str,
    state: str,
) -> None:
    with open_orchestration_sqlite(hub, durable=True) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO orch_queue_items (
                    queue_item_id, lane_id, source_kind, source_key, dedupe_key,
                    state, visible_at, claimed_at, completed_at, payload_json,
                    result_json, error_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    lane_id,
                    "automation_test",
                    item_id,
                    item_id,
                    state,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:01Z",
                    "2026-01-01T00:00:02Z",
                    "{}",
                    "{}",
                    None,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:02Z",
                ),
            )
