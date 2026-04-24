"""PMA automation domain package.

This package is the canonical home for PMA automation policy types and
domain events.  Adapters and runtime modules should import canonical types
from here rather than redefining them locally.

Layer boundaries
----------------
- **Domain models** (this package): pure data types, normalization, and
  serialization.  No I/O, no filesystem access, no SQLite.
- **Domain policy** (``publish_policy``): owns duplicate/noop suppression
  decisions, notice classification, and publish message construction.
  Adapters and surfaces delegate to these functions instead of implementing
  ad hoc string checks or open-coded message rules.
- **Delivery lifecycle** (``delivery_lifecycle``): owns delivery-attempt
  state transitions, retry policy, and outcome classification.  The ledger
  records domain reasoning, not adapter error strings.
- **Rebinding policy** (``rebinding_policy``): owns the decision about
  what to do when a binding changes after a dispatch decision is persisted.
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
    NOTICE_KIND_ESCALATION,
    NOTICE_KIND_NOOP,
    NOTICE_KIND_PROGRESS,
    NOTICE_KIND_TERMINAL_FOLLOWUP,
    PMA_AUTOMATION_VERSION,
    ROUTE_BOUND,
    ROUTE_EXPLICIT,
    ROUTE_PRIMARY_PMA,
    SOURCE_KIND_MANAGED_THREAD_COMPLETED,
    SUBSCRIPTION_STATE_ACTIVE,
    SUBSCRIPTION_STATE_CANCELLED,
    SUPPRESSED_REASON_DUPLICATE_NOOP,
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
from .delivery_lifecycle import (
    DELIVERY_LIFECYCLE_TERMINAL_STATES,
    DELIVERY_LIFECYCLE_TRANSITIONS,
    DeliveryAttemptOutcome,
    DeliveryLifecycleState,
    DeliveryRetryConfig,
    DeliveryTransition,
    advance_to_delivering,
    advance_to_dispatching,
    is_terminal_delivery_state,
    is_valid_delivery_transition,
    resolve_delivery_transition,
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
    PublishNoticeContext,
    PublishSuppressionDecision,
)
from .publish_policy import (
    build_publish_notice_message,
    classify_notice_kind,
    evaluate_publish_suppression,
    is_noop_duplicate_message,
)
from .rebinding_policy import (
    RebindingContext,
    RebindingDecision,
    RebindingResult,
    evaluate_rebinding_decision,
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
from .subscription_reducer import (
    ReduceTimerResult,
    ReduceTransitionResult,
    TimerFiredEvent,
    TransitionEvent,
    WakeupIntent,
    reduce_timer_fired,
    reduce_transition,
)

__all__ = [
    "DELIVERY_LIFECYCLE_TERMINAL_STATES",
    "DELIVERY_LIFECYCLE_TRANSITIONS",
    "DEFAULT_PMA_LANE_ID",
    "DEFAULT_WATCHDOG_IDLE_SECONDS",
    "DELIVERY_MODE_AUTO",
    "DELIVERY_MODE_BOUND",
    "DELIVERY_MODE_NONE",
    "DELIVERY_MODE_PRIMARY_PMA",
    "DELIVERY_MODE_SUPPRESSED",
    "NOTICE_KIND_ESCALATION",
    "NOTICE_KIND_NOOP",
    "NOTICE_KIND_PROGRESS",
    "NOTICE_KIND_TERMINAL_FOLLOWUP",
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
    "PublishNoticeContext",
    "PublishSuppressionDecision",
    "ReduceTimerResult",
    "ReduceTransitionResult",
    "RebindingContext",
    "RebindingDecision",
    "RebindingResult",
    "ROUTE_BOUND",
    "ROUTE_EXPLICIT",
    "ROUTE_PRIMARY_PMA",
    "SOURCE_KIND_MANAGED_THREAD_COMPLETED",
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_CANCELLED",
    "SUPPRESSED_REASON_DUPLICATE_NOOP",
    "SURFACE_KIND_DISCORD",
    "SURFACE_KIND_TELEGRAM",
    "TIMER_STATE_CANCELLED",
    "TIMER_STATE_FIRED",
    "TIMER_STATE_PENDING",
    "TIMER_TYPE_ONE_SHOT",
    "TIMER_TYPE_WATCHDOG",
    "WAKEUP_STATE_DISPATCHED",
    "WAKEUP_STATE_PENDING",
    "TimerFiredEvent",
    "TransitionEvent",
    "WakeupIntent",
    "DeliveryAttemptOutcome",
    "DeliveryLifecycleState",
    "DeliveryRetryConfig",
    "DeliveryTransition",
    "advance_to_delivering",
    "advance_to_dispatching",
    "build_publish_notice_message",
    "classify_notice_kind",
    "evaluate_publish_suppression",
    "evaluate_rebinding_decision",
    "is_noop_duplicate_message",
    "is_terminal_delivery_state",
    "is_valid_delivery_transition",
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
    "reduce_timer_fired",
    "reduce_transition",
    "resolve_delivery_transition",
]
