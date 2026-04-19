from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryIntent,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryRecoveryAction,
    ManagedThreadDeliveryState,
    ManagedThreadDeliveryTarget,
    build_managed_thread_delivery_idempotency_key,
    initialize_orchestration_sqlite,
    is_valid_managed_thread_delivery_transition,
    plan_managed_thread_delivery_recovery,
    record_from_intent,
)
from codex_autorunner.core.orchestration.managed_thread_delivery_ledger import (
    SQLiteManagedThreadDeliveryEngine,
    SQLiteManagedThreadDeliveryLedger,
)


def _hub_root(tmp_path: Path) -> Path:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    return hub_root


def _intent(
    *,
    delivery_id: str = "delivery-1",
    managed_thread_id: str = "thread-1",
    managed_turn_id: str = "turn-1",
    surface_kind: str = "telegram",
    surface_key: str = "chat-1",
    adapter_key: str | None = None,
    not_before: str | None = None,
    assistant_text: str = "hello",
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
            adapter_key=adapter_key or surface_kind,
            surface_key=surface_key,
        ),
        envelope=ManagedThreadDeliveryEnvelope(
            envelope_version="managed_thread_delivery.v1",
            final_status="ok",
            assistant_text=assistant_text,
        ),
        not_before=not_before,
    )


def _ledger(tmp_path: Path) -> SQLiteManagedThreadDeliveryLedger:
    return SQLiteManagedThreadDeliveryLedger(_hub_root(tmp_path), durable=False)


def _engine(tmp_path: Path) -> SQLiteManagedThreadDeliveryEngine:
    return SQLiteManagedThreadDeliveryEngine(
        _hub_root(tmp_path),
        durable=False,
        claim_ttl=timedelta(minutes=5),
        retry_backoff=timedelta(minutes=1),
        max_attempts=5,
    )


class TestLedgerRegisterIntent:
    def test_creates_new_record(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        intent = _intent()
        reg = ledger.register_intent(intent)

        assert reg.inserted is True
        assert reg.record.delivery_id == "delivery-1"
        assert reg.record.state is ManagedThreadDeliveryState.PENDING
        assert reg.record.envelope.assistant_text == "hello"
        assert reg.record.created_at is not None
        assert reg.record.updated_at is not None

    def test_idempotency_deduplication(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        intent = _intent(delivery_id="first")
        reg1 = ledger.register_intent(intent)
        assert reg1.inserted is True

        intent2 = _intent(delivery_id="second")
        reg2 = ledger.register_intent(intent2)
        assert reg2.inserted is False
        assert reg2.record.delivery_id == "first"

    def test_get_delivery_by_id(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent())
        record = ledger.get_delivery("delivery-1")
        assert record is not None
        assert record.delivery_id == "delivery-1"

    def test_get_delivery_by_idempotency_key(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        intent = _intent()
        ledger.register_intent(intent)
        record = ledger.get_delivery_by_idempotency_key(intent.idempotency_key)
        assert record is not None
        assert record.delivery_id == "delivery-1"

    def test_get_missing_delivery_returns_none(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        assert ledger.get_delivery("no-such-id") is None

    def test_different_surfaces_create_separate_records(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        reg1 = ledger.register_intent(
            _intent(delivery_id="d1", surface_kind="telegram", surface_key="chat-a")
        )
        reg2 = ledger.register_intent(
            _intent(delivery_id="d2", surface_kind="discord", surface_key="chan-b")
        )
        assert reg1.inserted is True
        assert reg2.inserted is True
        assert reg1.record.delivery_id != reg2.record.delivery_id


class TestLedgerPatchDelivery:
    def test_state_transition_pending_to_claimed(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent())

        updated = ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-1",
            claimed_at="2026-04-18T01:00:00Z",
            claim_expires_at="2026-04-18T01:05:00Z",
        )
        assert updated is not None
        assert updated.state is ManagedThreadDeliveryState.CLAIMED
        assert updated.claim_token == "tok-1"
        assert updated.attempt_count == 0

    def test_rejects_invalid_transition(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent())
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-1",
            claimed_at="2026-04-18T01:00:00Z",
            claim_expires_at="2026-04-18T01:05:00Z",
        )
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.DELIVERING,
        )

        with pytest.raises(ValueError, match="invalid delivery transition"):
            ledger.patch_delivery(
                "delivery-1",
                state=ManagedThreadDeliveryState.PENDING,
            )

    def test_allows_same_state_transition(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent())

        updated = ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.PENDING,
            last_error="re-check",
        )
        assert updated is not None
        assert updated.last_error == "re-check"

    def test_skip_validation_flag(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent())
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-1",
            claimed_at="2026-04-18T01:00:00Z",
            claim_expires_at="2026-04-18T01:05:00Z",
        )
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.DELIVERING,
        )
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.DELIVERED,
        )

        updated = ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.PENDING,
            validate_transition=False,
        )
        assert updated is not None
        assert updated.state is ManagedThreadDeliveryState.PENDING

    def test_metadata_updates_merge(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent())

        updated = ledger.patch_delivery(
            "delivery-1",
            metadata_updates={"retry_count": 1},
        )
        assert updated is not None
        assert updated.metadata.get("retry_count") == 1

    def test_patch_nonexistent_returns_none(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        result = ledger.patch_delivery("nope", state=ManagedThreadDeliveryState.CLAIMED)
        assert result is None


class TestLedgerListDueDeliveries:
    def test_returns_pending_due_records(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent(delivery_id="d1", adapter_key="telegram"))
        ledger.register_intent(_intent(delivery_id="d2", adapter_key="discord"))

        due = ledger.list_due_deliveries(adapter_key="telegram")
        assert len(due) == 1
        assert due[0].delivery_id == "d1"

    def test_excludes_not_before_in_future(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(
            _intent(delivery_id="d1", not_before="2099-01-01T00:00:00Z")
        )

        due = ledger.list_due_deliveries()
        assert len(due) == 0

    def test_excludes_terminal_states(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent(delivery_id="d1"))
        ledger.patch_delivery(
            "d1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok",
            claimed_at="2026-04-18T01:00:00Z",
            claim_expires_at="2026-04-18T01:05:00Z",
        )
        ledger.patch_delivery(
            "d1",
            state=ManagedThreadDeliveryState.DELIVERING,
        )
        ledger.patch_delivery(
            "d1",
            state=ManagedThreadDeliveryState.DELIVERED,
        )

        due = ledger.list_due_deliveries()
        assert len(due) == 0

    def test_excludes_active_claimed_records(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent(delivery_id="d1"))
        ledger.patch_delivery(
            "d1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok",
            claimed_at="2026-04-18T01:00:00Z",
            claim_expires_at="2026-04-18T01:05:00Z",
        )

        due = ledger.list_due_deliveries()
        assert due == []

    def test_limit_param(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        for i in range(5):
            ledger.register_intent(
                _intent(
                    delivery_id=f"d{i}",
                    managed_turn_id=f"turn-{i}",
                    surface_key=f"chat-{i}",
                )
            )

        due = ledger.list_due_deliveries(limit=2)
        assert len(due) == 2


class TestEngineCreateIntent:
    def test_delegates_to_ledger(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        reg = engine.create_intent(_intent())
        assert reg.inserted is True
        assert reg.record.delivery_id == "delivery-1"


class TestEngineClaimNextDelivery:
    def test_claims_pending_delivery(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None
        assert claim.record.state is ManagedThreadDeliveryState.CLAIMED
        assert claim.claim_token is not None
        assert claim.claimed_at is not None
        assert claim.claim_expires_at is not None

    def test_returns_none_when_nothing_due(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is None

    def test_skips_active_claim_and_claims_next_due_record(
        self, tmp_path: Path
    ) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(delivery_id="delivery-1", adapter_key="telegram"))
        engine.create_intent(
            _intent(
                delivery_id="delivery-2",
                managed_turn_id="turn-2",
                surface_key="surface-2",
                adapter_key="telegram",
            )
        )
        ledger = _ledger(tmp_path)
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="active-claim",
            claimed_at="2026-04-18T01:00:00Z",
            claim_expires_at="2026-04-18T01:10:00Z",
        )

        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 2, 0, tzinfo=timezone.utc),
        )
        assert claim is not None
        assert claim.record.delivery_id == "delivery-2"

    def test_abandons_when_budget_exhausted(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))
        ledger = _ledger(tmp_path)
        record = ledger.get_delivery("delivery-1")
        assert record is not None
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            attempt_count=6,
            claimed_at="2026-04-18T00:00:00Z",
            claim_expires_at="2026-04-18T00:05:00Z",
        )

        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is None
        abandoned = ledger.get_delivery("delivery-1")
        assert abandoned is not None
        assert abandoned.state is ManagedThreadDeliveryState.ABANDONED


class TestEngineClaimDelivery:
    def test_claims_specific_delivery(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))
        engine.create_intent(
            _intent(
                delivery_id="delivery-2",
                managed_turn_id="turn-2",
                surface_key="chat-2",
                adapter_key="telegram",
            )
        )

        claim = engine.claim_delivery(
            "delivery-2",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )

        assert claim is not None
        assert claim.record.delivery_id == "delivery-2"
        assert claim.record.state is ManagedThreadDeliveryState.CLAIMED

    def test_returns_none_for_missing_delivery(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)

        claim = engine.claim_delivery(
            "missing",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )

        assert claim is None


class TestEngineRecordAttemptResult:
    def test_success_marks_delivered(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))
        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None

        result = ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.DELIVERED,
        )
        updated = engine.record_attempt_result(
            "delivery-1",
            claim_token=claim.claim_token,
            result=result,
        )
        assert updated is not None
        assert updated.state is ManagedThreadDeliveryState.DELIVERED
        assert updated.delivered_at is not None
        assert updated.claim_token is None

    def test_duplicate_marks_delivered(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))
        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None

        result = ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.DUPLICATE,
        )
        updated = engine.record_attempt_result(
            "delivery-1",
            claim_token=claim.claim_token,
            result=result,
        )
        assert updated is not None
        assert updated.state is ManagedThreadDeliveryState.DELIVERED

    def test_retry_schedules_next_attempt(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))
        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None

        result = ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.RETRY,
            error="transient",
        )
        updated = engine.record_attempt_result(
            "delivery-1",
            claim_token=claim.claim_token,
            result=result,
        )
        assert updated is not None
        assert updated.state is ManagedThreadDeliveryState.RETRY_SCHEDULED
        assert updated.next_attempt_at is not None
        assert updated.last_error == "transient"
        assert updated.claim_token is None

    def test_failed_within_budget_schedules_retry(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))
        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None

        result = ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.FAILED,
            error="oops",
        )
        updated = engine.record_attempt_result(
            "delivery-1",
            claim_token=claim.claim_token,
            result=result,
        )
        assert updated is not None
        assert updated.state is ManagedThreadDeliveryState.RETRY_SCHEDULED

    def test_failed_at_budget_marks_terminal(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))
        ledger = _ledger(tmp_path)
        record = ledger.get_delivery("delivery-1")
        assert record is not None
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            attempt_count=5,
            claim_token="tok-final",
            claimed_at="2026-04-18T01:00:00Z",
            claim_expires_at="2026-04-18T01:05:00Z",
        )

        result = ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.FAILED,
            error="budget_exhausted",
        )
        updated = engine.record_attempt_result(
            "delivery-1",
            claim_token="tok-final",
            result=result,
        )
        assert updated is not None
        assert updated.state is ManagedThreadDeliveryState.FAILED

    def test_wrong_claim_token_returns_none(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))
        engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )

        result = ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.DELIVERED,
        )
        updated = engine.record_attempt_result(
            "delivery-1",
            claim_token="wrong-token",
            result=result,
        )
        assert updated is None


class TestEngineAbandonDelivery:
    def test_abandons_with_detail(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent())

        updated = engine.abandon_delivery("delivery-1", detail="policy_decision")
        assert updated is not None
        assert updated.state is ManagedThreadDeliveryState.ABANDONED
        assert updated.last_error == "policy_decision"


class TestStateMachineTransitions:
    @pytest.mark.parametrize(
        ("from_state", "to_state", "expected"),
        (
            (
                ManagedThreadDeliveryState.PENDING,
                ManagedThreadDeliveryState.CLAIMED,
                True,
            ),
            (
                ManagedThreadDeliveryState.PENDING,
                ManagedThreadDeliveryState.DELIVERING,
                True,
            ),
            (
                ManagedThreadDeliveryState.PENDING,
                ManagedThreadDeliveryState.ABANDONED,
                True,
            ),
            (
                ManagedThreadDeliveryState.CLAIMED,
                ManagedThreadDeliveryState.DELIVERING,
                True,
            ),
            (
                ManagedThreadDeliveryState.CLAIMED,
                ManagedThreadDeliveryState.RETRY_SCHEDULED,
                True,
            ),
            (
                ManagedThreadDeliveryState.DELIVERING,
                ManagedThreadDeliveryState.DELIVERED,
                True,
            ),
            (
                ManagedThreadDeliveryState.DELIVERING,
                ManagedThreadDeliveryState.FAILED,
                True,
            ),
            (
                ManagedThreadDeliveryState.DELIVERING,
                ManagedThreadDeliveryState.RETRY_SCHEDULED,
                True,
            ),
            (
                ManagedThreadDeliveryState.RETRY_SCHEDULED,
                ManagedThreadDeliveryState.CLAIMED,
                True,
            ),
            (
                ManagedThreadDeliveryState.RETRY_SCHEDULED,
                ManagedThreadDeliveryState.ABANDONED,
                True,
            ),
            (
                ManagedThreadDeliveryState.DELIVERED,
                ManagedThreadDeliveryState.PENDING,
                False,
            ),
            (
                ManagedThreadDeliveryState.DELIVERED,
                ManagedThreadDeliveryState.CLAIMED,
                False,
            ),
            (
                ManagedThreadDeliveryState.FAILED,
                ManagedThreadDeliveryState.PENDING,
                False,
            ),
            (
                ManagedThreadDeliveryState.ABANDONED,
                ManagedThreadDeliveryState.CLAIMED,
                False,
            ),
            (
                ManagedThreadDeliveryState.EXPIRED,
                ManagedThreadDeliveryState.PENDING,
                False,
            ),
        ),
    )
    def test_transition_validation(
        self,
        from_state: ManagedThreadDeliveryState,
        to_state: ManagedThreadDeliveryState,
        expected: bool,
    ) -> None:
        assert (
            is_valid_managed_thread_delivery_transition(from_state, to_state)
            is expected
        )

    def test_same_state_is_valid(self) -> None:
        assert (
            is_valid_managed_thread_delivery_transition(
                ManagedThreadDeliveryState.PENDING,
                ManagedThreadDeliveryState.PENDING,
            )
            is True
        )

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        for state in (
            ManagedThreadDeliveryState.DELIVERED,
            ManagedThreadDeliveryState.FAILED,
            ManagedThreadDeliveryState.ABANDONED,
            ManagedThreadDeliveryState.EXPIRED,
        ):
            for target in ManagedThreadDeliveryState:
                if target == state:
                    continue
                assert (
                    is_valid_managed_thread_delivery_transition(state, target) is False
                ), f"{state.value} -> {target.value} should be invalid"


class TestRecoveryPlanning:
    def test_pending_due_is_claimed(self) -> None:
        record = record_from_intent(_intent())
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.CLAIM

    def test_pending_not_before_in_future_is_noop(self) -> None:
        record = record_from_intent(_intent(not_before="2099-01-01T00:00:00Z"))
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.NOOP

    def test_expired_claim_triggers_retry(self) -> None:
        record = record_from_intent(_intent())
        record = record.__class__(
            **{
                **record.__dict__,
                "state": ManagedThreadDeliveryState.CLAIMED,
                "attempt_count": 2,
                "claimed_at": "2026-04-18T01:00:00Z",
                "claim_expires_at": "2026-04-18T01:05:00Z",
            }
        )
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 1, 10, 0, tzinfo=timezone.utc),
            max_attempts=5,
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.RETRY

    def test_expired_claim_with_exhausted_budget_abandons(self) -> None:
        record = record_from_intent(_intent())
        record = record.__class__(
            **{
                **record.__dict__,
                "state": ManagedThreadDeliveryState.CLAIMED,
                "attempt_count": 5,
                "claimed_at": "2026-04-18T01:00:00Z",
                "claim_expires_at": "2026-04-18T01:05:00Z",
            }
        )
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 1, 10, 0, tzinfo=timezone.utc),
            max_attempts=5,
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.ABANDON

    def test_active_claim_is_noop(self) -> None:
        record = record_from_intent(_intent())
        record = record.__class__(
            **{
                **record.__dict__,
                "state": ManagedThreadDeliveryState.CLAIMED,
                "attempt_count": 1,
                "claimed_at": "2026-04-18T01:00:00Z",
                "claim_expires_at": "2026-04-18T01:05:00Z",
            }
        )
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 1, 2, 0, tzinfo=timezone.utc),
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.NOOP

    def test_terminal_state_is_noop(self) -> None:
        for state in (
            ManagedThreadDeliveryState.DELIVERED,
            ManagedThreadDeliveryState.FAILED,
            ManagedThreadDeliveryState.ABANDONED,
            ManagedThreadDeliveryState.EXPIRED,
        ):
            record = record_from_intent(_intent())
            record = record.__class__(**{**record.__dict__, "state": state})
            decision = plan_managed_thread_delivery_recovery(
                record,
                now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
            )
            assert decision.action is ManagedThreadDeliveryRecoveryAction.NOOP


class TestClaimRecovery:
    def test_full_lifecycle_create_claim_deliver(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None

        engine.record_attempt_result(
            "delivery-1",
            claim_token=claim.claim_token,
            result=ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.DELIVERED,
            ),
        )

        ledger = _ledger(tmp_path)
        final = ledger.get_delivery("delivery-1")
        assert final is not None
        assert final.state is ManagedThreadDeliveryState.DELIVERED
        assert final.delivered_at is not None

    def test_claim_then_retry_then_deliver(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        claim1 = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert claim1 is not None

        engine.record_attempt_result(
            "delivery-1",
            claim_token=claim1.claim_token,
            result=ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.RETRY,
                error="network_error",
            ),
        )

        ledger = _ledger(tmp_path)
        after_retry = ledger.get_delivery("delivery-1")
        assert after_retry is not None
        assert after_retry.state is ManagedThreadDeliveryState.RETRY_SCHEDULED

        ledger.patch_delivery(
            "delivery-1",
            next_attempt_at="2026-04-18T01:05:00Z",
        )

        claim2 = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 10, 0, tzinfo=timezone.utc),
        )
        assert claim2 is not None

        engine.record_attempt_result(
            "delivery-1",
            claim_token=claim2.claim_token,
            result=ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.DELIVERED,
            ),
        )

        final = ledger.get_delivery("delivery-1")
        assert final is not None
        assert final.state is ManagedThreadDeliveryState.DELIVERED

    def test_replay_after_restart_recovers_claim(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))
        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None

        ledger = _ledger(tmp_path)
        record = ledger.get_delivery("delivery-1")
        assert record is not None
        assert record.state is ManagedThreadDeliveryState.CLAIMED

        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 1, 10, 0, tzinfo=timezone.utc),
            claim_ttl=timedelta(minutes=5),
            max_attempts=5,
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.RETRY

    def test_idempotent_replay_no_duplicate(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        reg1 = engine.create_intent(_intent())
        assert reg1.inserted is True

        reg2 = engine.create_intent(_intent())
        assert reg2.inserted is False
        assert reg2.record.delivery_id == reg1.record.delivery_id

    def test_concurrent_claim_next_delivery_is_compare_and_set(
        self, tmp_path: Path
    ) -> None:
        hub_root = _hub_root(tmp_path)
        engine = SQLiteManagedThreadDeliveryEngine(
            hub_root,
            durable=False,
            claim_ttl=timedelta(minutes=5),
            retry_backoff=timedelta(minutes=1),
            max_attempts=5,
        )
        engine.create_intent(_intent(delivery_id="delivery-a", adapter_key="telegram"))
        engine.create_intent(
            _intent(
                delivery_id="delivery-b",
                managed_turn_id="turn-b",
                adapter_key="telegram",
            )
        )
        now = datetime(2026, 4, 18, 1, 0, 0, tzinfo=timezone.utc)

        def _claim() -> str | None:
            claim = engine.claim_next_delivery(adapter_key="telegram", now=now)
            return claim.record.delivery_id if claim is not None else None

        delivery_ids: set[str] = set()
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_claim) for _ in range(16)]
            for fut in as_completed(futures):
                got = fut.result()
                if got is not None:
                    delivery_ids.add(got)

        assert delivery_ids == {"delivery-a", "delivery-b"}
