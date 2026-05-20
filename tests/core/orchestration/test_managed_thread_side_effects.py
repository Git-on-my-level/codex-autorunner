from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration import initialize_orchestration_sqlite
from codex_autorunner.core.orchestration.managed_thread_side_effects import (
    MANAGED_THREAD_SIDE_EFFECT_TERMINAL_STATES,
    ManagedThreadSideEffectAttemptResult,
    ManagedThreadSideEffectIntent,
    ManagedThreadSideEffectOutcome,
    ManagedThreadSideEffectState,
    SQLiteManagedThreadSideEffectEngine,
    SQLiteManagedThreadSideEffectLedger,
    build_managed_thread_side_effect_id,
    build_managed_thread_side_effect_idempotency_key,
    is_valid_managed_thread_side_effect_transition,
)


def _hub_root(tmp_path: Path) -> Path:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    return hub_root


def _engine(tmp_path: Path) -> SQLiteManagedThreadSideEffectEngine:
    return SQLiteManagedThreadSideEffectEngine(
        _hub_root(tmp_path),
        durable=False,
        retry_backoff=timedelta(seconds=0),
    )


def _ledger(tmp_path: Path) -> SQLiteManagedThreadSideEffectLedger:
    return SQLiteManagedThreadSideEffectLedger(_hub_root(tmp_path), durable=False)


def _intent(
    *,
    effect_kind: str = "transcript",
    managed_thread_id: str = "thread-1",
    managed_turn_id: str = "turn-1",
    surface_kind: str = "telegram",
    surface_key: str = "chat-1",
) -> ManagedThreadSideEffectIntent:
    return ManagedThreadSideEffectIntent(
        effect_id=build_managed_thread_side_effect_id(
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            surface_kind=surface_kind,
            surface_key=surface_key,
            effect_kind=effect_kind,
        ),
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        idempotency_key=build_managed_thread_side_effect_idempotency_key(
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            surface_kind=surface_kind,
            surface_key=surface_key,
            effect_kind=effect_kind,
        ),
        effect_kind=effect_kind,
        surface_kind=surface_kind,
        surface_key=surface_key,
        payload={"status": "ok"},
    )


def test_registers_side_effect_intent_idempotently(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    first = engine.create_intent(_intent())
    second = engine.create_intent(_intent())

    assert first.inserted is True
    assert second.inserted is False
    assert second.record.effect_id == first.record.effect_id
    assert second.record.state is ManagedThreadSideEffectState.PENDING


def test_retryable_persistence_failure_can_be_claimed_again(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    registration = engine.create_intent(_intent(effect_kind="final_timeline"))

    first_claim = engine.claim_effect(registration.record.effect_id)
    assert first_claim is not None
    failed = engine.record_attempt_result(
        registration.record.effect_id,
        claim_token=first_claim.claim_token,
        result=ManagedThreadSideEffectAttemptResult(
            outcome=ManagedThreadSideEffectOutcome.RETRY,
            error="timeline store unavailable",
        ),
    )
    assert failed is not None
    assert failed.state is ManagedThreadSideEffectState.RETRY_SCHEDULED
    assert failed.last_error == "timeline store unavailable"

    second_claim = engine.claim_next(effect_kind="final_timeline")
    assert second_claim is not None
    assert second_claim.record.effect_id == registration.record.effect_id

    completed = engine.record_attempt_result(
        registration.record.effect_id,
        claim_token=second_claim.claim_token,
        result=ManagedThreadSideEffectAttemptResult(
            outcome=ManagedThreadSideEffectOutcome.SUCCEEDED
        ),
    )
    assert completed is not None
    assert completed.state is ManagedThreadSideEffectState.SUCCEEDED
    assert completed.completed_at is not None


def test_expired_side_effect_claim_recovers_to_retry(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    registration = engine.create_intent(_intent(effect_kind="thread_activity"))
    claim = engine.claim_effect(registration.record.effect_id)
    assert claim is not None

    expired_at = datetime(2026, 5, 14, 1, 0, tzinfo=timezone.utc)
    engine._ledger.patch(
        registration.record.effect_id,
        claim_expires_at=(expired_at - timedelta(minutes=1)).isoformat(),
    )

    recovered = engine.recover_expired_claims(now=expired_at)

    assert recovered == 1
    record = engine._ledger.get_effect(registration.record.effect_id)
    assert record is not None
    assert record.state is ManagedThreadSideEffectState.RETRY_SCHEDULED
    assert record.last_error == "claim_expired"


@pytest.mark.parametrize(
    ("from_state", "to_state", "expected"),
    (
        (
            ManagedThreadSideEffectState.PENDING,
            ManagedThreadSideEffectState.CLAIMED,
            True,
        ),
        (
            ManagedThreadSideEffectState.RETRY_SCHEDULED,
            ManagedThreadSideEffectState.CLAIMED,
            True,
        ),
        (
            ManagedThreadSideEffectState.CLAIMED,
            ManagedThreadSideEffectState.RUNNING,
            True,
        ),
        (
            ManagedThreadSideEffectState.CLAIMED,
            ManagedThreadSideEffectState.RETRY_SCHEDULED,
            True,
        ),
        (
            ManagedThreadSideEffectState.RUNNING,
            ManagedThreadSideEffectState.SUCCEEDED,
            True,
        ),
        (
            ManagedThreadSideEffectState.RUNNING,
            ManagedThreadSideEffectState.FAILED,
            True,
        ),
        (
            ManagedThreadSideEffectState.RUNNING,
            ManagedThreadSideEffectState.ABANDONED,
            True,
        ),
        (
            ManagedThreadSideEffectState.RUNNING,
            ManagedThreadSideEffectState.RETRY_SCHEDULED,
            True,
        ),
        (
            ManagedThreadSideEffectState.PENDING,
            ManagedThreadSideEffectState.SUCCEEDED,
            False,
        ),
        (
            ManagedThreadSideEffectState.SUCCEEDED,
            ManagedThreadSideEffectState.PENDING,
            False,
        ),
    ),
)
def test_side_effect_transition_contract(
    from_state: ManagedThreadSideEffectState,
    to_state: ManagedThreadSideEffectState,
    expected: bool,
) -> None:
    assert (
        is_valid_managed_thread_side_effect_transition(from_state, to_state) is expected
    )


def test_side_effect_terminal_states_have_no_outgoing_transitions() -> None:
    for state in MANAGED_THREAD_SIDE_EFFECT_TERMINAL_STATES:
        for target in ManagedThreadSideEffectState:
            if target == state:
                continue
            assert (
                is_valid_managed_thread_side_effect_transition(state, target) is False
            ), f"{state.value} -> {target.value} should be invalid"


def test_same_state_metadata_patch_is_allowed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    registration = ledger.register_intent(_intent())

    updated = ledger.patch(
        registration.record.effect_id,
        state=ManagedThreadSideEffectState.PENDING,
        last_error="metadata refresh",
    )

    assert updated is not None
    assert updated.state is ManagedThreadSideEffectState.PENDING
    assert updated.last_error == "metadata refresh"


def test_terminal_side_effect_rewind_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    registration = engine.create_intent(_intent())
    claim = engine.claim_effect(registration.record.effect_id)
    assert claim is not None
    completed = engine.record_attempt_result(
        registration.record.effect_id,
        claim_token=claim.claim_token,
        result=ManagedThreadSideEffectAttemptResult(
            outcome=ManagedThreadSideEffectOutcome.SUCCEEDED
        ),
    )
    assert completed is not None
    assert completed.state is ManagedThreadSideEffectState.SUCCEEDED

    with pytest.raises(ValueError, match="invalid managed-thread side-effect"):
        engine._ledger.patch(
            registration.record.effect_id,
            state=ManagedThreadSideEffectState.CLAIMED,
        )

    stored = engine._ledger.get_effect(registration.record.effect_id)
    assert stored is not None
    assert stored.state is ManagedThreadSideEffectState.SUCCEEDED


def test_failed_and_abandoned_outcomes_persist(tmp_path: Path) -> None:
    failed_engine = SQLiteManagedThreadSideEffectEngine(
        _hub_root(tmp_path / "failed"),
        durable=False,
        max_attempts=1,
    )
    failed_registration = failed_engine.create_intent(_intent(effect_kind="failed"))
    failed_claim = failed_engine.claim_effect(failed_registration.record.effect_id)
    assert failed_claim is not None

    failed = failed_engine.record_attempt_result(
        failed_registration.record.effect_id,
        claim_token=failed_claim.claim_token,
        result=ManagedThreadSideEffectAttemptResult(
            outcome=ManagedThreadSideEffectOutcome.FAILED,
            error="permanent",
        ),
    )

    assert failed is not None
    assert failed.state is ManagedThreadSideEffectState.FAILED
    assert failed.last_error == "permanent"

    abandoned_engine = _engine(tmp_path / "abandoned")
    abandoned_registration = abandoned_engine.create_intent(
        _intent(effect_kind="abandoned")
    )
    abandoned_claim = abandoned_engine.claim_effect(
        abandoned_registration.record.effect_id
    )
    assert abandoned_claim is not None

    abandoned = abandoned_engine.record_attempt_result(
        abandoned_registration.record.effect_id,
        claim_token=abandoned_claim.claim_token,
        result=ManagedThreadSideEffectAttemptResult(
            outcome=ManagedThreadSideEffectOutcome.ABANDONED,
            error="policy",
        ),
    )

    assert abandoned is not None
    assert abandoned.state is ManagedThreadSideEffectState.ABANDONED
    assert abandoned.last_error == "policy"
