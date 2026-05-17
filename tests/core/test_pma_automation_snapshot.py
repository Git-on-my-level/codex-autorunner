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
    EXECUTOR_PMA_TURN,
    SCHEDULE_ONE_SHOT,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
)
from codex_autorunner.core.pma_automation_snapshot import snapshot_pma_automation


class _FakeAutomationStore:
    def list_subscriptions(self, **_: object) -> list[dict[str, object]]:
        return []

    def list_timers(self, **_: object) -> list[dict[str, object]]:
        return []

    def list_wakeups(
        self, *, state_filter: str | None = None, **_: object
    ) -> list[dict[str, object]]:
        if state_filter == "pending":
            return [
                {
                    "wakeup_id": f"wake-{index:02d}",
                    "timestamp": f"2026-04-13T12:{index:02d}:00Z",
                    "reason": f"pending-{index:02d}",
                }
                for index in range(12)
            ]
        if state_filter == "dispatched":
            return [{"wakeup_id": "done-1"}]
        return []


class _FakeSupervisor:
    pma_automation_store = _FakeAutomationStore()


def test_snapshot_pma_automation_samples_newest_pending_wakeups() -> None:
    snapshot = snapshot_pma_automation(_FakeSupervisor(), max_items=10)

    assert snapshot["wakeups"]["pending_count"] == 12
    assert [entry["wakeup_id"] for entry in snapshot["wakeups"]["pending_sample"]] == [
        "wake-11",
        "wake-10",
        "wake-09",
        "wake-08",
        "wake-07",
        "wake-06",
        "wake-05",
        "wake-04",
        "wake-03",
        "wake-02",
    ]


def test_snapshot_pma_automation_includes_unified_read_model(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-1",
            name="PMA timer",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["schedule.fire"]},
            target_policy=TARGET_POLICY_HUB,
            executor_kind=EXECUTOR_PMA_TURN,
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
            executor={"kind": EXECUTOR_PMA_TURN},
        )
    )
    supervisor = SimpleNamespace(
        pma_automation_store=_FakeAutomationStore(),
        hub_config=SimpleNamespace(root=tmp_path),
    )

    snapshot = snapshot_pma_automation(supervisor, max_items=10)

    assert snapshot["rules"]["enabled_count"] == 1
    assert snapshot["rules"]["sample"][0]["rule_id"] == "rule-1"
    assert snapshot["schedules"]["active_count"] == 1
    assert snapshot["schedules"]["sample"][0]["schedule_id"] == "schedule-1"
    assert snapshot["jobs"]["pending_count"] == 1
    assert snapshot["jobs"]["recent_sample"][0]["job_id"] == "job-1"
