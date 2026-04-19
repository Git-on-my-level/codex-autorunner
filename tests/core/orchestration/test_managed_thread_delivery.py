from __future__ import annotations

from datetime import datetime, timezone

from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryRecoveryAction,
    ManagedThreadDeliveryState,
    ManagedThreadDeliveryTarget,
    build_managed_thread_delivery_id,
    build_managed_thread_delivery_idempotency_key,
    is_valid_managed_thread_delivery_transition,
    plan_managed_thread_delivery_recovery,
    record_from_intent,
)
from codex_autorunner.core.orchestration.managed_thread_delivery import (
    ManagedThreadDeliveryIntent,
)


def _intent(
    *,
    delivery_id: str = "delivery-1",
    managed_thread_id: str = "thread-1",
    managed_turn_id: str = "turn-1",
    surface_kind: str = "telegram",
    surface_key: str = "chat-1",
    not_before: str | None = None,
) -> ManagedThreadDeliveryIntent:
    return ManagedThreadDeliveryIntent(
        delivery_id=delivery_id,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        idempotency_key=build_managed_thread_delivery_idempotency_key(
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            surface_kind=surface_kind,
            surface_key=surface_key,
        ),
        target=ManagedThreadDeliveryTarget(
            surface_kind=surface_kind,
            adapter_key=surface_kind,
            surface_key=surface_key,
        ),
        envelope=ManagedThreadDeliveryEnvelope(
            envelope_version="managed_thread_delivery.v1",
            final_status="ok",
            assistant_text="hello",
        ),
        not_before=not_before,
    )


def test_build_managed_thread_delivery_idempotency_key_is_stable() -> None:
    key_a = build_managed_thread_delivery_idempotency_key(
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        surface_kind="discord",
        surface_key="channel-1",
    )
    key_b = build_managed_thread_delivery_idempotency_key(
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        surface_kind="discord",
        surface_key="channel-1",
    )

    assert key_a == key_b
    assert key_a.startswith("managed-delivery:")


def test_build_managed_thread_delivery_id_changes_with_surface_target() -> None:
    first = build_managed_thread_delivery_id(
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        surface_kind="telegram",
        surface_key="chat-a",
    )
    second = build_managed_thread_delivery_id(
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        surface_kind="telegram",
        surface_key="chat-b",
    )

    assert first != second
    assert first.startswith("mtdlv:")


def test_record_from_intent_starts_pending_with_due_timestamp() -> None:
    record = record_from_intent(_intent(not_before="2026-04-18T01:02:03Z"))

    assert record.state is ManagedThreadDeliveryState.PENDING
    assert record.next_attempt_at == "2026-04-18T01:02:03Z"
    assert record.target.adapter_key == "telegram"


def test_recovery_claims_due_pending_delivery() -> None:
    record = record_from_intent(_intent())

    decision = plan_managed_thread_delivery_recovery(
        record,
        now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
    )

    assert decision.action is ManagedThreadDeliveryRecoveryAction.CLAIM


def test_recovery_retries_expired_claim_before_budget_exhaustion() -> None:
    record = record_from_intent(_intent())
    record = record.__class__(
        **{
            **record.__dict__,
            "state": ManagedThreadDeliveryState.CLAIMED,
            "attempt_count": 2,
            "claimed_at": "2026-04-18T01:00:00Z",
            "claim_expires_at": "2026-04-18T01:05:00Z",
            "updated_at": "2026-04-18T01:00:00Z",
        }
    )

    decision = plan_managed_thread_delivery_recovery(
        record,
        now=datetime(2026, 4, 18, 1, 10, 0, tzinfo=timezone.utc),
        max_attempts=5,
    )

    assert decision.action is ManagedThreadDeliveryRecoveryAction.RETRY


def test_transition_contract_rejects_delivered_back_to_pending() -> None:
    assert (
        is_valid_managed_thread_delivery_transition(
            ManagedThreadDeliveryState.DELIVERED,
            ManagedThreadDeliveryState.PENDING,
        )
        is False
    )
