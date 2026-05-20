"""Review workflow lifecycle reducer.

This module is the single owner for review status transitions.  The service
observes runtime facts and persists state, but status changes must pass through
``reduce_review_lifecycle`` so legal transitions, timestamps, and observable
transition metadata stay deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ...core.state import now_iso
from .models import ACTIVE_REVIEW_STATUSES, ReviewState, ReviewStatus


class ReviewTriggerKind(str, Enum):
    START = "start"
    REQUEST_STOP = "request_stop"
    RECOVER_INTERRUPTED = "recover_interrupted"
    MARK_STOPPED = "mark_stopped"
    MARK_FAILED = "mark_failed"
    MARK_COMPLETED = "mark_completed"
    RESET = "reset"


@dataclass(frozen=True)
class ReviewTrigger:
    kind: ReviewTriggerKind
    reason: str
    error_message: Optional[str] = None
    scratchpad_bundle_path: Optional[str] = None


@dataclass(frozen=True)
class ReviewTransitionResult:
    state: ReviewState
    from_status: ReviewStatus
    to_status: ReviewStatus
    trigger: ReviewTriggerKind
    reason: str
    changed: bool


class InvalidReviewTransition(ValueError):
    pass


_TERMINAL_STATUSES = frozenset(
    {
        ReviewStatus.IDLE,
        ReviewStatus.STOPPED,
        ReviewStatus.COMPLETED,
        ReviewStatus.FAILED,
        ReviewStatus.INTERRUPTED,
    }
)


def reduce_review_lifecycle(
    state: ReviewState, trigger: ReviewTrigger
) -> ReviewTransitionResult:
    if trigger.kind == ReviewTriggerKind.START:
        return _start(state, trigger)
    if trigger.kind == ReviewTriggerKind.REQUEST_STOP:
        return _request_stop(state, trigger)
    if trigger.kind == ReviewTriggerKind.RECOVER_INTERRUPTED:
        return _recover_interrupted(state, trigger)
    if trigger.kind == ReviewTriggerKind.MARK_STOPPED:
        return _mark_stopped(state, trigger)
    if trigger.kind == ReviewTriggerKind.MARK_FAILED:
        return _mark_failed(state, trigger)
    if trigger.kind == ReviewTriggerKind.MARK_COMPLETED:
        return _mark_completed(state, trigger)
    if trigger.kind == ReviewTriggerKind.RESET:
        return _reset(state, trigger)
    raise InvalidReviewTransition(f"Unknown review trigger: {trigger.kind}")


def _result(
    state: ReviewState,
    *,
    previous: ReviewStatus,
    trigger: ReviewTrigger,
    changed: bool,
) -> ReviewTransitionResult:
    return ReviewTransitionResult(
        state=state,
        from_status=previous,
        to_status=state.status,
        trigger=trigger.kind,
        reason=trigger.reason,
        changed=changed,
    )


def _require_status(
    state: ReviewState, trigger: ReviewTrigger, allowed: frozenset[ReviewStatus]
) -> None:
    if state.status not in allowed:
        allowed_names = ", ".join(sorted(status.value for status in allowed))
        raise InvalidReviewTransition(
            f"Trigger {trigger.kind.value} requires status in ({allowed_names}), "
            f"got {state.status.value}"
        )


def _start(state: ReviewState, trigger: ReviewTrigger) -> ReviewTransitionResult:
    _require_status(state, trigger, _TERMINAL_STATUSES)
    previous = state.status
    timestamp = now_iso()
    next_state = state.model_copy(
        update={
            "status": ReviewStatus.RUNNING,
            "stop_requested": False,
            "last_error": None,
            "finished_at": None,
            "started_at": state.started_at or timestamp,
            "updated_at": timestamp,
        }
    )
    return _result(next_state, previous=previous, trigger=trigger, changed=True)


def _request_stop(state: ReviewState, trigger: ReviewTrigger) -> ReviewTransitionResult:
    previous = state.status
    updates: dict[str, object] = {"stop_requested": True}
    changed = not state.stop_requested
    if state.status in ACTIVE_REVIEW_STATUSES:
        updates["status"] = ReviewStatus.STOPPING
        updates["updated_at"] = now_iso()
        changed = changed or previous != ReviewStatus.STOPPING
    next_state = state.model_copy(update=updates)
    return _result(next_state, previous=previous, trigger=trigger, changed=changed)


def _recover_interrupted(
    state: ReviewState, trigger: ReviewTrigger
) -> ReviewTransitionResult:
    _require_status(state, trigger, ACTIVE_REVIEW_STATUSES)
    previous = state.status
    next_state = state.model_copy(
        update={
            "status": ReviewStatus.INTERRUPTED,
            "last_error": trigger.error_message or "Recovered from restart",
            "stop_requested": False,
            "updated_at": now_iso(),
        }
    )
    return _result(next_state, previous=previous, trigger=trigger, changed=True)


def _mark_failed(state: ReviewState, trigger: ReviewTrigger) -> ReviewTransitionResult:
    _require_status(state, trigger, ACTIVE_REVIEW_STATUSES)
    previous = state.status
    timestamp = now_iso()
    next_state = state.model_copy(
        update={
            "status": ReviewStatus.FAILED,
            "last_error": trigger.error_message or trigger.reason,
            "finished_at": timestamp,
            "updated_at": timestamp,
        }
    )
    return _result(next_state, previous=previous, trigger=trigger, changed=True)


def _mark_stopped(state: ReviewState, trigger: ReviewTrigger) -> ReviewTransitionResult:
    _require_status(state, trigger, ACTIVE_REVIEW_STATUSES)
    previous = state.status
    timestamp = now_iso()
    next_state = state.model_copy(
        update={
            "status": ReviewStatus.STOPPED,
            "finished_at": timestamp,
            "updated_at": timestamp,
        }
    )
    return _result(next_state, previous=previous, trigger=trigger, changed=True)


def _mark_completed(
    state: ReviewState, trigger: ReviewTrigger
) -> ReviewTransitionResult:
    _require_status(state, trigger, ACTIVE_REVIEW_STATUSES)
    previous = state.status
    timestamp = now_iso()
    next_state = state.model_copy(
        update={
            "status": ReviewStatus.COMPLETED,
            "scratchpad_bundle_path": trigger.scratchpad_bundle_path,
            "finished_at": timestamp,
            "updated_at": timestamp,
        }
    )
    return _result(next_state, previous=previous, trigger=trigger, changed=True)


def _reset(state: ReviewState, trigger: ReviewTrigger) -> ReviewTransitionResult:
    _require_status(state, trigger, _TERMINAL_STATUSES)
    previous = state.status
    next_state = ReviewState()
    return _result(
        next_state,
        previous=previous,
        trigger=trigger,
        changed=previous != ReviewStatus.IDLE or state != next_state,
    )
