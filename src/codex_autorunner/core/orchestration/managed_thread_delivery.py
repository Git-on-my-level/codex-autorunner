"""Control-plane contract for durable managed-thread final delivery.

This module defines the architecture slice for issue #1498 without pretending
the migration is already complete. The control plane owns durable intent,
claims, replay policy, and terminal accounting for managed-thread final
delivery. Surface adapters may translate the envelope into transport calls, but
they must not become the long-term source of truth for whether a finalized turn
still needs delivery work.

Placement matters:

- `core/orchestration/*` owns the durable ledger model and engine entrypoints.
- `integrations/chat/*` owns the adapter contract that consumes these records.
- `integrations/chat/managed_thread_turns.py` remains a compatibility bridge
  until future tickets replace the legacy `deliver_result` callback seam with
  durable intent creation plus engine-driven replay.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from ..time_utils import now_iso

_UNSET = object()
_DEFAULT_CLAIM_TTL = timedelta(minutes=5)
_DEFAULT_RETRY_BACKOFF = timedelta(minutes=1)


class ManagedThreadDeliveryState(str, Enum):
    """Authoritative lifecycle states for one durable final-delivery record."""

    PENDING = "pending"
    CLAIMED = "claimed"
    DELIVERING = "delivering"
    RETRY_SCHEDULED = "retry_scheduled"
    DELIVERED = "delivered"
    FAILED = "failed"
    ABANDONED = "abandoned"
    EXPIRED = "expired"


MANAGED_THREAD_DELIVERY_TERMINAL_STATES = frozenset(
    {
        ManagedThreadDeliveryState.DELIVERED,
        ManagedThreadDeliveryState.FAILED,
        ManagedThreadDeliveryState.ABANDONED,
        ManagedThreadDeliveryState.EXPIRED,
    }
)

# Future tickets may add finer-grained bookkeeping, but they should extend this
# shared transition map rather than reintroducing adapter-local state machines.
MANAGED_THREAD_DELIVERY_ALLOWED_TRANSITIONS: dict[
    ManagedThreadDeliveryState, frozenset[ManagedThreadDeliveryState]
] = {
    ManagedThreadDeliveryState.PENDING: frozenset(
        {
            ManagedThreadDeliveryState.CLAIMED,
            ManagedThreadDeliveryState.DELIVERING,
            ManagedThreadDeliveryState.RETRY_SCHEDULED,
            ManagedThreadDeliveryState.ABANDONED,
            ManagedThreadDeliveryState.EXPIRED,
        }
    ),
    ManagedThreadDeliveryState.CLAIMED: frozenset(
        {
            ManagedThreadDeliveryState.DELIVERING,
            ManagedThreadDeliveryState.RETRY_SCHEDULED,
            ManagedThreadDeliveryState.ABANDONED,
            ManagedThreadDeliveryState.EXPIRED,
        }
    ),
    ManagedThreadDeliveryState.DELIVERING: frozenset(
        {
            ManagedThreadDeliveryState.DELIVERED,
            ManagedThreadDeliveryState.RETRY_SCHEDULED,
            ManagedThreadDeliveryState.FAILED,
            ManagedThreadDeliveryState.ABANDONED,
        }
    ),
    ManagedThreadDeliveryState.RETRY_SCHEDULED: frozenset(
        {
            ManagedThreadDeliveryState.CLAIMED,
            ManagedThreadDeliveryState.DELIVERING,
            ManagedThreadDeliveryState.ABANDONED,
            ManagedThreadDeliveryState.EXPIRED,
        }
    ),
    ManagedThreadDeliveryState.DELIVERED: frozenset(),
    ManagedThreadDeliveryState.FAILED: frozenset(),
    ManagedThreadDeliveryState.ABANDONED: frozenset(),
    ManagedThreadDeliveryState.EXPIRED: frozenset(),
}


class ManagedThreadDeliveryOutcome(str, Enum):
    """Engine-owned result classification for one adapter delivery attempt."""

    DELIVERED = "delivered"
    DUPLICATE = "duplicate"
    RETRY = "retry"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ManagedThreadDeliveryRecoveryAction(str, Enum):
    """Recovery work the engine should perform for an existing record."""

    NOOP = "noop"
    CLAIM = "claim"
    RETRY = "retry"
    ABANDON = "abandon"
    EXPIRE = "expire"


@dataclass(frozen=True)
class ManagedThreadDeliveryAttachment:
    """Transport-agnostic attachment metadata carried with final delivery."""

    attachment_id: str
    kind: str = "file"
    path: Optional[str] = None
    mime_type: Optional[str] = None
    caption: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagedThreadDeliveryEnvelope:
    """Stable final-delivery payload persisted before adapter IO begins."""

    envelope_version: str
    final_status: str
    assistant_text: str
    session_notice: Optional[str] = None
    error_text: Optional[str] = None
    backend_thread_id: Optional[str] = None
    token_usage: Optional[Mapping[str, Any]] = None
    attachments: tuple[ManagedThreadDeliveryAttachment, ...] = field(
        default_factory=tuple
    )
    transport_hints: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagedThreadDeliveryTarget:
    """Surface destination selected before the adapter performs transport IO."""

    surface_kind: str
    adapter_key: str
    surface_key: str
    transport_target: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagedThreadDeliveryIntent:
    """Durable intent emitted by managed-thread finalization."""

    delivery_id: str
    managed_thread_id: str
    managed_turn_id: str
    idempotency_key: str
    target: ManagedThreadDeliveryTarget
    envelope: ManagedThreadDeliveryEnvelope
    source: str = "managed_thread.finalization"
    created_at: Optional[str] = None
    not_before: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagedThreadDeliveryRecord:
    """Durable ledger snapshot for one final delivery obligation."""

    delivery_id: str
    managed_thread_id: str
    managed_turn_id: str
    idempotency_key: str
    target: ManagedThreadDeliveryTarget
    envelope: ManagedThreadDeliveryEnvelope
    state: ManagedThreadDeliveryState
    source: str = "managed_thread.finalization"
    attempt_count: int = 0
    claim_token: Optional[str] = None
    claimed_at: Optional[str] = None
    claim_expires_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    delivered_at: Optional[str] = None
    last_error: Optional[str] = None
    adapter_cursor: Optional[Mapping[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagedThreadDeliveryRegistration:
    record: ManagedThreadDeliveryRecord
    inserted: bool


@dataclass(frozen=True)
class ManagedThreadDeliveryClaim:
    """Lease-like claim granted by the engine before adapter delivery starts."""

    record: ManagedThreadDeliveryRecord
    claim_token: str
    claimed_at: str
    claim_expires_at: str


@dataclass(frozen=True)
class ManagedThreadDeliveryAttemptResult:
    """Adapter result normalized back into engine-owned semantics."""

    outcome: ManagedThreadDeliveryOutcome
    error: Optional[str] = None
    adapter_message_key: Optional[str] = None
    retry_at: Optional[str] = None
    adapter_cursor: Optional[Mapping[str, Any]] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagedThreadDeliveryRecoveryDecision:
    action: ManagedThreadDeliveryRecoveryAction
    reason: str


@runtime_checkable
class ManagedThreadDeliveryLedger(Protocol):
    """Persistence boundary for durable managed-thread delivery records."""

    def register_intent(
        self, intent: ManagedThreadDeliveryIntent
    ) -> ManagedThreadDeliveryRegistration:
        """Insert or reuse a delivery record by durable idempotency key."""

    def get_delivery(self, delivery_id: str) -> Optional[ManagedThreadDeliveryRecord]:
        """Return one durable delivery record by id."""

    def get_delivery_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[ManagedThreadDeliveryRecord]:
        """Return the canonical delivery record for one finalized turn/surface."""

    def patch_delivery(
        self,
        delivery_id: str,
        *,
        state: ManagedThreadDeliveryState | object = _UNSET,
        validate_transition: bool = True,
        metadata_updates: Optional[Mapping[str, Any]] = None,
        **changes: Any,
    ) -> Optional[ManagedThreadDeliveryRecord]:
        """Update durable state after claim, replay, retry scheduling, or completion."""

    def list_due_deliveries(
        self,
        *,
        adapter_key: Optional[str] = None,
        now: Optional[str] = None,
        limit: int = 100,
    ) -> list[ManagedThreadDeliveryRecord]:
        """Return records due for claim or retry work."""


@runtime_checkable
class ManagedThreadDeliveryEngine(Protocol):
    """Engine-owned orchestration entrypoints for durable final delivery."""

    def create_intent(
        self, intent: ManagedThreadDeliveryIntent
    ) -> ManagedThreadDeliveryRegistration:
        """Persist the durable obligation before any transport call begins."""

    def claim_next_delivery(
        self,
        *,
        adapter_key: str,
        now: Optional[datetime] = None,
    ) -> Optional[ManagedThreadDeliveryClaim]:
        """Claim the next due delivery record for one adapter worker."""

    def claim_delivery(
        self,
        delivery_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[ManagedThreadDeliveryClaim]:
        """Claim one specific delivery record for immediate adapter handoff."""

    def record_attempt_result(
        self,
        delivery_id: str,
        *,
        claim_token: str,
        result: ManagedThreadDeliveryAttemptResult,
    ) -> Optional[ManagedThreadDeliveryRecord]:
        """Commit the engine-owned state transition after an adapter attempt."""

    def abandon_delivery(
        self, delivery_id: str, *, detail: Optional[str] = None
    ) -> Optional[ManagedThreadDeliveryRecord]:
        """Mark a delivery as intentionally abandoned by policy."""


def is_valid_managed_thread_delivery_transition(
    current: ManagedThreadDeliveryState,
    nxt: ManagedThreadDeliveryState,
) -> bool:
    """Return True when a delivery state change preserves the shared contract."""

    if current == nxt:
        return True
    return nxt in MANAGED_THREAD_DELIVERY_ALLOWED_TRANSITIONS.get(current, frozenset())


def build_managed_thread_delivery_idempotency_key(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    surface_kind: str,
    surface_key: str,
    phase: str = "final",
) -> str:
    """Build the stable at-least-once suppression key for one delivery intent."""

    digest = hashlib.sha256(
        ":".join(
            (
                str(managed_thread_id or "").strip(),
                str(managed_turn_id or "").strip(),
                str(surface_kind or "").strip(),
                str(surface_key or "").strip(),
                str(phase or "final").strip(),
            )
        ).encode("utf-8")
    ).hexdigest()[:32]
    return f"managed-delivery:{digest}"


def build_managed_thread_delivery_id(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    surface_kind: str,
    surface_key: str,
) -> str:
    """Build the canonical durable record id for one delivery obligation."""

    digest = hashlib.sha256(
        ":".join(
            (
                str(managed_thread_id or "").strip(),
                str(managed_turn_id or "").strip(),
                str(surface_kind or "").strip(),
                str(surface_key or "").strip(),
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"mtdlv:{digest}"


def normalize_managed_thread_delivery_intent(
    intent: ManagedThreadDeliveryIntent,
) -> ManagedThreadDeliveryIntent:
    """Normalize timestamps and ids before persisting a new durable intent."""

    delivery_id = str(intent.delivery_id or "").strip()
    managed_thread_id = str(intent.managed_thread_id or "").strip()
    managed_turn_id = str(intent.managed_turn_id or "").strip()
    idempotency_key = str(intent.idempotency_key or "").strip()
    if not delivery_id:
        raise ValueError("delivery_id is required")
    if not managed_thread_id:
        raise ValueError("managed_thread_id is required")
    if not managed_turn_id:
        raise ValueError("managed_turn_id is required")
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    return replace(
        intent,
        delivery_id=delivery_id,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        idempotency_key=idempotency_key,
        source=_normalized_optional_text(intent.source)
        or "managed_thread.finalization",
        created_at=_normalized_optional_text(intent.created_at) or now_iso(),
        not_before=_normalized_optional_text(intent.not_before),
        metadata=dict(intent.metadata or {}),
    )


def record_from_intent(
    intent: ManagedThreadDeliveryIntent,
    *,
    state: ManagedThreadDeliveryState = ManagedThreadDeliveryState.PENDING,
) -> ManagedThreadDeliveryRecord:
    """Project a normalized intent into the initial durable ledger snapshot."""

    normalized = normalize_managed_thread_delivery_intent(intent)
    timestamp = normalized.created_at or now_iso()
    return ManagedThreadDeliveryRecord(
        delivery_id=normalized.delivery_id,
        managed_thread_id=normalized.managed_thread_id,
        managed_turn_id=normalized.managed_turn_id,
        idempotency_key=normalized.idempotency_key,
        target=normalized.target,
        envelope=normalized.envelope,
        state=state,
        source=normalized.source,
        next_attempt_at=normalized.not_before,
        created_at=timestamp,
        updated_at=timestamp,
        metadata=dict(normalized.metadata or {}),
    )


def plan_managed_thread_delivery_recovery(
    record: ManagedThreadDeliveryRecord,
    *,
    now: Optional[datetime] = None,
    claim_ttl: timedelta = _DEFAULT_CLAIM_TTL,
    retry_backoff: timedelta = _DEFAULT_RETRY_BACKOFF,
    max_attempts: int = 5,
) -> ManagedThreadDeliveryRecoveryDecision:
    """Choose claim/retry/abandon work from durable record state."""

    current_at = now or datetime.now(timezone.utc)
    if record.state in MANAGED_THREAD_DELIVERY_TERMINAL_STATES:
        return ManagedThreadDeliveryRecoveryDecision(
            action=ManagedThreadDeliveryRecoveryAction.NOOP,
            reason="terminal_state",
        )
    if record.state == ManagedThreadDeliveryState.PENDING:
        due_at = _parse_iso_timestamp(record.next_attempt_at)
        if due_at is None or due_at <= current_at:
            return ManagedThreadDeliveryRecoveryDecision(
                action=ManagedThreadDeliveryRecoveryAction.CLAIM,
                reason="pending_delivery_due",
            )
        return ManagedThreadDeliveryRecoveryDecision(
            action=ManagedThreadDeliveryRecoveryAction.NOOP,
            reason="pending_delivery_not_due",
        )
    if record.state == ManagedThreadDeliveryState.RETRY_SCHEDULED:
        due_at = _parse_iso_timestamp(record.next_attempt_at)
        if due_at is None or due_at <= current_at:
            return ManagedThreadDeliveryRecoveryDecision(
                action=ManagedThreadDeliveryRecoveryAction.CLAIM,
                reason="retry_due",
            )
        return ManagedThreadDeliveryRecoveryDecision(
            action=ManagedThreadDeliveryRecoveryAction.NOOP,
            reason="retry_backoff_active",
        )
    if record.state in {
        ManagedThreadDeliveryState.CLAIMED,
        ManagedThreadDeliveryState.DELIVERING,
    }:
        claim_expires_at = _parse_iso_timestamp(record.claim_expires_at)
        if claim_expires_at is None:
            claimed_at = _parse_iso_timestamp(record.claimed_at)
            claim_expires_at = (
                claimed_at + claim_ttl if claimed_at is not None else current_at
            )
        if claim_expires_at <= current_at:
            if int(record.attempt_count or 0) >= max_attempts:
                return ManagedThreadDeliveryRecoveryDecision(
                    action=ManagedThreadDeliveryRecoveryAction.ABANDON,
                    reason="claim_expired_attempt_budget_exhausted",
                )
            return ManagedThreadDeliveryRecoveryDecision(
                action=ManagedThreadDeliveryRecoveryAction.RETRY,
                reason="claim_expired_retry_required",
            )
        return ManagedThreadDeliveryRecoveryDecision(
            action=ManagedThreadDeliveryRecoveryAction.NOOP,
            reason="claim_active",
        )
    if record.state == ManagedThreadDeliveryState.FAILED:
        if int(record.attempt_count or 0) >= max_attempts:
            return ManagedThreadDeliveryRecoveryDecision(
                action=ManagedThreadDeliveryRecoveryAction.ABANDON,
                reason="failure_attempt_budget_exhausted",
            )
        last_update = _parse_iso_timestamp(record.updated_at)
        retry_due = last_update is None or current_at - last_update >= retry_backoff
        if retry_due:
            return ManagedThreadDeliveryRecoveryDecision(
                action=ManagedThreadDeliveryRecoveryAction.RETRY,
                reason="failure_backoff_elapsed",
            )
    return ManagedThreadDeliveryRecoveryDecision(
        action=ManagedThreadDeliveryRecoveryAction.NOOP,
        reason="no_recovery_action",
    )


def default_claim_expiry(
    *, claimed_at: Optional[datetime] = None, claim_ttl: timedelta = _DEFAULT_CLAIM_TTL
) -> str:
    """Return the default lease expiry timestamp for a claimed delivery."""

    started_at = claimed_at or datetime.now(timezone.utc)
    return (started_at + claim_ttl).astimezone(timezone.utc).isoformat()


def _normalized_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    normalized = _normalized_optional_text(value)
    if normalized is None:
        return None
    try:
        if normalized.endswith("Z"):
            return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "MANAGED_THREAD_DELIVERY_ALLOWED_TRANSITIONS",
    "MANAGED_THREAD_DELIVERY_TERMINAL_STATES",
    "ManagedThreadDeliveryAttachment",
    "ManagedThreadDeliveryAttemptResult",
    "ManagedThreadDeliveryClaim",
    "ManagedThreadDeliveryEngine",
    "ManagedThreadDeliveryEnvelope",
    "ManagedThreadDeliveryIntent",
    "ManagedThreadDeliveryLedger",
    "ManagedThreadDeliveryOutcome",
    "ManagedThreadDeliveryRecord",
    "ManagedThreadDeliveryRecoveryAction",
    "ManagedThreadDeliveryRecoveryDecision",
    "ManagedThreadDeliveryRegistration",
    "ManagedThreadDeliveryState",
    "ManagedThreadDeliveryTarget",
    "build_managed_thread_delivery_id",
    "build_managed_thread_delivery_idempotency_key",
    "default_claim_expiry",
    "is_valid_managed_thread_delivery_transition",
    "normalize_managed_thread_delivery_intent",
    "plan_managed_thread_delivery_recovery",
    "record_from_intent",
]
