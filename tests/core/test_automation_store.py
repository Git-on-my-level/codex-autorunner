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
    EXECUTOR_MANAGED_THREAD_TURN,
    JOB_CLAIMED,
    JOB_DEAD_LETTERED,
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
from codex_autorunner.core.pma_automation_unified import (
    PmaLegacyAutomationMigrationError,
)


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
                    "agent_task_turn",
                    json.dumps(
                        {"kind": "agent_task_turn", "prompt": "Run future task"}
                    ),
                    "rule-1",
                ),
            )

    loaded = store.get_rule("rule-1")
    assert loaded is not None
    assert loaded.executor_kind == "agent_task_turn"
    assert loaded.executor["kind"] == "agent_task_turn"
    assert loaded.known_executor is False
    assert loaded.executable is False
    assert store.list_rules()[0].executor_kind == "agent_task_turn"


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


def test_dead_lettered_job_requires_explicit_revive(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule())
    store.record_event(_event())
    store.create_job(
        job_id="job-dead",
        rule_id="rule-1",
        event_id="event-1",
        target={"repo_id": "repo-1"},
        executor={"kind": EXECUTOR_MANAGED_THREAD_TURN},
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


def test_claim_next_job_counts_claimed_jobs_against_concurrency(tmp_path) -> None:
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
            executor={"kind": EXECUTOR_MANAGED_THREAD_TURN},
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
            executor={"kind": EXECUTOR_MANAGED_THREAD_TURN},
            policy=policy,
        )
    )

    first = store.claim_next_job(lock_key="worker-1", now="2026-01-01T00:00:00Z")
    second = store.claim_next_job(lock_key="worker-2", now="2026-01-01T00:00:00Z")

    assert first is not None
    assert first.job_id == "job-1"
    assert second is None
    assert store.get_job("job-1").state == JOB_CLAIMED
    assert store.get_job("job-2").state == JOB_PENDING
    assert store.count_active_jobs(rule_id="rule-1") == 1


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
            executor={"kind": EXECUTOR_MANAGED_THREAD_TURN},
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
            executor={"kind": EXECUTOR_MANAGED_THREAD_TURN},
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


def test_schedule_crud(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    rule = AutomationRule.create(
        rule_id="daily-rule",
        name="Daily automation",
        trigger_kind=TRIGGER_KIND_SCHEDULE,
        trigger={"schedule_kind": SCHEDULE_DAILY},
        target_policy=TARGET_POLICY_HUB,
        executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
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


def _insert_legacy_pma_rows(tmp_path) -> None:
    with open_orchestration_sqlite(tmp_path) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO orch_automation_subscriptions (
                    subscription_id, event_types_json, repo_id, run_id,
                    thread_target_id, lane_id, from_state, to_state, notify_once,
                    state, match_count, metadata_json, created_at, updated_at,
                    reason_text, idempotency_key, max_matches
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "sub-legacy",
                    json.dumps(["flow_failed"]),
                    "repo-legacy",
                    "run-legacy",
                    "thread-legacy",
                    "pma:default",
                    "running",
                    "failed",
                    0,
                    "active",
                    0,
                    json.dumps({"source": "test"}),
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    "watch failures",
                    "sub-key",
                    None,
                ),
            )
            conn.execute(
                """
                INSERT INTO orch_automation_timers (
                    timer_id, subscription_id, repo_id, run_id, thread_target_id,
                    timer_kind, schedule_key, available_at, payload_json, state,
                    created_at, updated_at, fired_at, reason_text, idempotency_key,
                    idle_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "timer-legacy",
                    "sub-legacy",
                    "repo-legacy",
                    "run-legacy",
                    "thread-legacy",
                    "one_shot",
                    "sub-legacy",
                    "2026-01-02T00:00:00Z",
                    json.dumps(
                        {"lane_id": "pma:default", "metadata": {"source": "test"}}
                    ),
                    "pending",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    None,
                    "timer",
                    "timer-key",
                    None,
                ),
            )
            conn.execute(
                """
                INSERT INTO orch_automation_wakeups (
                    wakeup_id, subscription_id, repo_id, run_id, thread_target_id,
                    lane_id, wakeup_kind, state, available_at, claimed_at,
                    completed_at, reason_text, payload_json, created_at, updated_at,
                    timestamp, idempotency_key, timer_id, event_id, event_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "wakeup-legacy",
                    "sub-legacy",
                    "repo-legacy",
                    "run-legacy",
                    "thread-legacy",
                    "pma:default",
                    "lifecycle_subscription",
                    "completed",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:01Z",
                    "2026-01-01T00:00:02Z",
                    "legacy wakeup",
                    json.dumps({"from_state": "running", "to_state": "failed"}),
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:02Z",
                    "2026-01-01T00:00:00Z",
                    "wakeup-key",
                    None,
                    "event-legacy",
                    "flow_failed",
                ),
            )


def test_explicit_legacy_pma_migration_creates_unified_rows(tmp_path) -> None:
    _insert_legacy_pma_rows(tmp_path)

    store = AutomationStore(tmp_path)
    counts = store.migrate_legacy_pma_automation()

    assert counts["rules"] == 2
    assert counts["events"] == 1
    assert counts["jobs"] == 1
    assert counts["schedules"] == 1
    assert counts["attempts"] == 1
    assert counts["diagnostics"] == []
    assert any(
        rule.metadata.get("legacy_source_table") == "orch_automation_subscriptions"
        for rule in store.list_rules()
    )
    assert store.list_events()[0].event_type == "lifecycle.flow_failed"
    assert store.list_jobs()[0].executor["kind"] == EXECUTOR_MANAGED_THREAD_TURN
    assert (
        store.list_attempts("legacy-pma-wakeup:wakeup-legacy")[0].status == "succeeded"
    )
    assert store.list_schedules()[0].schedule_kind == "one_shot"

    assert store.migrate_legacy_pma_automation() == {
        "rules": 0,
        "events": 0,
        "jobs": 0,
        "schedules": 0,
        "attempts": 0,
        "diagnostics": [],
    }


def test_legacy_pma_migration_reports_malformed_rows(tmp_path) -> None:
    _insert_legacy_pma_rows(tmp_path)
    with open_orchestration_sqlite(tmp_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        with conn:
            conn.execute(
                "UPDATE orch_automation_subscriptions SET event_types_json = ? "
                "WHERE subscription_id = ?",
                ("not-json", "sub-legacy"),
            )
            conn.execute(
                "UPDATE orch_automation_timers SET subscription_id = ? "
                "WHERE timer_id = ?",
                ("missing-sub", "timer-legacy"),
            )

    with pytest.raises(PmaLegacyAutomationMigrationError) as excinfo:
        AutomationStore(tmp_path).migrate_legacy_pma_automation()

    diagnostics = [item.to_dict() for item in excinfo.value.diagnostics]
    assert {item["code"] for item in diagnostics} >= {
        "PMA_LEGACY_AUTOMATION_MALFORMED_JSON",
        "PMA_LEGACY_AUTOMATION_ORPHANED_ROW",
    }
    assert AutomationStore(tmp_path).list_rules() == []
