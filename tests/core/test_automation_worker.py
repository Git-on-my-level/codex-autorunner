from __future__ import annotations

from codex_autorunner.core.automation import (
    AutomationEvent,
    AutomationExecutorRegistry,
    AutomationExecutorResult,
    AutomationJob,
    AutomationJobWorker,
    AutomationRule,
    AutomationStore,
)
from codex_autorunner.core.automation.models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    JOB_CLAIMED,
    JOB_DEAD_LETTERED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    AutomationChildExecutionEdge,
    AutomationRuntimeContract,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite


class _SuccessExecutor:
    def execute(self, job):
        return AutomationExecutorResult(
            summary=f"ok:{job.job_id}",
            data={"ok": True},
            execution_refs={"pma_lane_id": "pma:default"},
        )


class _RunningExecutor:
    def execute(self, job):
        return AutomationExecutorResult(
            status=JOB_RUNNING,
            summary=f"started:{job.job_id}",
            data={"execution_phase": "running"},
            execution_refs={"pma_lane_id": "pma:default"},
        )


class _FailOnceExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, job):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        return AutomationExecutorResult(summary="recovered")


def _store_with_rule_and_event(tmp_path) -> AutomationStore:
    store = AutomationStore(tmp_path)
    store.upsert_rule(
        AutomationRule.hydrate_persisted(
            rule_id="rule-1",
            name="Worker rule",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_HUB,
            executor_kind=LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-1", event_type="manual.run")
    )
    return store


def _job(job_id: str, **kwargs) -> AutomationJob:
    args = {
        "job_id": job_id,
        "rule_id": "rule-1",
        "event_id": "event-1",
        "target": {"repo_id": "repo-1"},
        "executor": {"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
        "available_at": "2026-01-01T00:00:00Z",
    }
    args.update(kwargs)
    return AutomationJob.create(**args)


def test_worker_claims_records_attempt_and_completes_job(tmp_path) -> None:
    store = _store_with_rule_and_event(tmp_path)
    store.enqueue_job(_job("job-1"))
    registry = AutomationExecutorRegistry()
    registry.register(LEGACY_EXECUTOR_MANAGED_THREAD_TURN, _SuccessExecutor())

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    attempt = store.list_attempts("job-1")[0]
    assert result.succeeded == 1
    assert saved.state == JOB_SUCCEEDED
    assert attempt.execution_refs == {"pma_lane_id": "pma:default"}
    assert attempt.status == JOB_SUCCEEDED


def test_worker_persists_running_executor_result_without_success(tmp_path) -> None:
    store = _store_with_rule_and_event(tmp_path)
    store.enqueue_job(_job("job-1"))
    registry = AutomationExecutorRegistry()
    registry.register(LEGACY_EXECUTOR_MANAGED_THREAD_TURN, _RunningExecutor())

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    attempt = store.list_attempts("job-1")[0]
    assert result.running == 1
    assert result.succeeded == 0
    assert saved.state == JOB_RUNNING
    assert saved.lock_key is None
    assert saved.claimed_at is None
    assert saved.finished_at is None
    assert attempt.status == JOB_RUNNING
    assert attempt.execution_refs == {"pma_lane_id": "pma:default"}
    assert attempt.executor_result["execution_phase"] == "running"


def test_worker_retries_then_succeeds_and_records_attempts(tmp_path) -> None:
    store = _store_with_rule_and_event(tmp_path)
    store.enqueue_job(
        _job(
            "job-1",
            policy={"max_attempts": 2, "retry_backoff_seconds": 60},
            max_attempts=2,
        )
    )
    executor = _FailOnceExecutor()
    registry = AutomationExecutorRegistry()
    registry.register(LEGACY_EXECUTOR_MANAGED_THREAD_TURN, executor)
    worker = AutomationJobWorker(store, registry)

    first = worker.process_once(now="2026-01-01T00:00:00Z")
    saved = store.get_job("job-1")
    assert first.retried == 1
    assert saved.state == JOB_PENDING
    assert saved.available_at == "2026-01-01T00:01:00Z"
    assert store.list_attempts("job-1")[0].status == JOB_FAILED

    second = worker.process_once(now="2026-01-01T00:01:00Z")
    assert second.succeeded == 1
    assert store.get_job("job-1").state == JOB_SUCCEEDED
    assert len(store.list_attempts("job-1")) == 2


def test_worker_dead_letters_after_max_attempts(tmp_path) -> None:
    store = _store_with_rule_and_event(tmp_path)
    store.enqueue_job(_job("job-1", policy={"max_attempts": 1}, max_attempts=1))
    registry = AutomationExecutorRegistry()
    registry.register(
        LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
        type(
            "AlwaysFail",
            (),
            {"execute": lambda self, job: (_ for _ in ()).throw(RuntimeError("boom"))},
        )(),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    assert result.dead_lettered == 1
    assert store.get_job("job-1").state == JOB_DEAD_LETTERED
    assert store.list_attempts("job-1")[0].status == JOB_DEAD_LETTERED


def test_worker_dead_letters_unknown_executor_without_retrying(tmp_path) -> None:
    store = _store_with_rule_and_event(tmp_path)
    store.enqueue_job(
        _job(
            "job-unknown",
            executor={"kind": "agent_task_turn"},
            policy={"max_attempts": 3},
            max_attempts=3,
        )
    )
    registry = AutomationExecutorRegistry()

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-unknown")
    attempt = store.list_attempts("job-unknown")[0]
    assert result.failed == 1
    assert result.retried == 0
    assert result.dead_lettered == 1
    assert saved.state == JOB_DEAD_LETTERED
    assert "AUTOMATION_EXECUTOR_KIND_UNSUPPORTED" in saved.error_text
    assert attempt.status == JOB_DEAD_LETTERED
    assert attempt.executor_result == {
        "code": "AUTOMATION_EXECUTOR_KIND_UNSUPPORTED",
        "executor_kind": "agent_task_turn",
        "executable": False,
    }


def test_worker_recovers_stale_claim_and_respects_running_concurrency(tmp_path) -> None:
    store = _store_with_rule_and_event(tmp_path)
    running, _ = store.enqueue_job(_job("running-job", dedupe_key="running"))
    store.claim_next_job(lock_key="other", now="2026-01-01T00:09:30Z")
    store.start_job(running.job_id, now="2026-01-01T00:09:30Z")
    store.update_running_job(running.job_id, now="2026-01-01T00:09:31Z")
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id=running.job_id,
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="exec-open",
            requested_runtime=AutomationRuntimeContract(),
            actual_runtime=None,
        )
    )
    stale, _ = store.enqueue_job(_job("stale-job"))
    with open_orchestration_sqlite(tmp_path) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_automation_jobs
                   SET state = ?,
                       lock_key = ?,
                       claimed_at = ?,
                       updated_at = ?
                 WHERE job_id = ?
                """,
                (
                    JOB_CLAIMED,
                    "old",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    stale.job_id,
                ),
            )
    assert store.get_job(stale.job_id).state == JOB_CLAIMED

    registry = AutomationExecutorRegistry()
    registry.register(LEGACY_EXECUTOR_MANAGED_THREAD_TURN, _SuccessExecutor())
    result = AutomationJobWorker(store, registry, claim_lease_seconds=60).process_once(
        now="2026-01-01T00:10:00Z"
    )

    assert result.claimed == 1
    assert result.skipped == 1
    stale_saved = store.get_job("stale-job")
    assert stale_saved.state == JOB_PENDING
    assert stale_saved.blocked_by_job_id == "running-job"


def test_worker_reconciles_terminal_graph_blocker_before_concurrency(
    tmp_path,
) -> None:
    store = _store_with_rule_and_event(tmp_path)
    policy = {"max_concurrent_per_rule": 1, "max_concurrent_per_target": 1}
    running, _ = store.enqueue_job(
        _job("running-job", dedupe_key="running", policy=policy)
    )
    store.claim_next_job(lock_key="other", now="2026-01-01T00:00:00Z")
    store.start_job(running.job_id, now="2026-01-01T00:00:00Z")
    store.update_running_job(running.job_id, now="2026-01-01T00:00:01Z")
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id=running.job_id,
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="exec-terminal",
            requested_runtime=AutomationRuntimeContract(),
            actual_runtime=AutomationRuntimeContract(),
            terminal_state="succeeded",
            terminal_event_id="exec-terminal",
            terminal_observed_at="2026-01-01T00:00:02Z",
        )
    )
    store.enqueue_job(_job("pending-job", dedupe_key="pending", policy=policy))
    registry = AutomationExecutorRegistry()
    registry.register(LEGACY_EXECUTOR_MANAGED_THREAD_TURN, _SuccessExecutor())

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:03Z"
    )

    assert result.succeeded == 1
    assert result.skipped == 0
    assert store.get_job("running-job").state == JOB_SUCCEEDED
    pending = store.get_job("pending-job")
    assert pending.state == JOB_SUCCEEDED
    assert pending.blocked_by_job_id is None
    assert pending.blocked_reason is None


def test_worker_persists_real_graph_blocker_wait_reason(tmp_path) -> None:
    store = _store_with_rule_and_event(tmp_path)
    policy = {"max_concurrent_per_rule": 1, "max_concurrent_per_target": 1}
    running, _ = store.enqueue_job(
        _job("running-job", dedupe_key="running", policy=policy)
    )
    store.claim_next_job(lock_key="other", now="2026-01-01T00:00:00Z")
    store.start_job(running.job_id, now="2026-01-01T00:00:00Z")
    store.update_running_job(running.job_id, now="2026-01-01T00:00:01Z")
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id=running.job_id,
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="exec-open",
            requested_runtime=AutomationRuntimeContract(),
            actual_runtime=None,
        )
    )
    store.enqueue_job(_job("pending-job", dedupe_key="pending", policy=policy))
    registry = AutomationExecutorRegistry()
    registry.register(LEGACY_EXECUTOR_MANAGED_THREAD_TURN, _SuccessExecutor())

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:02Z"
    )

    pending = store.get_job("pending-job")
    assert result.skipped == 1
    assert pending.state == JOB_PENDING
    assert pending.blocked_by_job_id == "running-job"
    assert pending.blocked_reason == "max_concurrent_per_rule:rule-1"
    assert pending.blocked_at == "2026-01-01T00:00:02Z"
    assert pending.available_at == "2026-01-01T00:00:07Z"


def test_worker_unblocks_when_blocker_child_edge_becomes_terminal(tmp_path) -> None:
    store = _store_with_rule_and_event(tmp_path)
    policy = {"max_concurrent_per_rule": 1, "max_concurrent_per_target": 1}
    running, _ = store.enqueue_job(
        _job("running-job", dedupe_key="running", policy=policy)
    )
    store.claim_next_job(lock_key="other", now="2026-01-01T00:00:00Z")
    store.start_job(running.job_id, now="2026-01-01T00:00:00Z")
    store.update_running_job(running.job_id, now="2026-01-01T00:00:01Z")
    edge = store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id=running.job_id,
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="exec-open",
            requested_runtime=AutomationRuntimeContract(),
            actual_runtime=None,
        )
    )
    store.enqueue_job(_job("pending-job", dedupe_key="pending", policy=policy))
    registry = AutomationExecutorRegistry()
    registry.register(LEGACY_EXECUTOR_MANAGED_THREAD_TURN, _SuccessExecutor())
    worker = AutomationJobWorker(store, registry)

    first = worker.process_once(now="2026-01-01T00:00:02Z")
    store.mark_child_execution_terminal(
        edge.edge_id,
        terminal_state="succeeded",
        terminal_event_id="exec-open",
        actual_runtime=AutomationRuntimeContract().to_dict(),
        now="2026-01-01T00:00:03Z",
    )
    second = worker.process_once(now="2026-01-01T00:00:07Z")

    pending = store.get_job("pending-job")
    assert first.skipped == 1
    assert second.succeeded == 1
    assert store.get_job("running-job").state == JOB_SUCCEEDED
    assert pending.state == JOB_SUCCEEDED
    assert pending.blocked_by_job_id is None
    assert pending.blocked_reason is None
    assert pending.blocked_at is None
