from __future__ import annotations

from codex_autorunner.core.automation import (
    AutomationRule,
    AutomationRuleEngine,
    AutomationSchedule,
    AutomationScheduler,
    AutomationStore,
    calculate_next_fire_at,
)
from codex_autorunner.core.automation.models import (
    EXECUTOR_PMA_TURN,
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONE_SHOT,
    SCHEDULE_WEEKLY,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_SCHEDULE,
)


def _schedule_rule(rule_id="rule-1") -> AutomationRule:
    return AutomationRule.create(
        rule_id=rule_id,
        name="Scheduled PMA turn",
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"event_types": ["schedule.fire"]},
        filters={"schedule.rule_id": rule_id},
        target_policy=TARGET_POLICY_HUB,
        executor_kind=EXECUTOR_PMA_TURN,
        executor={"lane_id": "pma:default"},
        policy={"dedupe_key": "{{ event.event_id }}"},
    )


def test_due_one_shot_schedule_fires_once_and_enqueues_job(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_schedule_rule())
    store.upsert_schedule(
        AutomationSchedule.create(
            schedule_id="schedule-1",
            rule_id="rule-1",
            schedule_kind=SCHEDULE_ONE_SHOT,
            next_fire_at="2026-01-01T00:00:00Z",
        )
    )

    scheduler = AutomationScheduler(store, AutomationRuleEngine(store))
    result = scheduler.process_due(now="2026-01-01T00:00:00Z")
    second = scheduler.process_due(now="2026-01-01T00:00:00Z")

    assert result.schedules_fired == 1
    assert result.jobs_created == 1
    assert second.schedules_fired == 0
    assert store.get_schedule("schedule-1").state == "completed"
    assert len(store.list_events(event_type="schedule.fire")) == 1


def test_due_schedule_rule_uses_schedule_fire_event_path(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(
        AutomationRule.create(
            rule_id="daily-rule",
            name="Daily PMA turn",
            trigger_kind=TRIGGER_KIND_SCHEDULE,
            trigger={"schedule_kind": SCHEDULE_DAILY},
            target_policy=TARGET_POLICY_HUB,
            target={"repo_id": "{{ schedule.payload.repo_id }}"},
            executor_kind=EXECUTOR_PMA_TURN,
            executor={"message": "Fire {{ schedule.rule_id }}"},
            policy={"dedupe_key": "{{ event.event_id }}"},
        )
    )
    store.upsert_schedule(
        AutomationSchedule.create(
            schedule_id="schedule-1",
            rule_id="daily-rule",
            schedule_kind=SCHEDULE_DAILY,
            next_fire_at="2026-01-01T00:00:00Z",
            schedule={"payload": {"repo_id": "repo-1"}},
        )
    )

    result = AutomationScheduler(store, AutomationRuleEngine(store)).process_due(
        now="2026-01-01T00:00:00Z"
    )

    assert result.jobs_created == 1
    job = store.list_jobs()[0]
    assert job.event_id.startswith("schedule.fire:schedule-1:")
    assert job.target["repo_id"] == "repo-1"
    assert job.executor["message"] == "Fire daily-rule"


def test_interval_daily_and_weekly_next_fire_calculation() -> None:
    interval = AutomationSchedule.create(
        rule_id="rule",
        schedule_kind=SCHEDULE_INTERVAL,
        next_fire_at="2026-01-01T00:00:00Z",
        schedule={"interval_seconds": 90},
    )
    assert calculate_next_fire_at(interval) == "2026-01-01T00:01:30Z"

    daily = AutomationSchedule.create(
        rule_id="rule",
        schedule_kind=SCHEDULE_DAILY,
        timezone="America/New_York",
        next_fire_at="2026-01-01T14:00:00Z",
        schedule={"hour": 9, "minute": 30},
    )
    assert calculate_next_fire_at(daily) == "2026-01-02T14:30:00Z"

    weekly = AutomationSchedule.create(
        rule_id="rule",
        schedule_kind=SCHEDULE_WEEKLY,
        next_fire_at="2026-01-05T10:00:00Z",
        schedule={"weekdays": [0], "hour": 10, "minute": 0},
    )
    assert calculate_next_fire_at(weekly) == "2026-01-12T10:00:00Z"


def test_missed_daily_schedule_advances_to_future_after_single_fire() -> None:
    daily = AutomationSchedule.create(
        rule_id="rule",
        schedule_kind=SCHEDULE_DAILY,
        timezone="UTC",
        next_fire_at="2026-01-01T09:00:00Z",
        schedule={"hour": 9, "minute": 0},
    )

    assert (
        calculate_next_fire_at(daily, now="2026-01-05T12:00:00Z")
        == "2026-01-06T09:00:00Z"
    )


def test_missed_interval_schedule_advances_to_future_after_single_fire() -> None:
    interval = AutomationSchedule.create(
        rule_id="rule",
        schedule_kind=SCHEDULE_INTERVAL,
        next_fire_at="2026-01-01T00:00:00Z",
        schedule={"interval_seconds": 60},
    )

    assert (
        calculate_next_fire_at(interval, now="2026-01-01T00:05:30Z")
        == "2026-01-01T00:06:00Z"
    )
