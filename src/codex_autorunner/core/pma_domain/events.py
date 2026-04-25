from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class PmaDomainEventType(str, Enum):
    SUBSCRIPTION_CREATED = "subscription_created"
    SUBSCRIPTION_CANCELLED = "subscription_cancelled"
    THREAD_TRANSITIONED = "thread_transitioned"
    BINDING_OBSERVED_CHANGED = "binding_observed_changed"
    PUBLISH_REQUESTED = "publish_requested"
    PUBLISH_ATTEMPT_SUCCEEDED = "publish_attempt_succeeded"
    PUBLISH_ATTEMPT_FAILED = "publish_attempt_failed"
    DELIVERY_SUPPRESSED = "delivery_suppressed"
    TIMER_FIRED = "timer_fired"
    LIFECYCLE_TRANSITION_OCCURRED = "lifecycle_transition_occurred"
    WAKEUP_CREATED = "wakeup_created"
    WAKEUP_DISPATCHED = "wakeup_dispatched"


@dataclass(frozen=True)
class PmaDomainEvent:
    event_type: PmaDomainEventType
    event_id: str
    timestamp: str
    payload: dict[str, Any]
    correlation_id: Optional[str] = None
