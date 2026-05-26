from __future__ import annotations

import json

import pytest

from codex_autorunner.core.automation import (
    AutomationEvent,
    AutomationJob,
    AutomationJobAttempt,
    AutomationRule,
    AutomationSchedule,
    AutomationStore,
)
from codex_autorunner.core.automation.models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    EXECUTOR_PMA_OPERATOR_TURN,
    JOB_CLAIMED,
    JOB_DEAD_LETTERED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
    SCHEDULE_DAILY,
    TARGET_POLICY_AUTO_WORKTREE,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_SCHEDULE,
    AutomationChildExecutionEdge,
    AutomationRuntimeContract,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite


def _rule() -> AutomationRule:
    return AutomationRule.hydrate_persisted(
        rule_id="rule-1",
        name="PR review follow-up",
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"event_types": ["scm.github.pull_request_review.submitted"]},
        filters={"repo_id": "repo-1"},
        target_policy=TARGET_POLICY_AUTO_WORKTREE,
        target={"repo_id": "repo-1"},
        executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
        executor={"prompt_template": "Handle {{ pr.number }}"},
        policy={"max_attempts": 2, "approval_mode": "pause_and_request_user"},
        metadata={"label": "review"},
    )


def _event() -> AutomationEvent:
    return AutomationEvent.create(
        event_id="event-1",
        event_type="scm.github.pull_request_review.submitted",
        source="github",
        repo_id="repo-1",
        target={"repo_id": "repo-1"},
        payload={"pr": {"number": 42}},
        raw_payload={"action": "submitted"},
    )


def test_migration_creates_unified_automation_tables(tmp_path) -> None:
    with open_orchestration_sqlite(tmp_path) as conn:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert "orch_automation_rules" in names
    assert "orch_automation_events" in names
    assert "orch_automation_jobs" in names
    assert "orch_automation_job_attempts" in names
    assert "orch_automation_schedules" in names
    assert "orch_automation_rule_versions" in names


def test_rule_crud_event_recording_and_json_roundtrip(tmp_path) -> None:
    store = AutomationStore(tmp_path)

    saved_rule = store.upsert_rule(_rule())
    assert saved_rule.rule_id == "rule-1"
    assert saved_rule.target["repo_id"] == "repo-1"
    assert saved_rule.policy["max_attempts"] == 2

    disabled = store.set_rule_enabled("rule-1", False)
    assert disabled is not None
    assert disabled.enabled is False
    assert store.list_rules(enabled=True) == []

    event = store.record_event(_event())
    assert event.payload["pr"]["number"] == 42
    assert store.get_event("event-1") == event
    assert (
        store.list_events(event_type=event.event_type)[0].raw_payload["action"]
        == "submitted"
    )


def test_persisted_rule_with_unknown_executor_kind_hydrates_without_rewriting(
    tmp_path,
) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule())
    with open_orchestration_sqlite(tmp_path) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_automation_rules
                   SET executor_kind = ?,
                       executor_json = ?
                 WHERE rule_id = ?
                """,
                (
                    "future_executor_turn",
                    json.dumps(
                        {"kind": "future_executor_turn", "prompt": "Run future task"}
                    ),
                    "rule-1",
                ),
            )

    loaded = store.get_rule("rule-1")
    assert loaded is not None
    assert loaded.executor_kind == "future_executor_turn"
    assert loaded.executor["kind"] == "future_executor_turn"
    assert loaded.known_executor is False
    assert loaded.executable is False
    assert store.list_rules()[0].executor_kind == "future_executor_turn"


def test_record_event_preserves_first_seen_payload_for_duplicate_id(tmp_path) -> None:
    store = AutomationStore(tmp_path)

    first = store.record_event(_event())
    duplicate = store.record_event(
        AutomationEvent.create(
            event_id="event-1",
            event_type="manual.run",
            source="manual",
            repo_id="repo-other",
            target={"repo_id": "repo-other"},
            payload={"changed": True},
            raw_payload={"changed": True},
            metadata={"attempt": 2},
        )
    )

    assert duplicate == first
    saved = store.get_event("event-1")
    assert saved == first
    assert saved.payload == {"pr": {"number": 42}}
    assert saved.raw_payload == {"action": "submitted"}
    assert saved.metadata == {}


def test_rule_version_ids_do_not_collide_for_same_updated_at(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    rule = _rule()
    rule.updated_at = "2026-01-01T00:00:00Z"
    store.upsert_rule(rule)

    first_update = _rule()
    first_update.name = "First update"
    first_update.updated_at = "2026-01-01T00:00:00Z"
    store.upsert_rule(first_update)

    second_update = _rule()
    second_update.name = "Second update"
    second_update.updated_at = "2026-01-01T00:00:00Z"
    store.upsert_rule(second_update)

    with open_orchestration_sqlite(tmp_path) as conn:
        rows = conn.execute(
            """
            SELECT version_id
              FROM orch_automation_rule_versions
             WHERE rule_id = ?
             ORDER BY created_at ASC
            """,
            ("rule-1",),
        ).fetchall()

    assert len(rows) == 2
    assert len({row["version_id"] for row in rows}) == 2


def test_job_enqueue_dedupe_claim_complete_fail_and_attempts(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule())
    store.record_event(_event())

    job = AutomationJob.create(
        job_id="job-1",
        rule_id="rule-1",
        event_id="event-1",
        dedupe_key="dedupe-1",
        available_at="2026-01-01T00:00:00Z",
        target={"repo_id": "repo-1"},
        executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
        payload={"work": "review"},
    )
    saved, deduped = store.enqueue_job(job)
    assert deduped is False
    assert saved.state == JOB_PENDING

    duplicate, deduped = store.enqueue_job(
        AutomationJob.create(
            job_id="job-duplicate",
            rule_id="rule-1",
            event_id="event-1",
            dedupe_key="dedupe-1",
            available_at="2026-01-01T00:00:00Z",
            target={"repo_id": "repo-1"},
            executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
        )
    )
    assert deduped is True
    assert duplicate.job_id == "job-1"

    claimed = store.claim_next_job(lock_key="worker-1", now="2026-01-01T00:00:00Z")
    assert claimed is not None
    assert claimed.lock_key == "worker-1"

    running = store.start_job("job-1", now="2026-01-01T00:00:01Z")
    assert running.state == JOB_RUNNING
    assert running.attempt_count == 1

    attempt = store.record_attempt(
        AutomationJobAttempt.create(
            attempt_id="attempt-1",
            job_id="job-1",
            attempt_number=1,
            status=JOB_SUCCEEDED,
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
            executor_result={"ok": True},
            execution_refs={"managed_thread_execution_id": "exec-1"},
        )
    )
    assert attempt.executor_result == {"ok": True}
    assert store.list_attempts("job-1")[0].attempt_id == "attempt-1"

    completed = store.complete_job(
        "job-1",
        result_summary="done",
        execution_refs={"managed_thread_execution_id": "exec-1"},
        now="2026-01-01T00:00:03Z",
    )
    assert completed.state == JOB_SUCCEEDED
    assert completed.result_summary == "done"
    assert not hasattr(completed, "managed_thread_execution_id")

    with pytest.raises(ValueError, match="terminal"):
        store.fail_job("job-1", error_text="too late")


def test_failed_job_can_be_requeued_explicitly(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule())
    store.record_event(_event())
    store.create_job(
        job_id="job-fail",
        rule_id="rule-1",
        event_id="event-1",
        target={"repo_id": "repo-1"},
        executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
        available_at="2026-01-01T00:00:00Z",
    )
    store.claim_next_job(now="2026-01-01T00:00:00Z")
    store.start_job("job-fail", now="2026-01-01T00:00:01Z")
    failed = store.fail_job("job-fail", error_text="boom", now="2026-01-01T00:00:02Z")
    assert failed.state == JOB_FAILED

    requeued = store.retry_job("job-fail", available_at="2026-01-01T00:00:03Z")
    assert requeued.state == JOB_PENDING


def test_dead_lettered_job_requires_explicit_revive(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule())
    store.record_event(_event())
    store.create_job(
        job_id="job-dead",
        rule_id="rule-1",
        event_id="event-1",
        target={"repo_id": "repo-1"},
        executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
        available_at="2026-01-01T00:00:00Z",
    )
    store.claim_next_job(now="2026-01-01T00:00:00Z")
    store.start_job("job-dead", now="2026-01-01T00:00:01Z")
    dead = store.fail_job(
        "job-dead",
        error_text="exhausted",
        dead_letter=True,
        now="2026-01-01T00:00:02Z",
    )
    assert dead.state == JOB_DEAD_LETTERED

    with pytest.raises(ValueError, match="terminal"):
        store.retry_job("job-dead", available_at="2026-01-01T00:00:03Z")

    revived = store.revive_dead_lettered_job(
        "job-dead",
        available_at="2026-01-01T00:00:04Z",
    )

    assert revived.state == JOB_PENDING
    assert revived.available_at == "2026-01-01T00:00:04Z"
    assert revived.error_text is None
    assert revived.finished_at is None


def test_claim_next_job_leaves_concurrency_to_worker(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule())
    store.record_event(_event())
    policy = {"max_concurrent_per_rule": 1, "max_concurrent_per_target": 1}
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-1",
            rule_id="rule-1",
            event_id="event-1",
            dedupe_key="dedupe-1",
            available_at="2026-01-01T00:00:00Z",
            target={"repo_id": "repo-1"},
            executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
            policy=policy,
        )
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-2",
            rule_id="rule-1",
            event_id="event-1",
            dedupe_key="dedupe-2",
            available_at="2026-01-01T00:00:00Z",
            target={"repo_id": "repo-1"},
            executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
            policy=policy,
        )
    )

    first = store.claim_next_job(lock_key="worker-1", now="2026-01-01T00:00:00Z")
    second = store.claim_next_job(lock_key="worker-2", now="2026-01-01T00:00:00Z")

    assert first is not None
    assert first.job_id == "job-1"
    assert second is not None
    assert second.job_id == "job-2"
    assert store.get_job("job-1").state == JOB_CLAIMED
    assert store.get_job("job-2").state == JOB_CLAIMED
    assert store.count_active_jobs(rule_id="rule-1") == 2


def test_release_stale_claims_recovers_claimed_and_running_jobs(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule())
    store.record_event(_event())
    store.enqueue_job(
        AutomationJob.create(
            job_id="claimed-job",
            rule_id="rule-1",
            event_id="event-1",
            dedupe_key="dedupe-claimed",
            available_at="2026-01-01T00:00:00Z",
            target={"repo_id": "repo-1", "job": "claimed"},
            executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
            policy={"max_concurrent_per_rule": 2, "max_concurrent_per_target": 1},
        )
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="running-job",
            rule_id="rule-1",
            event_id="event-1",
            dedupe_key="dedupe-running",
            available_at="2026-01-01T00:00:00Z",
            target={"repo_id": "repo-1", "job": "running"},
            executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
            policy={"max_concurrent_per_rule": 2, "max_concurrent_per_target": 1},
        )
    )
    store.claim_next_job(lock_key="old", now="2026-01-01T00:00:00Z")
    store.claim_next_job(lock_key="old", now="2026-01-01T00:00:00Z")
    store.start_job("running-job", now="2026-01-01T00:00:01Z")

    released = store.release_stale_claims(
        stale_before="2026-01-01T00:05:00Z", now="2026-01-01T00:10:00Z"
    )

    assert released == 2
    claimed = store.get_job("claimed-job")
    running = store.get_job("running-job")
    assert claimed.state == JOB_PENDING
    assert claimed.lock_key is None
    assert claimed.claimed_at is None
    assert running.state == JOB_PENDING
    assert running.lock_key is None
    assert running.claimed_at is None
    assert running.started_at is None
    assert running.attempt_count == 1


def test_release_stale_claims_preserves_running_job_with_terminal_child_edge(
    tmp_path,
) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule())
    store.record_event(_event())
    store.enqueue_job(
        AutomationJob.create(
            job_id="running-parent",
            rule_id="rule-1",
            event_id="event-1",
            dedupe_key="dedupe-running-parent",
            available_at="2026-01-01T00:00:00Z",
            target={"repo_id": "repo-1"},
            executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
        )
    )
    store.claim_next_job(lock_key="old", now="2026-01-01T00:00:00Z")
    store.start_job("running-parent", now="2026-01-01T00:00:01Z")
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="running-parent",
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="child-1",
            requested_runtime=AutomationRuntimeContract(agent="opencode"),
            terminal_state=JOB_SUCCEEDED,
            terminal_observed_at="2026-01-01T00:01:00Z",
        )
    )

    released = store.release_stale_claims(
        stale_before="2026-01-01T00:05:00Z", now="2026-01-01T00:10:00Z"
    )

    running = store.get_job("running-parent")
    assert released == 0
    assert running.state == JOB_RUNNING
    assert running.started_at == "2026-01-01T00:00:01Z"


def test_child_execution_edge_persists_runtime_identity_stages(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule())
    store.record_event(_event())
    store.enqueue_job(
        AutomationJob.create(
            job_id="parent-runtime",
            rule_id="rule-1",
            event_id="event-1",
            dedupe_key="dedupe-parent-runtime",
            available_at="2026-01-01T00:00:00Z",
            target={"repo_id": "repo-1"},
            executor={"kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN},
        )
    )
    edge = store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="parent-runtime",
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="child-runtime",
            requested_runtime={
                "agent": "opencode",
                "model": "zai-coding-plan/glm-5.1",
                "provider_payload": {
                    "providerID": "zai-coding-plan",
                    "modelID": "glm-5.1",
                },
            },
        )
    )

    assert edge.runtime_identity.requested is not None
    assert (
        edge.runtime_identity.requested.canonical_model_label
        == "zai-coding-plan/glm-5.1"
    )

    terminal = store.mark_child_execution_terminal(
        edge.edge_id,
        terminal_state=JOB_SUCCEEDED,
        terminal_event_id="child-runtime",
        actual_runtime={
            "agent": "opencode",
            "model": "zai-coding-plan/glm-5.1",
            "backend_runtime_id": "session-1",
        },
    )

    assert terminal.runtime_identity.requested is not None
    assert terminal.runtime_identity.effective is not None
    assert terminal.runtime_identity.effective.backend_runtime_id == "session-1"


def test_schedule_crud(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    rule = AutomationRule.hydrate_persisted(
        rule_id="daily-rule",
        name="Daily automation",
        trigger_kind=TRIGGER_KIND_SCHEDULE,
        trigger={"schedule_kind": SCHEDULE_DAILY},
        target_policy=TARGET_POLICY_HUB,
        executor_kind=LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
    )
    store.upsert_rule(rule)

    schedule = store.upsert_schedule(
        AutomationSchedule.create(
            schedule_id="schedule-1",
            rule_id="daily-rule",
            schedule_kind=SCHEDULE_DAILY,
            timezone="America/New_York",
            next_fire_at="2026-01-02T09:00:00-05:00",
            schedule={"hour": 9, "minute": 0},
        )
    )
    assert schedule.next_fire_at == "2026-01-02T14:00:00Z"
    assert store.list_schedules(rule_id="daily-rule")[0].schedule["hour"] == 9
