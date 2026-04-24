"""PMA automation domain package.

This package is the canonical home for PMA automation policy types and
domain events.  Adapters and runtime modules should import canonical types
from here rather than redefining them locally.

Layer boundaries
----------------
- **Domain models** (this package): pure data types, normalization, and
  serialization.  No I/O, no filesystem access, no SQLite.
- **Adapters**: persistence (SQLite, JSON), transport (Discord, Telegram),
  and surface modules.  They consume domain types and execute side effects.
- **Surfaces**: CLI, web routes, chat commands.  They call adapters.

Every new PMA routing or wakeup policy decision should live in this package.
"""

from .constants import (
    DEFAULT_PMA_LANE_ID,
    DEFAULT_WATCHDOG_IDLE_SECONDS,
    DELIVERY_MODE_AUTO,
    DELIVERY_MODE_BOUND,
    DELIVERY_MODE_NONE,
    DELIVERY_MODE_PRIMARY_PMA,
    DELIVERY_MODE_SUPPRESSED,
    PMA_AUTOMATION_VERSION,
    ROUTE_BOUND,
    ROUTE_EXPLICIT,
    ROUTE_PRIMARY_PMA,
    SUBSCRIPTION_STATE_ACTIVE,
    SUBSCRIPTION_STATE_CANCELLED,
    SURFACE_KIND_DISCORD,
    SURFACE_KIND_TELEGRAM,
    TIMER_STATE_CANCELLED,
    TIMER_STATE_FIRED,
    TIMER_STATE_PENDING,
    TIMER_TYPE_ONE_SHOT,
    TIMER_TYPE_WATCHDOG,
    WAKEUP_STATE_DISPATCHED,
    WAKEUP_STATE_PENDING,
)
from .events import (
    PmaDomainEvent,
    PmaDomainEventType,
)
from .models import (
    PmaDeliveryAttempt,
    PmaDeliveryIntent,
    PmaDeliveryState,
    PmaDeliveryTarget,
    PmaDispatchAttempt,
    PmaDispatchDecision,
    PmaOriginContext,
    PmaSubscription,
    PmaTimer,
    PmaWakeup,
)
from .serialization import (
    normalize_pma_delivery_attempt,
    normalize_pma_delivery_intent,
    normalize_pma_delivery_state,
    normalize_pma_delivery_target,
    normalize_pma_dispatch_decision,
    normalize_pma_domain_event,
    normalize_pma_origin_context,
    normalize_pma_subscription,
    normalize_pma_timer,
    normalize_pma_wakeup,
    pma_dispatch_decision_to_dict,
    pma_origin_context_to_dict,
    pma_subscription_to_dict,
    pma_timer_to_dict,
    pma_wakeup_to_dict,
)

__all__ = [
    "DEFAULT_PMA_LANE_ID",
    "DEFAULT_WATCHDOG_IDLE_SECONDS",
    "DELIVERY_MODE_AUTO",
    "DELIVERY_MODE_BOUND",
    "DELIVERY_MODE_NONE",
    "DELIVERY_MODE_PRIMARY_PMA",
    "DELIVERY_MODE_SUPPRESSED",
    "PMA_AUTOMATION_VERSION",
    "PmaDeliveryAttempt",
    "PmaDeliveryIntent",
    "PmaDeliveryState",
    "PmaDeliveryTarget",
    "PmaDispatchAttempt",
    "PmaDispatchDecision",
    "PmaDomainEvent",
    "PmaDomainEventType",
    "PmaOriginContext",
    "PmaSubscription",
    "PmaTimer",
    "PmaWakeup",
    "ROUTE_BOUND",
    "ROUTE_EXPLICIT",
    "ROUTE_PRIMARY_PMA",
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_CANCELLED",
    "SURFACE_KIND_DISCORD",
    "SURFACE_KIND_TELEGRAM",
    "TIMER_STATE_CANCELLED",
    "TIMER_STATE_FIRED",
    "TIMER_STATE_PENDING",
    "TIMER_TYPE_ONE_SHOT",
    "TIMER_TYPE_WATCHDOG",
    "WAKEUP_STATE_DISPATCHED",
    "WAKEUP_STATE_PENDING",
    "normalize_pma_delivery_attempt",
    "normalize_pma_delivery_intent",
    "normalize_pma_delivery_state",
    "normalize_pma_delivery_target",
    "normalize_pma_dispatch_decision",
    "normalize_pma_domain_event",
    "normalize_pma_origin_context",
    "normalize_pma_subscription",
    "normalize_pma_timer",
    "normalize_pma_wakeup",
    "pma_dispatch_decision_to_dict",
    "pma_origin_context_to_dict",
    "pma_subscription_to_dict",
    "pma_timer_to_dict",
    "pma_wakeup_to_dict",
]
