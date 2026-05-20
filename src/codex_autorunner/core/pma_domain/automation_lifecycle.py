from __future__ import annotations

from .constants import (
    SUBSCRIPTION_STATE_ACTIVE,
    SUBSCRIPTION_STATE_CANCELLED,
    TIMER_STATE_CANCELLED,
    TIMER_STATE_FIRED,
    TIMER_STATE_PENDING,
    WAKEUP_STATE_DISPATCHED,
    WAKEUP_STATE_PENDING,
    WAKEUP_STATE_QUEUED,
    WAKEUP_STATE_WORKER_STARTED,
)

SCHEDULE_STATE_ACTIVE = "active"
SCHEDULE_STATE_CANCELLED = "cancelled"

SUBSCRIPTION_STATES = frozenset(
    {SUBSCRIPTION_STATE_ACTIVE, SUBSCRIPTION_STATE_CANCELLED}
)
TIMER_STATES = frozenset(
    {TIMER_STATE_PENDING, TIMER_STATE_FIRED, TIMER_STATE_CANCELLED}
)
WAKEUP_STATES = frozenset(
    {
        WAKEUP_STATE_PENDING,
        WAKEUP_STATE_QUEUED,
        WAKEUP_STATE_WORKER_STARTED,
        WAKEUP_STATE_DISPATCHED,
    }
)
SCHEDULE_STATES = frozenset({SCHEDULE_STATE_ACTIVE, SCHEDULE_STATE_CANCELLED})

SUBSCRIPTION_ACTIVE_STATES = frozenset({SUBSCRIPTION_STATE_ACTIVE})
TIMER_ACTIVE_STATES = frozenset({TIMER_STATE_PENDING})
WAKEUP_ACTIVE_STATES = frozenset({WAKEUP_STATE_PENDING, WAKEUP_STATE_QUEUED})
SCHEDULE_ACTIVE_STATES = frozenset({SCHEDULE_STATE_ACTIVE})


def _require_state(state: str, *, lifecycle: str, valid_states: frozenset[str]) -> str:
    normalized = str(state or "").strip().lower()
    if normalized not in valid_states:
        raise RuntimeError(
            f"unknown PMA automation {lifecycle} lifecycle state: {state}"
        )
    return normalized


def cancel_subscription_state(state: str) -> tuple[str, bool]:
    current = _require_state(
        state, lifecycle="subscription", valid_states=SUBSCRIPTION_STATES
    )
    if current == SUBSCRIPTION_STATE_CANCELLED:
        return current, False
    return SUBSCRIPTION_STATE_CANCELLED, True


def cancel_timer_state(state: str) -> tuple[str, bool]:
    current = _require_state(state, lifecycle="timer", valid_states=TIMER_STATES)
    if current == TIMER_STATE_CANCELLED:
        return current, False
    return TIMER_STATE_CANCELLED, True


def cancel_schedule_state(state: str) -> tuple[str, bool]:
    current = _require_state(state, lifecycle="schedule", valid_states=SCHEDULE_STATES)
    if current == SCHEDULE_STATE_CANCELLED:
        return current, False
    return SCHEDULE_STATE_CANCELLED, True


def subscription_is_active_for_purge(state: str) -> bool:
    current = _require_state(
        state, lifecycle="subscription", valid_states=SUBSCRIPTION_STATES
    )
    return current in SUBSCRIPTION_ACTIVE_STATES


def timer_is_active_for_purge(state: str) -> bool:
    current = _require_state(state, lifecycle="timer", valid_states=TIMER_STATES)
    return current in TIMER_ACTIVE_STATES


def wakeup_is_active_for_purge(state: str) -> bool:
    current = _require_state(state, lifecycle="wakeup", valid_states=WAKEUP_STATES)
    return current in WAKEUP_ACTIVE_STATES


def schedule_is_active_for_purge(state: str) -> bool:
    current = _require_state(state, lifecycle="schedule", valid_states=SCHEDULE_STATES)
    return current in SCHEDULE_ACTIVE_STATES
