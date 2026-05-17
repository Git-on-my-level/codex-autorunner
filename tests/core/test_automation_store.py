from __future__ import annotations

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
    EXECUTOR_MANAGED_THREAD_TURN,
    EXECUTOR_PMA_TURN,
    JOB_FAILED,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    SCHEDULE_DAILY,
    TARGET_POLICY_AUTO_WORKTREE,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_SCHEDULE,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_automation_store import PmaAutomationStore


def _rule() -> AutomationRule:
    return AutomationRule.create(
        rule_id="rule-1",
        name="PR review follow-up",
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"event_types": ["scm.github.pull_request_review.submitted"]},
        filters={"repo_id": "repo-1"},
        target_policy=TARGET_POLICY_AUTO_WORKTREE,
        target={"repo_id": "repo-1"},
        executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
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
        executor={"kind": EXECUTOR_MANAGED_THREAD_TURN},
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
            executor={"kind": EXECUTOR_MANAGED_THREAD_TURN},
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
    assert completed.managed_thread_execution_id == "exec-1"

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
        executor={"kind": EXECUTOR_MANAGED_THREAD_TURN},
        available_at="2026-01-01T00:00:00Z",
    )
    store.claim_next_job(now="2026-01-01T00:00:00Z")
    store.start_job("job-fail", now="2026-01-01T00:00:01Z")
    failed = store.fail_job("job-fail", error_text="boom", now="2026-01-01T00:00:02Z")
    assert failed.state == JOB_FAILED

    requeued = store.retry_job("job-fail", available_at="2026-01-01T00:00:03Z")
    assert requeued.state == JOB_PENDING


def test_schedule_crud(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    rule = AutomationRule.create(
        rule_id="daily-rule",
        name="Daily automation",
        trigger_kind=TRIGGER_KIND_SCHEDULE,
        trigger={"schedule_kind": SCHEDULE_DAILY},
        target_policy=TARGET_POLICY_HUB,
        executor_kind=EXECUTOR_PMA_TURN,
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


def test_backfill_legacy_pma_rows_creates_unified_rows(tmp_path) -> None:
    pma_store = PmaAutomationStore(tmp_path)
    subscription = pma_store.create_subscription(
        {
            "event_types": ["flow_failed"],
            "repo_id": "repo-legacy",
            "run_id": "run-legacy",
            "thread_id": "thread-legacy",
            "lane_id": "pma:default",
            "from_state": "running",
            "to_state": "failed",
            "metadata": {"source": "test"},
        }
    )["subscription"]
    pma_store.create_timer(
        {
            "subscription_id": subscription["subscription_id"],
            "due_at": "2026-01-01T00:00:00Z",
            "thread_id": "thread-legacy",
            "reason": "timer",
            "idempotency_key": "timer-legacy",
        }
    )
    pma_store.enqueue_wakeup(
        source="lifecycle_subscription",
        subscription_id=subscription["subscription_id"],
        repo_id="repo-legacy",
        run_id="run-legacy",
        thread_id="thread-legacy",
        lane_id="pma:default",
        from_state="running",
        to_state="failed",
        reason="legacy wakeup",
        event_type="flow_failed",
        idempotency_key="wakeup-legacy",
    )

    store = AutomationStore(tmp_path)
    counts = store.backfill_legacy_pma_automation()

    assert counts["rules"] >= 1
    assert counts["events"] == 1
    assert counts["jobs"] == 1
    assert counts["schedules"] == 1
    assert (
        store.list_rules()[0].metadata["legacy_source"]
        == "orch_automation_subscriptions"
    )
    assert store.list_events()[0].event_type == "lifecycle.flow_failed"
    assert store.list_jobs()[0].pma_lane_id == "pma:default"
    assert store.list_schedules()[0].schedule_kind == "one_shot"

    assert store.backfill_legacy_pma_automation() == {
        "rules": 0,
        "events": 0,
        "jobs": 0,
        "schedules": 0,
    }
