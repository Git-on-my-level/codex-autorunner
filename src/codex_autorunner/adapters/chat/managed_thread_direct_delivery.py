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
class ManagedThreadDirectDeliveryLease:
    engine: SQLiteManagedThreadDeliveryEngine
    delivery_id: str
    claim_token: str


class ManagedThreadDirectDeliverySendSuppressed:
    """Active foreign claim owns delivery; direct surface send must not run."""

    __slots__ = ()


MANAGED_THREAD_DIRECT_DELIVERY_SEND_SUPPRESSED = (
    ManagedThreadDirectDeliverySendSuppressed()
)


def begin_managed_thread_direct_delivery(
    state_root: Path,
    *,
    delivery_id: Optional[str],
    claim_token: Optional[str] = None,
) -> Optional[ManagedThreadDirectDeliveryLease]:
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
    return ManagedThreadDirectDeliveryLease(
        engine=engine,
        delivery_id=normalized_delivery_id,
        claim_token=claim.claim_token,
    )


def complete_managed_thread_direct_delivery(
    lease: ManagedThreadDirectDeliveryLease,
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
    return lease.engine.record_attempt_result(
        lease.delivery_id,
        claim_token=lease.claim_token,
        result=result,
    )


__all__ = [
    "MANAGED_THREAD_DIRECT_DELIVERY_SEND_SUPPRESSED",
    "ManagedThreadDirectDeliveryLease",
    "ManagedThreadDirectDeliverySendSuppressed",
    "begin_managed_thread_direct_delivery",
    "complete_managed_thread_direct_delivery",
]
