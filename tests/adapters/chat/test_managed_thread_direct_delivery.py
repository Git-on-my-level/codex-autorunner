"""Tests for direct-surface durable delivery reservation helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_autorunner.adapters.chat.managed_thread_direct_delivery import (
    begin_managed_thread_direct_delivery,
    complete_managed_thread_direct_delivery,
)
from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryIntent,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryState,
    ManagedThreadDeliveryTarget,
    SQLiteManagedThreadDeliveryEngine,
    build_managed_thread_delivery_idempotency_key,
    initialize_orchestration_sqlite,
)


def _intent(
    tmp_path: Path, *, adapter_key: str = "telegram"
) -> ManagedThreadDeliveryIntent:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    return ManagedThreadDeliveryIntent(
        delivery_id="delivery-1",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        idempotency_key=build_managed_thread_delivery_idempotency_key(
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
            surface_kind="telegram",
            surface_key="chat-1",
        ),
        target=ManagedThreadDeliveryTarget(
            surface_kind="telegram",
            adapter_key=adapter_key,
            surface_key="chat-1",
        ),
        envelope=ManagedThreadDeliveryEnvelope(
            envelope_version="managed_thread_delivery.v1",
            final_status="ok",
            assistant_text="hello",
        ),
    )


def test_begin_returns_none_when_another_active_claim_owns_delivery(
    tmp_path: Path,
) -> None:
    intent = _intent(tmp_path)
    hub_root = tmp_path / "hub"
    engine = SQLiteManagedThreadDeliveryEngine(
        hub_root,
        durable=False,
        claim_ttl=timedelta(minutes=5),
    )
    engine.create_intent(intent)
    current_at = datetime.now(timezone.utc)
    first = engine.claim_delivery(
        "delivery-1",
        now=current_at,
    )
    assert first is not None
    lease = begin_managed_thread_direct_delivery(
        hub_root,
        delivery_id="delivery-1",
        claim_token="stale-token",
    )
    assert lease is None


def test_begin_accepts_matching_active_claim_token_before_recording(
    tmp_path: Path,
) -> None:
    intent = _intent(tmp_path)
    hub_root = tmp_path / "hub"
    engine = SQLiteManagedThreadDeliveryEngine(
        hub_root,
        durable=False,
        claim_ttl=timedelta(minutes=5),
    )
    engine.create_intent(intent)
    current_at = datetime.now(timezone.utc)
    first = engine.claim_delivery(
        "delivery-1",
        now=current_at,
    )
    assert first is not None
    lease = begin_managed_thread_direct_delivery(
        hub_root,
        delivery_id="delivery-1",
        claim_token=first.claim_token,
    )
    assert lease is not None
    assert lease.claim_token == first.claim_token
    updated = complete_managed_thread_direct_delivery(
        lease,
        delivered=True,
        detail="test",
        metadata={"delivery_surface": "telegram"},
    )
    assert updated is not None
    assert updated.state is ManagedThreadDeliveryState.DIRECT_SURFACE_DELIVERED


def test_reserve_returns_none_when_already_direct_surface_delivered(
    tmp_path: Path,
) -> None:
    intent = _intent(tmp_path)
    hub_root = tmp_path / "hub"
    engine = SQLiteManagedThreadDeliveryEngine(hub_root, durable=False)
    engine.create_intent(intent)
    claim = engine.claim_delivery(
        "delivery-1",
        now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
    )
    assert claim is not None
    engine.record_attempt_result(
        "delivery-1",
        claim_token=claim.claim_token,
        result=ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.DIRECT_SURFACE_DELIVERED,
        ),
    )
    lease = begin_managed_thread_direct_delivery(
        hub_root,
        delivery_id="delivery-1",
        claim_token=claim.claim_token,
    )
    assert lease is None
