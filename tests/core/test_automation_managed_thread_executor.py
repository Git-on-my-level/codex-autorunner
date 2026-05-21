from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.automation import (
    AgentTaskTurnAutomationExecutor,
    AutomationEvent,
    AutomationExecutorRegistry,
    AutomationJob,
    AutomationJobWorker,
    AutomationRule,
    AutomationStore,
    ManagedThreadTurnAutomationExecutor,
    PmaOperatorTurnAutomationExecutor,
)
from codex_autorunner.core.automation.models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    AUTOMATION_CHILD_KIND_PMA_OPERATOR,
    EXECUTOR_AGENT_TASK_TURN,
    EXECUTOR_MANAGED_THREAD_TURN,
    EXECUTOR_PMA_OPERATOR_TURN,
    JOB_DEAD_LETTERED,
    JOB_FAILED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
)
from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState


def _store_rule_event(
    tmp_path: Path,
) -> tuple[AutomationStore, ManagedThreadStore, str]:
    hub_root = tmp_path / "hub"
    store = AutomationStore(hub_root)
    threads = ManagedThreadStore(hub_root)
    thread = threads.create_thread("codex", tmp_path)
    thread_id = str(thread["managed_thread_id"])
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-1",
            name="Managed turn",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_HUB,
            executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
        )
    )
    store.record_event(
        AutomationEvent.create(
            event_id="event-1",
            event_type="manual.run",
            payload={"name": "Ada"},
        )
    )
    return store, threads, thread_id


def _job(thread_id: str, **kwargs) -> AutomationJob:
    args = {
        "job_id": "job-1",
        "rule_id": "rule-1",
        "event_id": "event-1",
        "target": {"thread_target_id": thread_id},
        "executor": {
            "kind": EXECUTOR_MANAGED_THREAD_TURN,
            "prompt": "Say hi to {{ event.payload.name }}",
            "client_turn_id": "client-1",
            "model": "gpt-test",
            "reasoning": "low",
        },
        "available_at": "2026-01-01T00:00:00Z",
    }
    args.update(kwargs)
    return AutomationJob.create(**args)


def test_managed_thread_turn_creates_turn_and_refs(tmp_path: Path) -> None:
    store, threads, thread_id = _store_rule_event(tmp_path)
    started_workers: list[str] = []
    store.enqueue_job(_job(thread_id, policy={"approval_mode": "inherit_profile"}))
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
            queue_worker_starter_fn=started_workers.append,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    assert result.running == 1
    assert result.succeeded == 0
    assert saved.state == JOB_RUNNING
    assert saved.managed_thread_target_id == thread_id
    turn = threads.get_turn(thread_id, str(saved.managed_thread_execution_id))
    assert turn["prompt"] == "Say hi to Ada"
    assert turn["client_turn_id"] == "client-1"
    assert turn["status"] == "queued"
    assert [
        entry["managed_turn_id"] for entry in threads.list_queued_turns(thread_id)
    ] == [str(saved.managed_thread_execution_id)]
    assert started_workers == [thread_id]


def test_managed_thread_turn_creates_automation_thread_when_target_is_repo(
    tmp_path: Path,
) -> None:
    store, threads, _thread_id = _store_rule_event(tmp_path)
    started_workers: list[str] = []
    store.enqueue_job(
        _job(
            "",
            target={"repo_id": "repo-1"},
            executor={
                "kind": EXECUTOR_MANAGED_THREAD_TURN,
                "message_text": "Inspect {{ target.repo_id }}",
                "agent": "codex",
                "profile": "automation",
                "client_turn_id": "client-1",
            },
            policy={"approval_mode": "inherit_profile"},
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
            queue_worker_starter_fn=started_workers.append,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    assert result.running == 1
    assert saved.managed_thread_target_id
    thread = threads.get_thread(str(saved.managed_thread_target_id))
    assert thread["repo_id"] == "repo-1"
    assert thread["metadata"]["automation_job_id"] == "job-1"
    turn = threads.get_turn(
        str(saved.managed_thread_target_id), str(saved.managed_thread_execution_id)
    )
    assert turn["prompt"] == "Inspect repo-1"
    assert started_workers == [str(saved.managed_thread_target_id)]


def test_managed_thread_turn_dead_letters_without_queue_worker_starter(
    tmp_path: Path,
) -> None:
    store, threads, thread_id = _store_rule_event(tmp_path)
    store.enqueue_job(_job(thread_id, policy={"approval_mode": "inherit_profile"}))
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    assert result.dead_lettered == 1
    assert store.get_job("job-1").state == JOB_DEAD_LETTERED
    assert threads.list_turns(thread_id) == []


def test_managed_thread_turn_dead_letters_when_queue_worker_unavailable(
    tmp_path: Path,
) -> None:
    store, threads, thread_id = _store_rule_event(tmp_path)
    started_workers: list[str] = []
    store.enqueue_job(_job(thread_id, policy={"approval_mode": "inherit_profile"}))
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
            queue_worker_starter_fn=started_workers.append,
            queue_worker_available_fn=lambda: False,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    assert result.dead_lettered == 1
    assert store.get_job("job-1").state == JOB_DEAD_LETTERED
    assert threads.list_turns(thread_id) == []
    assert started_workers == []


def test_managed_thread_turn_materializes_opencode_model_in_canonical_record(
    tmp_path: Path,
) -> None:
    store, threads, thread_id = _store_rule_event(tmp_path)
    started_workers: list[str] = []
    store.enqueue_job(
        _job(
            thread_id,
            executor={
                "kind": EXECUTOR_MANAGED_THREAD_TURN,
                "prompt": "Say hi to {{ event.payload.name }}",
                "client_turn_id": "client-opencode-1",
                "agent": "opencode",
                "model": "zai-coding-plan/glm-5.1",
                "reasoning": "medium",
            },
            policy={"approval_mode": "never_require_approval"},
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
            queue_worker_starter_fn=started_workers.append,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    assert result.running == 1
    assert started_workers == [thread_id]
    assert saved.managed_thread_execution_id is not None
    request = threads.get_turn_execution_request(
        thread_id,
        str(saved.managed_thread_execution_id),
    )
    record = threads.get_turn_execution_record(
        thread_id,
        str(saved.managed_thread_execution_id),
    )
    assert request is not None
    assert record is not None
    assert request.agent == "opencode"
    assert request.model == "zai-coding-plan/glm-5.1"
    assert request.model_payload == {
        "providerID": "zai-coding-plan",
        "modelID": "glm-5.1",
    }
    assert record.request.model == "zai-coding-plan/glm-5.1"
    assert record.request.reasoning == "medium"


def test_managed_thread_default_approval_pauses_unattended_job(tmp_path: Path) -> None:
    store, threads, thread_id = _store_rule_event(tmp_path)
    store.enqueue_job(_job(thread_id))
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    assert result.paused == 1


def test_agent_task_turn_launches_direct_codex_task_and_records_runtime_edge(
    tmp_path: Path,
) -> None:
    store, threads, _thread_id = _store_rule_event(tmp_path)
    started_workers: list[str] = []
    store.enqueue_job(
        _job(
            "",
            target={"repo_id": "repo-1"},
            executor={
                "kind": EXECUTOR_AGENT_TASK_TURN,
                "message_text": "Inspect {{ target.repo_id }}",
                "client_turn_id": "client-direct-codex",
                "requested_runtime": {
                    "agent": "codex",
                    "model": "gpt-5.5",
                    "profile": "security",
                    "reasoning": "medium",
                    "approval_policy": "never_require_approval",
                    "sandbox_policy": "dangerFullAccess",
                },
            },
            policy={"approval_mode": "never_require_approval"},
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_AGENT_TASK_TURN,
        AgentTaskTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
            queue_worker_starter_fn=started_workers.append,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    assert result.running == 1
    assert saved.state == JOB_RUNNING
    assert saved.pma_queue_item_id is None
    assert saved.pma_lane_id is None
    assert saved.managed_thread_target_id
    assert saved.managed_thread_execution_id
    assert started_workers == [str(saved.managed_thread_target_id)]
    request = threads.get_turn_execution_request(
        str(saved.managed_thread_target_id),
        str(saved.managed_thread_execution_id),
    )
    assert request.agent == "codex"
    assert request.model == "gpt-5.5"
    assert request.profile == "security"
    edge = store.list_child_execution_edges("job-1")[0]
    assert edge.child_kind == AUTOMATION_CHILD_KIND_AGENT_TASK
    assert edge.child_id == saved.managed_thread_execution_id
    assert edge.requested_runtime.agent == "codex"
    assert edge.requested_runtime.model == "gpt-5.5"
    assert edge.actual_runtime is not None
    assert edge.actual_runtime.agent == "codex"
    assert edge.actual_runtime.model == "gpt-5.5"


def test_agent_task_turn_launches_direct_opencode_task_with_model_payload(
    tmp_path: Path,
) -> None:
    store, threads, _thread_id = _store_rule_event(tmp_path)
    started_workers: list[str] = []
    store.enqueue_job(
        _job(
            "",
            target={"repo_id": "repo-1"},
            executor={
                "kind": EXECUTOR_AGENT_TASK_TURN,
                "message_text": "Scan {{ target.repo_id }}",
                "client_turn_id": "client-direct-opencode",
                "requested_runtime": {
                    "agent": "opencode",
                    "model": "zai-coding-plan/glm-5.1",
                    "profile": "security",
                    "reasoning": "high",
                    "approval_policy": "never_require_approval",
                    "sandbox_policy": "dangerFullAccess",
                },
            },
            policy={"approval_mode": "never_require_approval"},
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_AGENT_TASK_TURN,
        AgentTaskTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
            queue_worker_starter_fn=started_workers.append,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    assert result.running == 1
    request = threads.get_turn_execution_request(
        str(saved.managed_thread_target_id),
        str(saved.managed_thread_execution_id),
    )
    assert request.agent == "opencode"
    assert request.model == "zai-coding-plan/glm-5.1"
    assert request.model_payload == {
        "providerID": "zai-coding-plan",
        "modelID": "glm-5.1",
    }
    edge = store.list_child_execution_edges("job-1")[0]
    assert edge.requested_runtime.provider_payload == {
        "providerID": "zai-coding-plan",
        "modelID": "glm-5.1",
    }
    assert edge.actual_runtime.provider_payload == {
        "providerID": "zai-coding-plan",
        "modelID": "glm-5.1",
    }
    assert started_workers == [str(saved.managed_thread_target_id)]


def test_agent_task_turn_runtime_mismatch_fails_without_fallback(
    tmp_path: Path,
) -> None:
    store, threads, thread_id = _store_rule_event(tmp_path)
    store.enqueue_job(
        _job(
            thread_id,
            executor={
                "kind": EXECUTOR_AGENT_TASK_TURN,
                "message_text": "Scan",
                "client_turn_id": "client-mismatch",
                "requested_runtime": {
                    "agent": "opencode",
                    "model": "zai-coding-plan/glm-5.1",
                },
            },
            policy={"approval_mode": "never_require_approval", "max_attempts": 1},
            max_attempts=1,
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_AGENT_TASK_TURN,
        AgentTaskTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
            queue_worker_starter_fn=lambda _thread_id: None,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    assert result.dead_lettered == 1
    assert saved.state == JOB_DEAD_LETTERED
    assert "requested agent 'opencode' does not match thread agent 'codex'" in (
        saved.error_text or ""
    )
    assert threads.list_turns(thread_id) == []
    assert store.list_child_execution_edges("job-1") == []


def test_pma_operator_turn_queues_coordinator_and_records_runtime_edge(
    tmp_path: Path,
) -> None:
    store, threads, _thread_id = _store_rule_event(tmp_path)
    started_lanes: list[str] = []
    store.enqueue_job(
        _job(
            "",
            target={"repo_id": "repo-1"},
            executor={
                "kind": EXECUTOR_PMA_OPERATOR_TURN,
                "message_text": "Decide what to do with {{ target.repo_id }}",
                "client_turn_id": "client-pma-operator",
                "lane_id": "pma:default",
                "requested_runtime": {
                    "agent": "codex",
                    "model": "gpt-5.5",
                    "profile": "operator",
                    "reasoning": "medium",
                    "approval_policy": "never_require_approval",
                    "sandbox_policy": "dangerFullAccess",
                },
            },
            policy={"approval_mode": "never_require_approval"},
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_PMA_OPERATOR_TURN,
        PmaOperatorTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            pma_queue=PmaQueue(tmp_path / "hub"),
            lane_worker_starter_fn=started_lanes.append,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    assert result.running == 1
    assert saved.state == JOB_RUNNING
    assert saved.pma_lane_id == "pma:default"
    assert saved.pma_queue_item_id
    assert saved.managed_thread_target_id is None
    assert saved.managed_thread_execution_id is None
    assert started_lanes == ["pma:default"]
    item = PmaQueue(tmp_path / "hub").get_item_sync(str(saved.pma_queue_item_id))
    assert item is not None
    assert item.state == QueueItemState.PENDING
    turn_request = item.payload["turn_request"]
    assert turn_request["agent"] == "codex"
    assert turn_request["model"] == "gpt-5.5"
    edges = sorted(
        store.list_child_execution_edges("job-1"),
        key=lambda edge: (
            0 if edge.child_kind == AUTOMATION_CHILD_KIND_PMA_OPERATOR else 1
        ),
    )
    assert len(edges) == 1
    assert edges[0].child_kind == AUTOMATION_CHILD_KIND_PMA_OPERATOR
    assert edges[0].child_id == saved.pma_queue_item_id
    assert edges[0].authoritative_for_parent_completion is True
    assert edges[0].requested_runtime.agent == "codex"
    assert edges[0].actual_runtime.agent == "codex"


def test_pma_operator_turn_records_separate_declared_worker_child_edge(
    tmp_path: Path,
) -> None:
    store, _threads, _thread_id = _store_rule_event(tmp_path)
    store.enqueue_job(
        _job(
            "",
            executor={
                "kind": EXECUTOR_PMA_OPERATOR_TURN,
                "message_text": "Coordinate work",
                "client_turn_id": "client-pma-worker",
                "requested_runtime": {"agent": "codex", "model": "gpt-5.5"},
                "coordinator_authoritative": False,
                "worker_child": {
                    "child_id": "worker-turn-1",
                    "authoritative_for_parent_completion": True,
                    "requested_runtime": {
                        "agent": "opencode",
                        "model": "zai-coding-plan/glm-5.1",
                    },
                    "actual_runtime": {
                        "agent": "opencode",
                        "model": "zai-coding-plan/glm-5.1",
                    },
                },
            },
            policy={"approval_mode": "never_require_approval"},
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_PMA_OPERATOR_TURN,
        PmaOperatorTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            pma_queue=PmaQueue(tmp_path / "hub"),
            lane_worker_starter_fn=lambda _lane: None,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    assert result.running == 1
    edges = sorted(
        store.list_child_execution_edges("job-1"),
        key=lambda edge: (
            0 if edge.child_kind == AUTOMATION_CHILD_KIND_PMA_OPERATOR else 1
        ),
    )
    assert [edge.child_kind for edge in edges] == [
        AUTOMATION_CHILD_KIND_PMA_OPERATOR,
        AUTOMATION_CHILD_KIND_AGENT_TASK,
    ]
    assert edges[0].authoritative_for_parent_completion is False
    assert edges[0].requested_runtime.agent == "codex"
    assert edges[1].child_id == "worker-turn-1"
    assert edges[1].authoritative_for_parent_completion is True
    assert edges[1].requested_runtime.agent == "opencode"
    assert edges[1].actual_runtime.agent == "opencode"


def test_pma_operator_turn_coordinator_success_worker_failure_policy_is_visible(
    tmp_path: Path,
) -> None:
    store, _threads, _thread_id = _store_rule_event(tmp_path)
    job = _job(
        "",
        executor={
            "kind": EXECUTOR_PMA_OPERATOR_TURN,
            "message_text": "Coordinate work",
            "client_turn_id": "client-policy",
            "requested_runtime": {"agent": "codex", "model": "gpt-5.5"},
            "coordinator_authoritative": False,
            "worker_child": {
                "child_id": "worker-turn-1",
                "authoritative_for_parent_completion": True,
                "requested_runtime": {"agent": "opencode", "model": "glm-5.1"},
                "actual_runtime": {"agent": "opencode", "model": "glm-5.1"},
            },
        },
        policy={"approval_mode": "never_require_approval"},
    )
    store.enqueue_job(job)
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_PMA_OPERATOR_TURN,
        PmaOperatorTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            pma_queue=PmaQueue(tmp_path / "hub"),
            lane_worker_starter_fn=lambda _lane: None,
        ),
    )
    AutomationJobWorker(store, registry).process_once(now="2026-01-01T00:00:00Z")

    edges = store.list_child_execution_edges("job-1")
    authoritative = [edge for edge in edges if edge.authoritative_for_parent_completion]
    non_authoritative = [
        edge for edge in edges if not edge.authoritative_for_parent_completion
    ]
    assert len(authoritative) == 1
    assert authoritative[0].child_kind == AUTOMATION_CHILD_KIND_AGENT_TASK
    assert authoritative[0].terminal_mapping["failed"] == JOB_FAILED
    assert non_authoritative[0].child_kind == AUTOMATION_CHILD_KIND_PMA_OPERATOR
    assert non_authoritative[0].terminal_mapping["succeeded"] == JOB_SUCCEEDED


def test_managed_thread_auto_decline_dead_letters_and_escalates(tmp_path: Path) -> None:
    store, threads, thread_id = _store_rule_event(tmp_path)
    store.enqueue_job(
        _job(
            thread_id,
            policy={
                "max_attempts": 1,
                "approval_mode": "auto_decline",
                "on_failure": {
                    "executor": {
                        "kind": EXECUTOR_MANAGED_THREAD_TURN,
                        "message_text": "Escalate failed automation",
                    },
                    "target": {"thread_target_id": thread_id},
                },
            },
            max_attempts=1,
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    assert result.dead_lettered == 1
    assert result.escalated == 1
    assert store.get_job("job-1").state == JOB_DEAD_LETTERED
    escalations = [job for job in store.list_jobs() if job.job_id != "job-1"]
    assert len(escalations) == 1
    assert escalations[0].executor["kind"] == EXECUTOR_MANAGED_THREAD_TURN
