"""Domain-level recovery contract for chat operations.

This module defines the authoritative recovery decision model for chat
operations.  The domain recovery planner produces typed decisions from
durable snapshot state.  Adapters project these decisions into
transport-specific recovery behavior without reinterpreting the domain
contract.

## Domain vs Adapter Boundary

### Domain owns:

- Recovery action taxonomy (the set of valid recovery actions).
- Recovery decision structure (what fields a decision carries).
- State-based recovery planning logic (which durable states lead to
  which recovery actions).

### Adapter owns:

- Transport-specific delivery cursor inspection (e.g., Discord
  interaction cursor state / mode, Telegram message-id tracking).
- Acknowledgment mode interpretation (e.g., whether a Discord
  ``defer_ephemeral`` constitutes a durable ack).
- Exponential backoff timing and cursor-unchanged detection.
- Envelope / payload validation before recovery is attempted.
- Mapping from domain actions to transport-specific side effects.

### Extension rule:

When an adapter needs a recovery outcome not represented in
``ChatOperationRecoveryAction``, the domain contract must be extended
rather than bypassed with adapter-local action strings.

## Decision Model

The recovery planner takes a ``ChatOperationSnapshot`` and produces a
``ChatOperationRecoveryDecision``.  The decision carries:

- **action** -- what recovery step to take.
- **reason** -- human-readable rationale for observability.
- **previous_state** -- the durable state that triggered this decision.
- **delivery_pending** -- whether the snapshot has a pending or failed
  delivery.
- **execution_replayable** -- whether the snapshot state suggests
  execution can be resumed.
- **attempt_count** -- how many delivery recovery attempts have been made.
- **rationale** -- structured key-value pairs for debugging and metrics.

Adapters may supplement the domain decision with adapter-specific checks
(e.g., backoff timing, cursor hash comparison) but must not override a
domain terminal decision (``MARK_ABANDONED``, ``MARK_EXPIRED``) with a
non-terminal action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional

from ..text_utils import _parse_iso_timestamp
from .chat_operation_state import (
    CHAT_OPERATION_TERMINAL_STATES,
    ChatOperationSnapshot,
    ChatOperationState,
)

_DEFAULT_UNACKED_EXPIRY = timedelta(minutes=5)
_DEFAULT_DELIVERY_STALE_WINDOW = timedelta(minutes=15)
_DEFAULT_MAX_DELIVERY_ATTEMPTS = 3

_EXECUTION_RESUME_STATES: frozenset[ChatOperationState] = frozenset(
    {
        ChatOperationState.ACKNOWLEDGED,
        ChatOperationState.VISIBLE,
        ChatOperationState.QUEUED,
        ChatOperationState.RUNNING,
        ChatOperationState.INTERRUPTING,
        ChatOperationState.ROUTING,
        ChatOperationState.BLOCKED,
    }
)


class ChatOperationRecoveryAction(str, Enum):
    """Authoritative recovery actions for one chat operation.

    These actions represent the domain-level decision space.  Adapters
    project these into transport-specific behavior (e.g., Discord
    interaction replay, Telegram message re-delivery).

    Members are ``str`` subclasses so that equality comparisons with
    plain strings remain backward-compatible.
    """

    NOOP = "noop"
    RESUME_EXECUTION = "resume_execution"
    REPLAY_DELIVERY = "replay_delivery"
    MARK_ABANDONED = "mark_abandoned"
    MARK_EXPIRED = "mark_expired"


def _delivery_is_pending(snapshot: ChatOperationSnapshot) -> bool:
    return snapshot.delivery_state in {"pending", "failed"}


def _execution_is_resumeable(snapshot: ChatOperationSnapshot) -> bool:
    return snapshot.state in _EXECUTION_RESUME_STATES


@dataclass(frozen=True)
class ChatOperationRecoveryDecision:
    """Authoritative recovery decision for one chat operation.

    Contains enough structured evidence for adapters to project recovery
    behavior without re-interpreting durable state or duplicating
    decision logic.
    """

    action: ChatOperationRecoveryAction
    reason: str
    previous_state: ChatOperationState
    delivery_pending: bool
    execution_replayable: bool
    attempt_count: int
    rationale: Mapping[str, Any] = field(default_factory=dict)


def plan_chat_operation_recovery(
    snapshot: ChatOperationSnapshot,
    *,
    now: Optional[datetime] = None,
    max_delivery_attempts: int = _DEFAULT_MAX_DELIVERY_ATTEMPTS,
    unacked_expiry: timedelta = _DEFAULT_UNACKED_EXPIRY,
    delivery_stale_window: timedelta = _DEFAULT_DELIVERY_STALE_WINDOW,
) -> ChatOperationRecoveryDecision:
    """Plan recovery for one chat operation from its durable snapshot.

    This is the domain-level pure decision function.  It computes a
    recovery decision from the snapshot's durable state without
    accessing adapter-specific stores or performing I/O.

    Adapters call this function and then project the decision into
    transport-specific behavior.  If the adapter has additional
    constraints (e.g., backoff timing, cursor hash), those are applied
    after the domain decision but must not override terminal decisions.
    """
    current_at = now or datetime.now(timezone.utc)
    delivery_pending = _delivery_is_pending(snapshot)
    execution_replayable = _execution_is_resumeable(snapshot)
    attempt_count = int(snapshot.delivery_attempt_count or 0)

    # ``terminal_outcome`` records adapter-visible outcomes; it is not always a
    # durable terminal for recovery. For example Discord records
    # ``delivery_failed`` while the operation remains ``DELIVERING`` with a
    # failed delivery cursor so replay/abandon logic can still run.
    if snapshot.terminal_outcome and not (
        snapshot.terminal_outcome == "delivery_failed"
        and snapshot.state not in CHAT_OPERATION_TERMINAL_STATES
        and _delivery_is_pending(snapshot)
    ):
        return ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.NOOP,
            reason="terminal_outcome_already_recorded",
            previous_state=snapshot.state,
            delivery_pending=delivery_pending,
            execution_replayable=execution_replayable,
            attempt_count=attempt_count,
            rationale={"terminal_outcome": snapshot.terminal_outcome},
        )

    if snapshot.state in CHAT_OPERATION_TERMINAL_STATES:
        return ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.NOOP,
            reason="terminal_state",
            previous_state=snapshot.state,
            delivery_pending=delivery_pending,
            execution_replayable=execution_replayable,
            attempt_count=attempt_count,
        )

    if delivery_pending:
        if attempt_count >= max_delivery_attempts:
            return ChatOperationRecoveryDecision(
                action=ChatOperationRecoveryAction.MARK_ABANDONED,
                reason="delivery_attempt_budget_exhausted",
                previous_state=snapshot.state,
                delivery_pending=True,
                execution_replayable=execution_replayable,
                attempt_count=attempt_count,
                rationale={
                    "attempt_count": attempt_count,
                    "max_attempts": max_delivery_attempts,
                },
            )
        updated_at = _parse_iso_timestamp(snapshot.updated_at)
        if updated_at is None or current_at - updated_at >= delivery_stale_window:
            return ChatOperationRecoveryDecision(
                action=ChatOperationRecoveryAction.REPLAY_DELIVERY,
                reason="delivery_replay_required",
                previous_state=snapshot.state,
                delivery_pending=True,
                execution_replayable=execution_replayable,
                attempt_count=attempt_count,
                rationale={
                    "stale": updated_at is None,
                    "stale_window_seconds": delivery_stale_window.total_seconds(),
                },
            )
        return ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.NOOP,
            reason="delivery_backoff_active",
            previous_state=snapshot.state,
            delivery_pending=True,
            execution_replayable=execution_replayable,
            attempt_count=attempt_count,
            rationale={
                "attempt_count": attempt_count,
                "stale_window_seconds": delivery_stale_window.total_seconds(),
            },
        )

    if execution_replayable:
        return ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.RESUME_EXECUTION,
            reason="execution_resume_required",
            previous_state=snapshot.state,
            delivery_pending=False,
            execution_replayable=True,
            attempt_count=attempt_count,
            rationale={"state": snapshot.state.value},
        )

    if snapshot.state == ChatOperationState.RECEIVED:
        created_at = _parse_iso_timestamp(snapshot.created_at or snapshot.updated_at)
        if created_at is None or current_at - created_at >= unacked_expiry:
            return ChatOperationRecoveryDecision(
                action=ChatOperationRecoveryAction.MARK_EXPIRED,
                reason="accepted_operation_never_acknowledged",
                previous_state=snapshot.state,
                delivery_pending=False,
                execution_replayable=False,
                attempt_count=attempt_count,
                rationale={
                    "unacked_expiry_seconds": unacked_expiry.total_seconds(),
                },
            )

    return ChatOperationRecoveryDecision(
        action=ChatOperationRecoveryAction.NOOP,
        reason="no_recovery_action",
        previous_state=snapshot.state,
        delivery_pending=False,
        execution_replayable=False,
        attempt_count=attempt_count,
    )


__all__ = [
    "ChatOperationRecoveryAction",
    "ChatOperationRecoveryDecision",
    "plan_chat_operation_recovery",
]
