from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.orchestration.managed_thread_delivery import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryRecord,
)
from ...core.orchestration.managed_thread_delivery_ledger import (
    SQLiteManagedThreadDeliveryEngine,
)


@dataclass(frozen=True)
class ManagedThreadDirectDeliveryReservation:
    engine: SQLiteManagedThreadDeliveryEngine
    delivery_id: str
    claim_token: str


def reserve_managed_thread_direct_delivery(
    state_root: Path,
    *,
    delivery_id: Optional[str],
    claim_token: Optional[str] = None,
) -> Optional[ManagedThreadDirectDeliveryReservation]:
    normalized_delivery_id = str(delivery_id or "").strip()
    if not normalized_delivery_id:
        return None
    engine = SQLiteManagedThreadDeliveryEngine(Path(state_root))
    normalized_claim_token = str(claim_token or "").strip()
    claim = engine.ensure_direct_delivery_claim(
        normalized_delivery_id,
        proposed_token=normalized_claim_token or None,
    )
    if claim is None:
        return None
    return ManagedThreadDirectDeliveryReservation(
        engine=engine,
        delivery_id=normalized_delivery_id,
        claim_token=claim.claim_token,
    )


def record_managed_thread_direct_delivery(
    reservation: ManagedThreadDirectDeliveryReservation,
    *,
    delivered: bool,
    detail: Optional[str],
    metadata: Optional[Mapping[str, Any]] = None,
) -> Optional[ManagedThreadDeliveryRecord]:
    outcome = (
        ManagedThreadDeliveryOutcome.DIRECT_SURFACE_DELIVERED
        if delivered
        else ManagedThreadDeliveryOutcome.RETRY
    )
    result = ManagedThreadDeliveryAttemptResult(
        outcome=outcome,
        error=str(detail or "").strip() or None,
        metadata=dict(metadata or {}),
    )
    return reservation.engine.record_attempt_result(
        reservation.delivery_id,
        claim_token=reservation.claim_token,
        result=result,
    )


__all__ = [
    "ManagedThreadDirectDeliveryReservation",
    "record_managed_thread_direct_delivery",
    "reserve_managed_thread_direct_delivery",
]
