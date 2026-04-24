"""Domain-owned delivery lifecycle transitions and retry policy.

This module unifies delivery-attempt state transitions, retry behavior, and
outcome classification under domain language.  Adapters translate transport
results into domain outcomes; the domain decides the next state.  The ledger
records domain reasoning, not adapter error strings.

Layer contract:
- **This module**: pure functions, no I/O, no SQLite.
- **Adapters**: call ``resolve_delivery_transition`` after each attempt; persist
  the returned ``DeliveryTransition`` via the engine.
- **Engine**: validates transitions against ``DELIVERY_LIFECYCLE_TRANSITIONS``
  before writing to the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any, Optional


class DeliveryLifecycleState(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    DELIVERING = "delivering"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    ABANDONED = "abandoned"


DELIVERY_LIFECYCLE_TERMINAL_STATES = frozenset(
    {
        DeliveryLifecycleState.SUCCEEDED,
        DeliveryLifecycleState.FAILED,
        DeliveryLifecycleState.SUPPRESSED,
        DeliveryLifecycleState.ABANDONED,
    }
)

DELIVERY_LIFECYCLE_TRANSITIONS: dict[
    DeliveryLifecycleState, frozenset[DeliveryLifecycleState]
] = {
    DeliveryLifecycleState.PENDING: frozenset(
        {
            DeliveryLifecycleState.DISPATCHED,
            DeliveryLifecycleState.DELIVERING,
            DeliveryLifecycleState.SUPPRESSED,
            DeliveryLifecycleState.ABANDONED,
        }
    ),
    DeliveryLifecycleState.DISPATCHED: frozenset(
        {
            DeliveryLifecycleState.DELIVERING,
            DeliveryLifecycleState.RETRY_SCHEDULED,
            DeliveryLifecycleState.SUPPRESSED,
            DeliveryLifecycleState.ABANDONED,
        }
    ),
    DeliveryLifecycleState.DELIVERING: frozenset(
        {
            DeliveryLifecycleState.SUCCEEDED,
            DeliveryLifecycleState.RETRY_SCHEDULED,
            DeliveryLifecycleState.FAILED,
            DeliveryLifecycleState.ABANDONED,
        }
    ),
    DeliveryLifecycleState.RETRY_SCHEDULED: frozenset(
        {
            DeliveryLifecycleState.DISPATCHED,
            DeliveryLifecycleState.DELIVERING,
            DeliveryLifecycleState.SUPPRESSED,
            DeliveryLifecycleState.ABANDONED,
        }
    ),
    DeliveryLifecycleState.SUCCEEDED: frozenset(),
    DeliveryLifecycleState.FAILED: frozenset(),
    DeliveryLifecycleState.SUPPRESSED: frozenset(),
    DeliveryLifecycleState.ABANDONED: frozenset(),
}


class DeliveryAttemptOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_PERMANENT = "failed_permanent"
    SUPPRESSED_DUPLICATE = "suppressed_duplicate"
    SUPPRESSED_NOOP = "suppressed_noop"
    ABANDONED_BY_POLICY = "abandoned_by_policy"


@dataclass(frozen=True)
class DeliveryRetryConfig:
    max_attempts: int = 5
    backoff_base: timedelta = field(default_factory=lambda: timedelta(minutes=1))
    backoff_multiplier: float = 2.0
    max_backoff: timedelta = field(default_factory=lambda: timedelta(minutes=30))


@dataclass(frozen=True)
class DeliveryTransition:
    next_state: DeliveryLifecycleState
    outcome: DeliveryAttemptOutcome
    domain_reason: str
    attempt_number: int
    retry_at: Optional[str] = None
    domain_metadata: dict[str, Any] = field(default_factory=dict, hash=False)


def is_valid_delivery_transition(
    current: DeliveryLifecycleState,
    target: DeliveryLifecycleState,
) -> bool:
    if current == target:
        return True
    return target in DELIVERY_LIFECYCLE_TRANSITIONS.get(current, frozenset())


def is_terminal_delivery_state(state: DeliveryLifecycleState) -> bool:
    return state in DELIVERY_LIFECYCLE_TERMINAL_STATES


def resolve_delivery_transition(
    *,
    current_state: DeliveryLifecycleState,
    outcome: DeliveryAttemptOutcome,
    attempt_number: int,
    retry_config: DeliveryRetryConfig,
    domain_reason: str = "",
    domain_metadata: Optional[dict[str, Any]] = None,
) -> DeliveryTransition:
    if is_terminal_delivery_state(current_state):
        return DeliveryTransition(
            next_state=current_state,
            outcome=outcome,
            domain_reason=f"already_terminal:{current_state.value}",
            attempt_number=attempt_number,
            domain_metadata=domain_metadata or {},
        )

    metadata = dict(domain_metadata or {})

    if outcome == DeliveryAttemptOutcome.SUCCEEDED:
        return DeliveryTransition(
            next_state=DeliveryLifecycleState.SUCCEEDED,
            outcome=outcome,
            domain_reason=domain_reason or "delivery_succeeded",
            attempt_number=attempt_number,
            domain_metadata=metadata,
        )

    if outcome == DeliveryAttemptOutcome.SUPPRESSED_DUPLICATE:
        return DeliveryTransition(
            next_state=DeliveryLifecycleState.SUPPRESSED,
            outcome=outcome,
            domain_reason=domain_reason or "duplicate_suppressed",
            attempt_number=attempt_number,
            domain_metadata=metadata,
        )

    if outcome == DeliveryAttemptOutcome.SUPPRESSED_NOOP:
        return DeliveryTransition(
            next_state=DeliveryLifecycleState.SUPPRESSED,
            outcome=outcome,
            domain_reason=domain_reason or "noop_suppressed",
            attempt_number=attempt_number,
            domain_metadata=metadata,
        )

    if outcome == DeliveryAttemptOutcome.ABANDONED_BY_POLICY:
        return DeliveryTransition(
            next_state=DeliveryLifecycleState.ABANDONED,
            outcome=outcome,
            domain_reason=domain_reason or "abandoned_by_policy",
            attempt_number=attempt_number,
            domain_metadata=metadata,
        )

    if outcome == DeliveryAttemptOutcome.FAILED_PERMANENT:
        return DeliveryTransition(
            next_state=DeliveryLifecycleState.FAILED,
            outcome=outcome,
            domain_reason=domain_reason or "permanent_failure",
            attempt_number=attempt_number,
            domain_metadata=metadata,
        )

    if outcome == DeliveryAttemptOutcome.FAILED_TRANSIENT:
        if attempt_number >= retry_config.max_attempts:
            return DeliveryTransition(
                next_state=DeliveryLifecycleState.FAILED,
                outcome=DeliveryAttemptOutcome.FAILED_PERMANENT,
                domain_reason=domain_reason
                or f"transient_exhausted_after_{attempt_number}_attempts",
                attempt_number=attempt_number,
                domain_metadata=metadata,
            )
        backoff = _compute_backoff(
            attempt_number=attempt_number,
            retry_config=retry_config,
        )
        return DeliveryTransition(
            next_state=DeliveryLifecycleState.RETRY_SCHEDULED,
            outcome=outcome,
            domain_reason=domain_reason or "transient_failure_retry_scheduled",
            attempt_number=attempt_number,
            retry_at=backoff,
            domain_metadata=metadata,
        )

    return DeliveryTransition(
        next_state=current_state,
        outcome=outcome,
        domain_reason=domain_reason or "unhandled_outcome",
        attempt_number=attempt_number,
        domain_metadata=metadata,
    )


def advance_to_dispatching(
    current_state: DeliveryLifecycleState,
    *,
    attempt_number: int,
) -> DeliveryTransition:
    if current_state == DeliveryLifecycleState.PENDING:
        return DeliveryTransition(
            next_state=DeliveryLifecycleState.DISPATCHED,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            domain_reason="dispatch_initiated_from_pending",
            attempt_number=attempt_number,
        )
    if current_state == DeliveryLifecycleState.RETRY_SCHEDULED:
        return DeliveryTransition(
            next_state=DeliveryLifecycleState.DISPATCHED,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            domain_reason="dispatch_initiated_from_retry",
            attempt_number=attempt_number,
        )
    return DeliveryTransition(
        next_state=current_state,
        outcome=DeliveryAttemptOutcome.SUCCEEDED,
        domain_reason=f"dispatch_skipped_in_{current_state.value}",
        attempt_number=attempt_number,
    )


def advance_to_delivering(
    current_state: DeliveryLifecycleState,
    *,
    attempt_number: int,
) -> DeliveryTransition:
    if current_state in (
        DeliveryLifecycleState.DISPATCHED,
        DeliveryLifecycleState.PENDING,
        DeliveryLifecycleState.RETRY_SCHEDULED,
    ):
        return DeliveryTransition(
            next_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            domain_reason=f"adapter_delivery_started_from_{current_state.value}",
            attempt_number=attempt_number,
        )
    return DeliveryTransition(
        next_state=current_state,
        outcome=DeliveryAttemptOutcome.SUCCEEDED,
        domain_reason=f"delivery_start_skipped_in_{current_state.value}",
        attempt_number=attempt_number,
    )


def _compute_backoff(
    *,
    attempt_number: int,
    retry_config: DeliveryRetryConfig,
) -> str:
    from datetime import datetime, timezone

    exponent = max(0, attempt_number - 1)
    delay_seconds = retry_config.backoff_base.total_seconds() * (
        retry_config.backoff_multiplier**exponent
    )
    delay_seconds = min(delay_seconds, retry_config.max_backoff.total_seconds())
    delay_seconds = max(0.0, delay_seconds)
    base = datetime.now(timezone.utc)
    from datetime import timedelta as td

    return (base + td(seconds=delay_seconds)).isoformat()


__all__ = [
    "DELIVERY_LIFECYCLE_TERMINAL_STATES",
    "DELIVERY_LIFECYCLE_TRANSITIONS",
    "DeliveryAttemptOutcome",
    "DeliveryLifecycleState",
    "DeliveryRetryConfig",
    "DeliveryTransition",
    "advance_to_delivering",
    "advance_to_dispatching",
    "is_terminal_delivery_state",
    "is_valid_delivery_transition",
    "resolve_delivery_transition",
]
