from __future__ import annotations

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
