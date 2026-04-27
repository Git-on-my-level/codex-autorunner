"""Domain-owned duplicate-ingress policy for shared chat operations.

This module decides how adapters should treat a second ingress event when a
durable chat operation already exists. The goal is to keep duplicate handling
aligned with the shared chat-operation state machine rather than letting each
adapter guess whether an in-flight operation should restart, stay suppressed, or
be treated as a terminal duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

from .chat_operation_state import (
    CHAT_OPERATION_TERMINAL_STATES,
    ChatOperationSnapshot,
    ChatOperationState,
)


class ChatOperationDuplicateAction(str, Enum):
    """Authoritative duplicate-ingress actions for one chat operation."""

    ACCEPT_FRESH = "accept_fresh"
    REJECT_TERMINAL = "reject_terminal"
    SUPPRESS_IN_FLIGHT = "suppress_in_flight"
    SUPPRESS_PENDING_DELIVERY = "suppress_pending_delivery"


@dataclass(frozen=True)
class ChatOperationDuplicateDecision:
    action: ChatOperationDuplicateAction
    reason: str
    previous_state: Optional[ChatOperationState]
    delivery_pending: bool
    rationale: Mapping[str, Any] = field(default_factory=dict)


def chat_operation_is_terminal_duplicate(snapshot: ChatOperationSnapshot) -> bool:
    """Return whether a snapshot represents a duplicate that should be rejected."""

    if snapshot.terminal_outcome in {"abandoned", "expired"}:
        return True
    return snapshot.state in CHAT_OPERATION_TERMINAL_STATES


def plan_chat_operation_duplicate(
    snapshot: Optional[ChatOperationSnapshot],
    *,
    operation_already_registered: bool = False,
    delivery_pending: bool = False,
) -> ChatOperationDuplicateDecision:
    """Plan how ingress should handle an existing chat operation.

    Adapters may provide a transport-local ``delivery_pending`` hint when the
    delivery cursor or ledger indicates final delivery is still outstanding even
    if the shared snapshot is missing or stale.
    """

    if delivery_pending:
        return ChatOperationDuplicateDecision(
            action=ChatOperationDuplicateAction.SUPPRESS_PENDING_DELIVERY,
            reason="delivery_pending_duplicate_suppressed",
            previous_state=snapshot.state if snapshot is not None else None,
            delivery_pending=True,
            rationale={
                "delivery_state": (
                    snapshot.delivery_state if snapshot is not None else None
                ),
            },
        )

    if snapshot is None:
        if operation_already_registered:
            return ChatOperationDuplicateDecision(
                action=ChatOperationDuplicateAction.SUPPRESS_IN_FLIGHT,
                reason="existing_operation_duplicate_suppressed",
                previous_state=None,
                delivery_pending=False,
            )
        return ChatOperationDuplicateDecision(
            action=ChatOperationDuplicateAction.ACCEPT_FRESH,
            reason="no_existing_snapshot",
            previous_state=None,
            delivery_pending=False,
        )

    if chat_operation_is_terminal_duplicate(snapshot):
        return ChatOperationDuplicateDecision(
            action=ChatOperationDuplicateAction.REJECT_TERMINAL,
            reason="terminal_duplicate_rejected",
            previous_state=snapshot.state,
            delivery_pending=False,
            rationale={"terminal_outcome": snapshot.terminal_outcome},
        )

    if snapshot.state == ChatOperationState.DELIVERING:
        return ChatOperationDuplicateDecision(
            action=ChatOperationDuplicateAction.SUPPRESS_PENDING_DELIVERY,
            reason="delivering_duplicate_suppressed",
            previous_state=snapshot.state,
            delivery_pending=False,
            rationale={"delivery_state": snapshot.delivery_state},
        )

    return ChatOperationDuplicateDecision(
        action=ChatOperationDuplicateAction.SUPPRESS_IN_FLIGHT,
        reason="non_terminal_duplicate_suppressed",
        previous_state=snapshot.state,
        delivery_pending=False,
    )


__all__ = [
    "ChatOperationDuplicateAction",
    "ChatOperationDuplicateDecision",
    "chat_operation_is_terminal_duplicate",
    "plan_chat_operation_duplicate",
]
