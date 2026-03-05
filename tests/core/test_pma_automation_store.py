from __future__ import annotations

import pytest

from codex_autorunner.core.pma_automation_store import PmaAutomationStore


def test_subscription_idempotent_dedupe_and_lifecycle_matching(tmp_path) -> None:
    store = PmaAutomationStore(tmp_path)

    first, deduped = store.upsert_subscription(
        event_types=["flow_failed"],
        repo_id="repo-1",
        run_id="run-1",
        from_state="running",
        to_state="failed",
        reason="manual_check",
        idempotency_key="sub-key-1",
    )
    assert deduped is False

    duplicate, deduped = store.upsert_subscription(
        event_types=["flow_failed"],
        repo_id="repo-1",
        run_id="run-1",
        from_state="running",
        to_state="failed",
        reason="manual_check",
        idempotency_key="sub-key-1",
    )
    assert deduped is True
    assert duplicate.subscription_id == first.subscription_id

    matches = store.match_lifecycle_subscriptions(
        event_type="flow_failed",
        repo_id="repo-1",
        run_id="run-1",
        from_state="running",
        to_state="failed",
    )
    assert len(matches) == 1
    assert matches[0]["subscription_id"] == first.subscription_id


def test_due_timers_fire_once(tmp_path) -> None:
    store = PmaAutomationStore(tmp_path)

    created, deduped = store.upsert_timer(
        due_at="2000-01-01T00:00:00+00:00",
        thread_id="thread-123",
        reason="timer_due",
        idempotency_key="timer-key-1",
    )
    assert deduped is False

    due = store.dequeue_due_timers(limit=10)
    assert len(due) == 1
    assert due[0]["timer_id"] == created.timer_id
    assert due[0]["state"] == "fired"
    assert isinstance(due[0]["fired_at"], str)

    due_again = store.dequeue_due_timers(limit=10)
    assert due_again == []


def test_watchdog_timer_refires_until_touched(tmp_path) -> None:
    store = PmaAutomationStore(tmp_path)

    created = store.create_timer(
        {
            "timer_type": "watchdog",
            "idle_seconds": 1,
            "thread_id": "thread-abc",
            "reason": "watchdog_stalled",
            "idempotency_key": "watchdog-1",
        }
    )["timer"]
    timer_id = created["timer_id"]

    # Force an immediate fire.
    touched = store.touch_timer(timer_id, {"due_at": "2000-01-01T00:00:00+00:00"})
    assert touched["touched"] is True

    first_due = store.dequeue_due_timers(limit=10)
    assert len(first_due) == 1
    assert first_due[0]["timer_id"] == timer_id
    assert first_due[0]["timer_type"] == "watchdog"

    second_due = store.dequeue_due_timers(limit=10)
    assert second_due == []

    # Touch again to simulate progress heartbeat and force another due.
    touched_again = store.touch_timer(
        timer_id,
        {"delay_seconds": 0, "reason": "heartbeat"},
    )
    assert touched_again["touched"] is True
    assert touched_again["timer"]["reason"] == "heartbeat"

    third_due = store.dequeue_due_timers(limit=10)
    assert len(third_due) == 1
    assert third_due[0]["timer_id"] == timer_id


def test_wakeup_queue_dedup_and_dispatch_persists(tmp_path) -> None:
    store = PmaAutomationStore(tmp_path)

    created, deduped = store.enqueue_wakeup(
        source="lifecycle_subscription",
        repo_id="repo-1",
        run_id="run-1",
        from_state="running",
        to_state="failed",
        reason="manual_check",
        timestamp="2026-01-01T00:00:00+00:00",
        idempotency_key="wake-key-1",
    )
    assert deduped is False

    duplicate, deduped = store.enqueue_wakeup(
        source="lifecycle_subscription",
        repo_id="repo-1",
        run_id="run-1",
        from_state="running",
        to_state="failed",
        reason="manual_check",
        timestamp="2026-01-01T00:00:00+00:00",
        idempotency_key="wake-key-1",
    )
    assert deduped is True
    assert duplicate.wakeup_id == created.wakeup_id

    pending = store.list_pending_wakeups(limit=10)
    assert len(pending) == 1
    assert pending[0]["wakeup_id"] == created.wakeup_id

    assert store.mark_wakeup_dispatched(created.wakeup_id) is True
    assert store.mark_wakeup_dispatched(created.wakeup_id) is False

    reloaded = PmaAutomationStore(tmp_path)
    pending_after = reloaded.list_pending_wakeups(limit=10)
    assert pending_after == []
    dispatched = reloaded.list_wakeups(state_filter="dispatched")
    assert len(dispatched) == 1
    assert dispatched[0]["wakeup_id"] == created.wakeup_id


def test_subscription_lane_id_flows_into_transition_wakeup(tmp_path) -> None:
    store = PmaAutomationStore(tmp_path)
    subscription = store.create_subscription(
        {
            "event_types": ["flow_completed"],
            "repo_id": "repo-1",
            "run_id": "run-1",
            "from_state": "running",
            "to_state": "completed",
            "lane_id": "pma:lane-next",
        }
    )["subscription"]

    result = store.notify_transition(
        {
            "event_type": "flow_completed",
            "repo_id": "repo-1",
            "run_id": "run-1",
            "from_state": "running",
            "to_state": "completed",
            "transition_id": "flow-completed-1",
            "reason": "completion",
        }
    )
    assert result["matched"] == 1
    assert result["created"] == 1

    pending = store.list_pending_wakeups(limit=10)
    assert len(pending) == 1
    assert pending[0]["subscription_id"] == subscription["subscription_id"]
    assert pending[0]["lane_id"] == "pma:lane-next"


def test_notify_once_subscription_cancels_after_first_match(tmp_path) -> None:
    store = PmaAutomationStore(tmp_path)
    subscription = store.create_subscription(
        {
            "event_types": ["managed_thread_completed"],
            "thread_id": "thread-1",
            "lane_id": "pma:lane-next",
            "notify_once": True,
        }
    )["subscription"]

    first = store.notify_transition(
        {
            "event_type": "managed_thread_completed",
            "thread_id": "thread-1",
            "from_state": "running",
            "to_state": "completed",
            "transition_id": "managed-turn-1:completed",
        }
    )
    assert first["matched"] == 1
    assert first["created"] == 1

    active = store.list_subscriptions(thread_id="thread-1")
    assert active == []
    all_subs = store.list_subscriptions(include_inactive=True, thread_id="thread-1")
    assert all_subs
    assert all_subs[0]["subscription_id"] == subscription["subscription_id"]
    assert all_subs[0]["state"] == "cancelled"
    assert all_subs[0]["match_count"] == 1
    assert all_subs[0]["max_matches"] == 1

    second = store.notify_transition(
        {
            "event_type": "managed_thread_completed",
            "thread_id": "thread-1",
            "from_state": "running",
            "to_state": "completed",
            "transition_id": "managed-turn-2:completed",
        }
    )
    assert second["matched"] == 0
    assert second["created"] == 0


def test_timer_rejects_invalid_due_at_timestamp(tmp_path) -> None:
    store = PmaAutomationStore(tmp_path)
    with pytest.raises(ValueError):
        store.create_timer({"timer_type": "one_shot", "due_at": "not-a-timestamp"})
