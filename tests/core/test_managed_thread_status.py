from __future__ import annotations

from codex_autorunner.core.managed_thread_status import (
    ManagedThreadStatusReason,
    backfill_managed_thread_status,
    build_managed_thread_status_snapshot,
    transition_managed_thread_status,
)


def test_transient_failure_recovery_is_deterministic() -> None:
    state = build_managed_thread_status_snapshot(
        reason=ManagedThreadStatusReason.THREAD_CREATED,
        changed_at="2026-03-08T00:00:00Z",
    )
    state = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.TURN_STARTED,
        changed_at="2026-03-08T00:00:10Z",
        turn_id="turn-1",
    )
    state = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.MANAGED_TURN_FAILED,
        changed_at="2026-03-08T00:00:20Z",
        turn_id="turn-1",
    )
    state = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.TURN_STARTED,
        changed_at="2026-03-08T00:00:30Z",
        turn_id="turn-2",
    )
    state = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.MANAGED_TURN_COMPLETED,
        changed_at="2026-03-08T00:00:40Z",
        turn_id="turn-2",
    )

    assert state.status == "completed"
    assert state.reason_code == "managed_turn_completed"
    assert state.turn_id == "turn-2"
    assert state.terminal is True


def test_paused_then_resumed_returns_to_idle() -> None:
    state = build_managed_thread_status_snapshot(
        reason=ManagedThreadStatusReason.THREAD_CREATED,
        changed_at="2026-03-08T00:00:00Z",
    )
    state = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.THREAD_COMPACTED,
        changed_at="2026-03-08T00:00:10Z",
    )
    state = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.THREAD_RESUMED,
        changed_at="2026-03-08T00:00:20Z",
    )

    assert state.status == "idle"
    assert state.reason_code == "thread_resumed"
    assert state.terminal is False


def test_duplicate_completion_event_is_idempotent() -> None:
    state = build_managed_thread_status_snapshot(
        reason=ManagedThreadStatusReason.THREAD_CREATED,
        changed_at="2026-03-08T00:00:00Z",
    )
    state = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.TURN_STARTED,
        changed_at="2026-03-08T00:00:10Z",
        turn_id="turn-1",
    )
    completed = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.MANAGED_TURN_COMPLETED,
        changed_at="2026-03-08T00:00:20Z",
        turn_id="turn-1",
    )
    duplicate = transition_managed_thread_status(
        completed,
        reason=ManagedThreadStatusReason.MANAGED_TURN_COMPLETED,
        changed_at="2026-03-08T00:00:20Z",
        turn_id="turn-1",
    )

    assert duplicate == completed


def test_out_of_order_event_delivery_ignores_stale_transition() -> None:
    state = build_managed_thread_status_snapshot(
        reason=ManagedThreadStatusReason.THREAD_CREATED,
        changed_at="2026-03-08T00:00:00Z",
    )
    state = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.TURN_STARTED,
        changed_at="2026-03-08T00:00:10Z",
        turn_id="turn-1",
    )
    state = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.MANAGED_TURN_COMPLETED,
        changed_at="2026-03-08T00:00:20Z",
        turn_id="turn-1",
    )
    stale = transition_managed_thread_status(
        state,
        reason=ManagedThreadStatusReason.TURN_STARTED,
        changed_at="2026-03-08T00:00:15Z",
        turn_id="turn-1",
    )

    assert stale == state


def test_backfill_prefers_terminal_latest_turn_over_compacted_state() -> None:
    completed = backfill_managed_thread_status(
        lifecycle_status="active",
        latest_turn_status="ok",
        changed_at="2026-03-08T00:00:20Z",
        compacted=True,
    )
    failed = backfill_managed_thread_status(
        lifecycle_status="active",
        latest_turn_status="error",
        changed_at="2026-03-08T00:00:20Z",
        compacted=True,
    )

    assert completed.status == "completed"
    assert completed.reason_code == "managed_turn_completed"
    assert failed.status == "failed"
    assert failed.reason_code == "managed_turn_failed"
