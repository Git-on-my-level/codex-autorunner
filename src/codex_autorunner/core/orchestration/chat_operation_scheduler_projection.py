"""Domain-owned projection from Discord scheduler/execution states to shared
chat-operation lifecycle state.

This module is the sole authority for mapping Discord transport-local
scheduler states and execution statuses to the shared
``ChatOperationState`` machine.  The Discord adapter delegates to these
projections rather than maintaining inline if/elif chains.

Architectural invariants:
- All projections are pure functions (no I/O, no side effects).
- The mapping tables are exhaustive; unmapped values return ``None``.
- Terminal outcome derivation is co-located with the state projection so
  the adapter does not need to re-derive it from the mapped state.
"""

from __future__ import annotations

from typing import Optional

from .chat_operation_state import ChatOperationState

_DISCORD_SCHEDULER_STATE_MAP: dict[str, ChatOperationState] = {
    "received": ChatOperationState.RECEIVED,
    "dispatch_ready": ChatOperationState.RECEIVED,
    "dispatch_ack_pending": ChatOperationState.RECEIVED,
    "queue_wait_ack_pending": ChatOperationState.RECEIVED,
    "acknowledged": ChatOperationState.ACKNOWLEDGED,
    "scheduled": ChatOperationState.QUEUED,
    "waiting_on_resources": ChatOperationState.QUEUED,
    "recovery_scheduled": ChatOperationState.QUEUED,
    "executing": ChatOperationState.RUNNING,
    "delivery_pending": ChatOperationState.DELIVERING,
    "delivery_replaying": ChatOperationState.DELIVERING,
    "completed": ChatOperationState.COMPLETED,
    "abandoned": ChatOperationState.FAILED,
    "delivery_expired": ChatOperationState.CANCELLED,
}

_DISCORD_SCHEDULER_STATE_INTERRUPT_KEYWORD = "interrupt"

_DISCORD_EXECUTION_STATUS_TO_SHARED_STATE: dict[
    str, tuple[ChatOperationState, Optional[str]]
] = {
    "completed": (ChatOperationState.COMPLETED, None),
    "cancelled": (ChatOperationState.CANCELLED, None),
    "timeout": (ChatOperationState.FAILED, "timeout"),
    "failed": (ChatOperationState.FAILED, None),
    "running": (ChatOperationState.RUNNING, None),
    "acknowledged": (ChatOperationState.ACKNOWLEDGED, None),
    "received": (ChatOperationState.RECEIVED, None),
}

_DISCORD_SCHEDULER_TERMINAL_OUTCOMES: dict[str, str] = {
    "abandoned": "abandoned",
    "delivery_expired": "expired",
}

_DISCORD_DELIVERY_PENDING_STATES = frozenset({"delivery_pending", "delivery_replaying"})

_DISCORD_DELIVERY_PENDING_CURSOR_STATES = frozenset({"pending", "failed"})


def discord_scheduler_state_to_chat_operation_state(
    scheduler_state: str,
) -> Optional[ChatOperationState]:
    """Map a Discord scheduler state to the shared chat-operation state.

    Returns ``None`` for unrecognized states (including interrupt variants
    that are matched by keyword rather than exact value).
    """
    normalized = str(scheduler_state or "").strip().lower()
    mapped = _DISCORD_SCHEDULER_STATE_MAP.get(normalized)
    if mapped is not None:
        return mapped
    if _DISCORD_SCHEDULER_STATE_INTERRUPT_KEYWORD in normalized:
        return ChatOperationState.INTERRUPTING
    return None


def discord_execution_status_to_chat_operation_state(
    execution_status: str,
    *,
    has_pending_delivery: bool = False,
) -> tuple[Optional[ChatOperationState], Optional[str]]:
    """Map a Discord execution status to shared state + terminal outcome.

    When ``execution_status`` is ``"completed"`` but a pending delivery
    cursor exists, the shared state is ``DELIVERING`` instead of
    ``COMPLETED``.

    Returns ``(shared_state, terminal_outcome)`` where ``terminal_outcome``
    is a non-None string only for terminal states that carry extra semantics
    (e.g. ``"timeout"``).
    """
    normalized = str(execution_status or "").strip().lower()
    entry = _DISCORD_EXECUTION_STATUS_TO_SHARED_STATE.get(normalized)
    if entry is None:
        return (None, None)
    shared_state, terminal_outcome = entry
    if normalized == "completed" and has_pending_delivery:
        shared_state = ChatOperationState.DELIVERING
    return (shared_state, terminal_outcome)


def discord_scheduler_terminal_outcome(
    scheduler_state: str,
) -> Optional[str]:
    """Derive the shared terminal-outcome string for a Discord scheduler state.

    Returns ``None`` for non-terminal scheduler states.
    """
    normalized = str(scheduler_state or "").strip().lower()
    return _DISCORD_SCHEDULER_TERMINAL_OUTCOMES.get(normalized)


def discord_interaction_has_pending_delivery(
    *,
    scheduler_state: str,
    delivery_cursor_state: Optional[str] = None,
) -> bool:
    """Determine whether a Discord interaction has pending delivery.

    An interaction has pending delivery when either:
    - Its scheduler state is a delivery-pending variant, or
    - Its delivery cursor reports ``"pending"`` or ``"failed"``.
    """
    normalized_scheduler = str(scheduler_state or "").strip().lower()
    if normalized_scheduler in _DISCORD_DELIVERY_PENDING_STATES:
        return True
    if delivery_cursor_state is not None:
        normalized_cursor = str(delivery_cursor_state).strip().lower()
        if normalized_cursor in _DISCORD_DELIVERY_PENDING_CURSOR_STATES:
            return True
    return False


__all__ = [
    "discord_execution_status_to_chat_operation_state",
    "discord_interaction_has_pending_delivery",
    "discord_scheduler_state_to_chat_operation_state",
    "discord_scheduler_terminal_outcome",
]
