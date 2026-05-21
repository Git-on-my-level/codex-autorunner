from __future__ import annotations

from types import SimpleNamespace

from codex_autorunner.core.automation import (
    AutomationEvent,
    AutomationJob,
    AutomationRule,
    AutomationSchedule,
    AutomationStore,
)
from codex_autorunner.core.automation.models import (
    EXECUTOR_MANAGED_THREAD_TURN,
    SCHEDULE_ONE_SHOT,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
)
from codex_autorunner.core.pma_automation_snapshot import snapshot_pma_automation


def test_snapshot_pma_automation_samples_pending_automation_jobs(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-1",
            name="Managed thread follow-up",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["lifecycle.flow_completed"]},
            target_policy=TARGET_POLICY_HUB,
            executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
            system_owned=True,
        )
    )
    store.record_event(
        AutomationEvent.create(
            event_id="event-1", event_type="lifecycle.flow_completed"
        )
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-1",
            rule_id="rule-1",
            event_id="event-1",
            target={},
            executor={"kind": EXECUTOR_MANAGED_THREAD_TURN},
        )
    )
    supervisor = SimpleNamespace(hub_config=SimpleNamespace(root=tmp_path))

    snapshot = snapshot_pma_automation(supervisor, max_items=10)

    assert snapshot["wakeups"]["pending_count"] == 1
    assert snapshot["wakeups"]["pending_sample"][0]["job_id"] == "job-1"


def test_snapshot_pma_automation_includes_unified_read_model(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-1",
            name="PMA timer",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["schedule.fire"]},
            target_policy=TARGET_POLICY_HUB,
            executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
            system_owned=True,
        )
    )
    store.upsert_schedule(
        AutomationSchedule.create(
            schedule_id="schedule-1",
            rule_id="rule-1",
            schedule_kind=SCHEDULE_ONE_SHOT,
            next_fire_at="2026-01-01T00:00:00Z",
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-1", event_type="schedule.fire")
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-1",
            rule_id="rule-1",
            event_id="event-1",
            target={},
            executor={"kind": EXECUTOR_MANAGED_THREAD_TURN},
        )
    )
    supervisor = SimpleNamespace(hub_config=SimpleNamespace(root=tmp_path))

    snapshot = snapshot_pma_automation(supervisor, max_items=10)

    assert snapshot["rules"]["enabled_count"] == 1
    assert snapshot["rules"]["sample"][0]["rule_id"] == "rule-1"
    assert snapshot["schedules"]["active_count"] == 1
    assert snapshot["schedules"]["sample"][0]["schedule_id"] == "schedule-1"
    assert snapshot["jobs"]["pending_count"] == 1
    assert snapshot["jobs"]["recent_sample"][0]["job_id"] == "job-1"
