"""Adapter contract for durable managed-thread final delivery.

This module belongs to `integrations/chat` because adapters own transport
translation, not delivery truth. The control plane persists
`ManagedThreadDeliveryRecord` snapshots and owns claims, retries, replay, and
terminal accounting. Discord and Telegram implement this protocol to turn a
durable envelope into platform API calls or platform-local outbox records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from ...core.orchestration.managed_thread_delivery import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryClaim,
    ManagedThreadDeliveryRecord,
)


@dataclass(frozen=True)
class ManagedThreadDeliveryAdapterContext:
    """Small adapter-facing view of the durable record identity."""

    delivery_id: str
    managed_thread_id: str
    managed_turn_id: str
    surface_kind: str
    surface_key: str
    idempotency_key: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(
        cls, record: ManagedThreadDeliveryRecord
    ) -> "ManagedThreadDeliveryAdapterContext":
        return cls(
            delivery_id=record.delivery_id,
            managed_thread_id=record.managed_thread_id,
            managed_turn_id=record.managed_turn_id,
            surface_kind=record.target.surface_kind,
            surface_key=record.target.surface_key,
            idempotency_key=record.idempotency_key,
            metadata=dict(record.metadata or {}),
        )


@runtime_checkable
class ManagedThreadDeliveryAdapter(Protocol):
    """Transport-only adapter contract for durable managed-thread delivery.

    Adapters may:
    - translate the envelope into native API calls
    - reuse transport-local outbox helpers internally
    - attach adapter-specific cursors for replay

    Adapters may not:
    - decide whether a delivery record exists
    - own retry or lease policy
    - mark durable terminal state without returning a normalized result
    """

    @property
    def adapter_key(self) -> str:
        """Stable adapter id used by the engine when claiming work."""

    async def deliver_managed_thread_record(
        self,
        record: ManagedThreadDeliveryRecord,
        *,
        claim: ManagedThreadDeliveryClaim,
    ) -> ManagedThreadDeliveryAttemptResult:
        """Perform one delivery attempt for a control-plane owned record."""


__all__ = [
    "ManagedThreadDeliveryAdapter",
    "ManagedThreadDeliveryAdapterContext",
]
