"""Shared lifecycle policy for Discord interaction ledger states.

Discord owns transport-local durability for interaction acknowledgement,
delivery cursors, and replay metadata.  This module owns the lifecycle policy
for the two string columns persisted in that ledger so adapter code can ask a
single contract whether a transition is legal before writing SQLite rows.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .chat_operation_state import ChatOperationState


class DiscordInteractionSchedulerState(str, Enum):
    RECEIVED = "received"
    DISPATCH_READY = "dispatch_ready"
    DISPATCH_ACK_PENDING = "dispatch_ack_pending"
    QUEUE_WAIT_ACK_PENDING = "queue_wait_ack_pending"
    ACKNOWLEDGED = "acknowledged"
    SCHEDULED = "scheduled"
    WAITING_ON_RESOURCES = "waiting_on_resources"
    EXECUTING = "executing"
    DELIVERY_PENDING = "delivery_pending"
    DELIVERY_REPLAYING = "delivery_replaying"
    DELIVERY_EXPIRED = "delivery_expired"
    RECOVERY_SCHEDULED = "recovery_scheduled"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class DiscordInteractionExecutionStatus(str, Enum):
    RECEIVED = "received"
    ACKNOWLEDGED = "acknowledged"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


DISCORD_INTERACTION_TERMINAL_SCHEDULER_STATES = frozenset(
    {
        DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
        DiscordInteractionSchedulerState.ABANDONED,
    }
)

DISCORD_INTERACTION_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        DiscordInteractionExecutionStatus.COMPLETED,
        DiscordInteractionExecutionStatus.FAILED,
        DiscordInteractionExecutionStatus.TIMEOUT,
        DiscordInteractionExecutionStatus.CANCELLED,
    }
)

DISCORD_INTERACTION_SCHEDULER_TRANSITIONS: dict[
    DiscordInteractionSchedulerState, frozenset[DiscordInteractionSchedulerState]
] = {
    DiscordInteractionSchedulerState.RECEIVED: frozenset(
        {
            DiscordInteractionSchedulerState.DISPATCH_READY,
            DiscordInteractionSchedulerState.DISPATCH_ACK_PENDING,
            DiscordInteractionSchedulerState.QUEUE_WAIT_ACK_PENDING,
            DiscordInteractionSchedulerState.ACKNOWLEDGED,
            DiscordInteractionSchedulerState.SCHEDULED,
            DiscordInteractionSchedulerState.WAITING_ON_RESOURCES,
            DiscordInteractionSchedulerState.EXECUTING,
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
            DiscordInteractionSchedulerState.COMPLETED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.DISPATCH_READY: frozenset(
        {
            DiscordInteractionSchedulerState.DISPATCH_ACK_PENDING,
            DiscordInteractionSchedulerState.ACKNOWLEDGED,
            DiscordInteractionSchedulerState.SCHEDULED,
            DiscordInteractionSchedulerState.WAITING_ON_RESOURCES,
            DiscordInteractionSchedulerState.EXECUTING,
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
            DiscordInteractionSchedulerState.COMPLETED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.DISPATCH_ACK_PENDING: frozenset(
        {
            DiscordInteractionSchedulerState.ACKNOWLEDGED,
            DiscordInteractionSchedulerState.SCHEDULED,
            DiscordInteractionSchedulerState.WAITING_ON_RESOURCES,
            DiscordInteractionSchedulerState.EXECUTING,
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
            DiscordInteractionSchedulerState.COMPLETED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.QUEUE_WAIT_ACK_PENDING: frozenset(
        {
            DiscordInteractionSchedulerState.ACKNOWLEDGED,
            DiscordInteractionSchedulerState.SCHEDULED,
            DiscordInteractionSchedulerState.WAITING_ON_RESOURCES,
            DiscordInteractionSchedulerState.EXECUTING,
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
            DiscordInteractionSchedulerState.COMPLETED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.ACKNOWLEDGED: frozenset(
        {
            DiscordInteractionSchedulerState.SCHEDULED,
            DiscordInteractionSchedulerState.WAITING_ON_RESOURCES,
            DiscordInteractionSchedulerState.EXECUTING,
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
            DiscordInteractionSchedulerState.RECOVERY_SCHEDULED,
            DiscordInteractionSchedulerState.COMPLETED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.SCHEDULED: frozenset(
        {
            DiscordInteractionSchedulerState.WAITING_ON_RESOURCES,
            DiscordInteractionSchedulerState.EXECUTING,
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
            DiscordInteractionSchedulerState.RECOVERY_SCHEDULED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.WAITING_ON_RESOURCES: frozenset(
        {
            DiscordInteractionSchedulerState.SCHEDULED,
            DiscordInteractionSchedulerState.EXECUTING,
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
            DiscordInteractionSchedulerState.RECOVERY_SCHEDULED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.EXECUTING: frozenset(
        {
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
            DiscordInteractionSchedulerState.RECOVERY_SCHEDULED,
            DiscordInteractionSchedulerState.COMPLETED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.DELIVERY_PENDING: frozenset(
        {
            DiscordInteractionSchedulerState.SCHEDULED,
            DiscordInteractionSchedulerState.EXECUTING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
            DiscordInteractionSchedulerState.RECOVERY_SCHEDULED,
            DiscordInteractionSchedulerState.COMPLETED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.DELIVERY_REPLAYING: frozenset(
        {
            DiscordInteractionSchedulerState.SCHEDULED,
            DiscordInteractionSchedulerState.EXECUTING,
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.RECOVERY_SCHEDULED,
            DiscordInteractionSchedulerState.COMPLETED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.RECOVERY_SCHEDULED: frozenset(
        {
            DiscordInteractionSchedulerState.SCHEDULED,
            DiscordInteractionSchedulerState.WAITING_ON_RESOURCES,
            DiscordInteractionSchedulerState.EXECUTING,
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
            DiscordInteractionSchedulerState.COMPLETED,
            DiscordInteractionSchedulerState.DELIVERY_EXPIRED,
            DiscordInteractionSchedulerState.ABANDONED,
        }
    ),
    DiscordInteractionSchedulerState.COMPLETED: frozenset(
        {
            DiscordInteractionSchedulerState.DELIVERY_PENDING,
            DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
        }
    ),
    DiscordInteractionSchedulerState.DELIVERY_EXPIRED: frozenset(),
    DiscordInteractionSchedulerState.ABANDONED: frozenset(),
}

DISCORD_INTERACTION_EXECUTION_TRANSITIONS: dict[
    DiscordInteractionExecutionStatus, frozenset[DiscordInteractionExecutionStatus]
] = {
    DiscordInteractionExecutionStatus.RECEIVED: frozenset(
        {
            DiscordInteractionExecutionStatus.ACKNOWLEDGED,
            DiscordInteractionExecutionStatus.RUNNING,
            DiscordInteractionExecutionStatus.COMPLETED,
            DiscordInteractionExecutionStatus.FAILED,
            DiscordInteractionExecutionStatus.TIMEOUT,
            DiscordInteractionExecutionStatus.CANCELLED,
        }
    ),
    DiscordInteractionExecutionStatus.ACKNOWLEDGED: frozenset(
        {
            DiscordInteractionExecutionStatus.RUNNING,
            DiscordInteractionExecutionStatus.COMPLETED,
            DiscordInteractionExecutionStatus.FAILED,
            DiscordInteractionExecutionStatus.TIMEOUT,
            DiscordInteractionExecutionStatus.CANCELLED,
        }
    ),
    DiscordInteractionExecutionStatus.RUNNING: frozenset(
        {
            DiscordInteractionExecutionStatus.COMPLETED,
            DiscordInteractionExecutionStatus.FAILED,
            DiscordInteractionExecutionStatus.TIMEOUT,
            DiscordInteractionExecutionStatus.CANCELLED,
        }
    ),
    DiscordInteractionExecutionStatus.COMPLETED: frozenset(),
    DiscordInteractionExecutionStatus.FAILED: frozenset(),
    DiscordInteractionExecutionStatus.TIMEOUT: frozenset(),
    DiscordInteractionExecutionStatus.CANCELLED: frozenset(),
}

_DISCORD_SCHEDULER_STATE_MAP: dict[
    DiscordInteractionSchedulerState, ChatOperationState
] = {
    DiscordInteractionSchedulerState.RECEIVED: ChatOperationState.RECEIVED,
    DiscordInteractionSchedulerState.DISPATCH_READY: ChatOperationState.RECEIVED,
    DiscordInteractionSchedulerState.DISPATCH_ACK_PENDING: ChatOperationState.RECEIVED,
    DiscordInteractionSchedulerState.QUEUE_WAIT_ACK_PENDING: ChatOperationState.RECEIVED,
    DiscordInteractionSchedulerState.ACKNOWLEDGED: ChatOperationState.ACKNOWLEDGED,
    DiscordInteractionSchedulerState.SCHEDULED: ChatOperationState.QUEUED,
    DiscordInteractionSchedulerState.WAITING_ON_RESOURCES: ChatOperationState.QUEUED,
    DiscordInteractionSchedulerState.RECOVERY_SCHEDULED: ChatOperationState.QUEUED,
    DiscordInteractionSchedulerState.EXECUTING: ChatOperationState.RUNNING,
    DiscordInteractionSchedulerState.DELIVERY_PENDING: ChatOperationState.DELIVERING,
    DiscordInteractionSchedulerState.DELIVERY_REPLAYING: ChatOperationState.DELIVERING,
    DiscordInteractionSchedulerState.COMPLETED: ChatOperationState.COMPLETED,
    DiscordInteractionSchedulerState.ABANDONED: ChatOperationState.FAILED,
    DiscordInteractionSchedulerState.DELIVERY_EXPIRED: ChatOperationState.CANCELLED,
}

_DISCORD_EXECUTION_STATUS_TO_SHARED_STATE: dict[
    DiscordInteractionExecutionStatus, tuple[ChatOperationState, Optional[str]]
] = {
    DiscordInteractionExecutionStatus.COMPLETED: (ChatOperationState.COMPLETED, None),
    DiscordInteractionExecutionStatus.CANCELLED: (ChatOperationState.CANCELLED, None),
    DiscordInteractionExecutionStatus.TIMEOUT: (ChatOperationState.FAILED, "timeout"),
    DiscordInteractionExecutionStatus.FAILED: (ChatOperationState.FAILED, None),
    DiscordInteractionExecutionStatus.RUNNING: (ChatOperationState.RUNNING, None),
    DiscordInteractionExecutionStatus.ACKNOWLEDGED: (
        ChatOperationState.ACKNOWLEDGED,
        None,
    ),
    DiscordInteractionExecutionStatus.RECEIVED: (ChatOperationState.RECEIVED, None),
}

_DISCORD_SCHEDULER_TERMINAL_OUTCOMES: dict[DiscordInteractionSchedulerState, str] = {
    DiscordInteractionSchedulerState.ABANDONED: "abandoned",
    DiscordInteractionSchedulerState.DELIVERY_EXPIRED: "expired",
}

_DISCORD_DELIVERY_PENDING_STATES = frozenset(
    {
        DiscordInteractionSchedulerState.DELIVERY_PENDING,
        DiscordInteractionSchedulerState.DELIVERY_REPLAYING,
    }
)

_DISCORD_DELIVERY_PENDING_CURSOR_STATES = frozenset({"pending", "failed"})


def normalize_discord_interaction_scheduler_state(
    value: DiscordInteractionSchedulerState | str,
) -> DiscordInteractionSchedulerState:
    if isinstance(value, DiscordInteractionSchedulerState):
        return value
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("interaction scheduler state must be a non-empty string")
    try:
        return DiscordInteractionSchedulerState(normalized)
    except ValueError:
        raise ValueError(f"unknown interaction scheduler state: {normalized}") from None


def normalize_discord_interaction_execution_status(
    value: DiscordInteractionExecutionStatus | str,
) -> DiscordInteractionExecutionStatus:
    if isinstance(value, DiscordInteractionExecutionStatus):
        return value
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("interaction execution status must be a non-empty string")
    try:
        return DiscordInteractionExecutionStatus(normalized)
    except ValueError:
        raise ValueError(
            f"unknown interaction execution status: {normalized}"
        ) from None


def is_discord_interaction_scheduler_terminal(
    state: DiscordInteractionSchedulerState | str,
) -> bool:
    normalized = normalize_discord_interaction_scheduler_state(state)
    return normalized in DISCORD_INTERACTION_TERMINAL_SCHEDULER_STATES


def is_discord_interaction_execution_terminal(
    status: DiscordInteractionExecutionStatus | str,
) -> bool:
    normalized = normalize_discord_interaction_execution_status(status)
    return normalized in DISCORD_INTERACTION_TERMINAL_EXECUTION_STATUSES


def is_valid_discord_interaction_scheduler_transition(
    from_state: DiscordInteractionSchedulerState | str,
    to_state: DiscordInteractionSchedulerState | str,
) -> bool:
    normalized_from = normalize_discord_interaction_scheduler_state(from_state)
    normalized_to = normalize_discord_interaction_scheduler_state(to_state)
    if normalized_from == normalized_to:
        return True
    return normalized_to in DISCORD_INTERACTION_SCHEDULER_TRANSITIONS[normalized_from]


def is_valid_discord_interaction_execution_transition(
    from_status: DiscordInteractionExecutionStatus | str,
    to_status: DiscordInteractionExecutionStatus | str,
) -> bool:
    normalized_from = normalize_discord_interaction_execution_status(from_status)
    normalized_to = normalize_discord_interaction_execution_status(to_status)
    if normalized_from == normalized_to:
        return True
    return normalized_to in DISCORD_INTERACTION_EXECUTION_TRANSITIONS[normalized_from]


def validate_discord_interaction_scheduler_transition(
    from_state: DiscordInteractionSchedulerState | str,
    to_state: DiscordInteractionSchedulerState | str,
) -> DiscordInteractionSchedulerState:
    normalized_from = normalize_discord_interaction_scheduler_state(from_state)
    normalized_to = normalize_discord_interaction_scheduler_state(to_state)
    if not is_valid_discord_interaction_scheduler_transition(
        normalized_from, normalized_to
    ):
        raise ValueError(
            "illegal interaction scheduler transition: "
            f"{normalized_from.value} -> {normalized_to.value}"
        )
    return normalized_to


def validate_discord_interaction_execution_transition(
    from_status: DiscordInteractionExecutionStatus | str,
    to_status: DiscordInteractionExecutionStatus | str,
) -> DiscordInteractionExecutionStatus:
    normalized_from = normalize_discord_interaction_execution_status(from_status)
    normalized_to = normalize_discord_interaction_execution_status(to_status)
    if not is_valid_discord_interaction_execution_transition(
        normalized_from, normalized_to
    ):
        raise ValueError(
            "illegal interaction execution transition: "
            f"{normalized_from.value} -> {normalized_to.value}"
        )
    return normalized_to


def discord_scheduler_state_to_chat_operation_state(
    scheduler_state: str,
) -> Optional[ChatOperationState]:
    normalized = str(scheduler_state or "").strip().lower()
    if "interrupt" in normalized:
        return ChatOperationState.INTERRUPTING
    try:
        state = normalize_discord_interaction_scheduler_state(normalized)
    except ValueError:
        return None
    return _DISCORD_SCHEDULER_STATE_MAP[state]


def discord_execution_status_to_chat_operation_state(
    execution_status: str,
    *,
    has_pending_delivery: bool = False,
) -> tuple[Optional[ChatOperationState], Optional[str]]:
    try:
        status = normalize_discord_interaction_execution_status(execution_status)
    except ValueError:
        return (None, None)
    shared_state, terminal_outcome = _DISCORD_EXECUTION_STATUS_TO_SHARED_STATE[status]
    if status == DiscordInteractionExecutionStatus.COMPLETED and has_pending_delivery:
        shared_state = ChatOperationState.DELIVERING
    return (shared_state, terminal_outcome)


def discord_scheduler_terminal_outcome(
    scheduler_state: str,
) -> Optional[str]:
    try:
        state = normalize_discord_interaction_scheduler_state(scheduler_state)
    except ValueError:
        return None
    return _DISCORD_SCHEDULER_TERMINAL_OUTCOMES.get(state)


def discord_interaction_has_pending_delivery(
    *,
    scheduler_state: str,
    delivery_cursor_state: Optional[str] = None,
) -> bool:
    try:
        state = normalize_discord_interaction_scheduler_state(scheduler_state)
    except ValueError:
        return False
    if state in _DISCORD_DELIVERY_PENDING_STATES:
        return True
    if delivery_cursor_state is not None:
        normalized_cursor = str(delivery_cursor_state).strip().lower()
        if normalized_cursor in _DISCORD_DELIVERY_PENDING_CURSOR_STATES:
            return True
    return False


__all__ = [
    "DISCORD_INTERACTION_EXECUTION_TRANSITIONS",
    "DISCORD_INTERACTION_SCHEDULER_TRANSITIONS",
    "DISCORD_INTERACTION_TERMINAL_EXECUTION_STATUSES",
    "DISCORD_INTERACTION_TERMINAL_SCHEDULER_STATES",
    "DiscordInteractionExecutionStatus",
    "DiscordInteractionSchedulerState",
    "discord_execution_status_to_chat_operation_state",
    "discord_interaction_has_pending_delivery",
    "discord_scheduler_state_to_chat_operation_state",
    "discord_scheduler_terminal_outcome",
    "is_discord_interaction_execution_terminal",
    "is_discord_interaction_scheduler_terminal",
    "is_valid_discord_interaction_execution_transition",
    "is_valid_discord_interaction_scheduler_transition",
    "normalize_discord_interaction_execution_status",
    "normalize_discord_interaction_scheduler_state",
    "validate_discord_interaction_execution_transition",
    "validate_discord_interaction_scheduler_transition",
]
