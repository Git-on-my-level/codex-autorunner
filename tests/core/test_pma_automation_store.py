from __future__ import annotations

import logging

import pytest

from codex_autorunner.core.orchestration import OrchestrationBindingStore
from codex_autorunner.core.pma_automation_store import (
    PmaAutomationStore,
    PmaAutomationThreadNotFoundError,
)
from codex_autorunner.core.pma_thread_store import (
    PmaThreadStore,
    prepare_pma_thread_store,
)


def _create_managed_thread(
    tmp_path,
    *,
    surface_kind: str | None = None,
    thread_store: PmaThreadStore | None = None,
    binding_store: OrchestrationBindingStore | None = None,
) -> str:
    ts = thread_store or PmaThreadStore(tmp_path)
    thread = ts.create_thread("codex", tmp_path)
    thread_id = str(thread["managed_thread_id"])
    if surface_kind is not None:
        bs = binding_store or OrchestrationBindingStore(tmp_path)
        bs.upsert_binding(
            surface_kind=surface_kind,
            surface_key=f"{surface_kind}:binding-1",
            thread_target_id=thread_id,
        )
    return thread_id


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


def test_explicit_prepare_runs_legacy_backfill_once(tmp_path, monkeypatch) -> None:
    call_count = 0

    def _fake_backfill(*args, **kwargs):
        nonlocal call_count
        _ = args, kwargs
        call_count += 1
        return {"subscriptions": 0, "timers": 0, "wakeups": 0}

    monkeypatch.setattr(
        "codex_autorunner.core.orchestration.legacy_backfill_gate.backfill_legacy_automation_state",
        _fake_backfill,
    )

    thread_id = _create_managed_thread(tmp_path)
    prepare_pma_thread_store(tmp_path, durable=False)
    prepare_pma_thread_store(tmp_path, durable=False)

    store = PmaAutomationStore(tmp_path, durable=False)
    state = store.load()
    created, deduped = store.upsert_subscription(
        event_types=["flow_failed"],
        thread_id=thread_id,
        idempotency_key="sub-key-1",
    )
    matches = store.match_lifecycle_subscriptions(
        event_type="flow_failed",
        thread_id=thread_id,
    )

    assert state["subscriptions"] == []
    assert deduped is False
    assert created.thread_id == thread_id
    assert len(matches) == 1
    assert call_count == 1


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
            "idle_seconds": 60,
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


def test_timer_rejects_unknown_subscription_id(tmp_path) -> None:
    store = PmaAutomationStore(tmp_path)

    with pytest.raises(ValueError, match="Unknown subscription_id: missing-sub"):
        store.create_timer(
            {
                "subscription_id": "missing-sub",
                "thread_id": "thread-1",
                "reason": "subscription-timer",
            }
        )

    assert store.list_timers(include_inactive=True) == []


def test_purge_logs_orphan_cleanup_notice(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    store = PmaAutomationStore(tmp_path)
    subscription = store.create_subscription(
        {
            "thread_id": "thread-purge",
            "lane_id": "pma:lane-purge",
            "event_types": ["failed"],
        }
    )["subscription"]
    store.create_timer(
        {
            "subscription_id": subscription["subscription_id"],
            "thread_id": "thread-purge",
            "reason": "subscription-timer",
        }
    )
    store.enqueue_wakeup(
        source="lifecycle_subscription",
        subscription_id=subscription["subscription_id"],
        thread_id="thread-purge",
        reason="subscription-wakeup",
        idempotency_key="purge-wakeup-log-1",
    )
    assert store.cancel_subscription(subscription["subscription_id"]) is True

    with caplog.at_level(
        logging.WARNING, logger="codex_autorunner.core.pma_automation_store"
    ):
        removed = store.purge_subscriptions(state_filter="cancelled", dry_run=False)

    assert [entry["subscription_id"] for entry in removed] == [
        subscription["subscription_id"]
    ]
    assert any(
        "Dropping orphaned automation rows before save (timers=1, wakeups=1)"
        in record.getMessage()
        for record in caplog.records
    )


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


def test_create_subscription_accepts_singular_event_type_and_triggers_transition(
    tmp_path,
) -> None:
    store = PmaAutomationStore(tmp_path, durable=False)
    ts = PmaThreadStore(tmp_path)
    thread_id = _create_managed_thread(tmp_path, thread_store=ts)
    subscription = store.create_subscription(
        {
            "event_type": "managed_thread_completed",
            "thread_id": thread_id,
            "lane_id": "pma:lane-next",
        }
    )["subscription"]

    assert subscription["event_types"] == ["managed_thread_completed"]

    result = store.notify_transition(
        {
            "event_type": "managed_thread_completed",
            "thread_id": thread_id,
            "from_state": "running",
            "to_state": "completed",
            "transition_id": f"{thread_id}:completed",
        }
    )

    assert result["matched"] == 1
    assert result["created"] == 1
    pending = store.list_pending_wakeups(limit=10)
    assert len(pending) == 1
    assert pending[0]["subscription_id"] == subscription["subscription_id"]
    assert pending[0]["event_type"] == "managed_thread_completed"


def test_create_subscription_warns_when_event_types_empty(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    store = PmaAutomationStore(tmp_path)

    with caplog.at_level(
        logging.WARNING, logger="codex_autorunner.core.pma_automation_store"
    ):
        subscription = store.create_subscription({"repo_id": "repo-empty"})[
            "subscription"
        ]

    assert subscription["event_types"] == []
    assert any(
        "Creating PMA subscription with empty event_types" in record.getMessage()
        for record in caplog.records
    )


def test_create_subscription_auto_resolves_lane_from_thread_binding(tmp_path) -> None:
    store = PmaAutomationStore(tmp_path, durable=False)
    thread_id = _create_managed_thread(tmp_path, surface_kind="discord")

    subscription = store.create_subscription(
        {
            "event_type": "managed_thread_completed",
            "thread_id": thread_id,
        }
    )["subscription"]

    assert subscription["thread_id"] == thread_id
    assert subscription["lane_id"] == "discord"
    assert subscription["metadata"]["delivery_target"] == {
        "surface_kind": "discord",
        "surface_key": "discord:binding-1",
    }


def test_create_subscription_prefers_origin_thread_binding_for_defaults(
    tmp_path,
) -> None:
    store = PmaAutomationStore(tmp_path, durable=False)
    ts = PmaThreadStore(tmp_path)
    bs = OrchestrationBindingStore(tmp_path, durable=False)
    managed_thread_id = _create_managed_thread(
        tmp_path, thread_store=ts, binding_store=bs
    )
    origin_thread_id = _create_managed_thread(
        tmp_path, surface_kind="discord", thread_store=ts, binding_store=bs
    )

    subscription = store.create_subscription(
        {
            "event_type": "managed_thread_completed",
            "thread_id": managed_thread_id,
            "origin_thread_id": origin_thread_id,
        }
    )["subscription"]

    assert subscription["thread_id"] == managed_thread_id
    assert subscription["lane_id"] == "discord"
    assert subscription["metadata"]["delivery_target"] == {
        "surface_kind": "discord",
        "surface_key": "discord:binding-1",
    }


def test_create_subscription_reuses_covering_auto_subscription_for_auto_keys(
    tmp_path,
) -> None:
    store = PmaAutomationStore(tmp_path, durable=False)
    ts = PmaThreadStore(tmp_path)
    thread_id = _create_managed_thread(tmp_path, thread_store=ts)

    first = store.create_subscription(
        {
            "event_types": [
                "managed_thread_completed",
                "managed_thread_failed",
                "managed_thread_interrupted",
            ],
            "thread_id": thread_id,
            "idempotency_key": f"managed-thread-notify:{thread_id}",
            "notify_once": True,
        }
    )
    second = store.create_subscription(
        {
            "event_types": [
                "managed_thread_completed",
                "managed_thread_failed",
                "managed_thread_interrupted",
            ],
            "thread_id": thread_id,
            "idempotency_key": "managed-thread-send-notify:turn-1",
            "notify_once": True,
        }
    )

    assert first["deduped"] is False
    assert second["deduped"] is True
    assert (
        second["subscription"]["subscription_id"]
        == first["subscription"]["subscription_id"]
    )
    subscriptions = store.list_subscriptions(thread_id=thread_id)
    assert len(subscriptions) == 1


def test_notify_transition_copies_subscription_delivery_target_into_wakeup(
    tmp_path,
) -> None:
    store = PmaAutomationStore(tmp_path, durable=False)
    thread_id = _create_managed_thread(tmp_path, surface_kind="telegram")

    subscription = store.create_subscription(
        {
            "event_type": "managed_thread_completed",
            "thread_id": thread_id,
        }
    )["subscription"]

    result = store.notify_transition(
        {
            "event_type": "managed_thread_completed",
            "thread_id": thread_id,
            "from_state": "running",
            "to_state": "completed",
            "transition_id": f"{thread_id}:completed",
        }
    )

    assert result["matched"] == 1
    assert result["created"] == 1
    pending = store.list_pending_wakeups(limit=10)
    assert len(pending) == 1
    assert pending[0]["subscription_id"] == subscription["subscription_id"]
    assert pending[0]["metadata"]["delivery_target"] == {
        "surface_kind": "telegram",
        "surface_key": "telegram:binding-1",
    }


def test_create_subscription_with_unknown_thread_requires_resolvable_lane(
    tmp_path,
) -> None:
    store = PmaAutomationStore(tmp_path)

    with pytest.raises(PmaAutomationThreadNotFoundError, match="Unknown thread_id"):
        store.create_subscription(
            {
                "event_type": "managed_thread_completed",
                "thread_id": "missing-thread",
            }
        )


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
