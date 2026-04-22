"""Regression tests for replay, lease recovery, idempotency, and backoff.

These tests cover the durability guarantees required by the spec:
- Pending or failed delivery records can be replayed after restart.
- Claimed/in-flight records are recoverable after worker death or cancellation.
- Retry behavior is bounded and observable.
- Duplicate suppression via idempotency.
- Abandonment after budget exhaustion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryRecoveryAction,
    ManagedThreadDeliveryRecoverySweepResult,
    ManagedThreadDeliveryState,
    SQLiteManagedThreadDeliveryEngine,
    SQLiteManagedThreadDeliveryLedger,
    initialize_orchestration_sqlite,
    plan_managed_thread_delivery_recovery,
    record_from_intent,
)
from codex_autorunner.core.orchestration.managed_thread_delivery import (
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryIntent,
    ManagedThreadDeliveryTarget,
    build_managed_thread_delivery_idempotency_key,
)
from codex_autorunner.core.orchestration.managed_thread_delivery_ledger import (
    _compute_next_attempt_at,
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


def _engine(
    tmp_path: Path,
    *,
    retry_backoff_seconds: int = 60,
    max_attempts: int = 5,
    backoff_multiplier: float = 2.0,
    max_backoff_minutes: int = 30,
) -> SQLiteManagedThreadDeliveryEngine:
    return SQLiteManagedThreadDeliveryEngine(
        _hub_root(tmp_path),
        durable=False,
        claim_ttl=timedelta(minutes=5),
        retry_backoff=timedelta(seconds=retry_backoff_seconds),
        max_attempts=max_attempts,
        backoff_multiplier=backoff_multiplier,
        max_backoff=timedelta(minutes=max_backoff_minutes),
    )


def _ledger(tmp_path: Path) -> SQLiteManagedThreadDeliveryLedger:
    return SQLiteManagedThreadDeliveryLedger(_hub_root(tmp_path), durable=False)


class TestStartupReplay:
    def test_pending_records_replayed_after_restart(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert sweep.due_pending == 1
        assert sweep.recovered_claims == 0

        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None
        assert claim.record.delivery_id == "delivery-1"

    def test_multiple_pending_records_replayed_in_order(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        for i in range(3):
            engine.create_intent(
                _intent(
                    delivery_id=f"d{i}",
                    managed_turn_id=f"turn-{i}",
                    surface_key=f"chat-{i}",
                    adapter_key="telegram",
                )
            )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert sweep.due_pending == 3

        claimed_ids: list[str] = []
        for _ in range(3):
            claim = engine.claim_next_delivery(
                adapter_key="telegram",
                now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
            )
            assert claim is not None
            claimed_ids.append(claim.record.delivery_id)
            engine.record_attempt_result(
                claim.record.delivery_id,
                claim_token=claim.claim_token,
                result=ManagedThreadDeliveryAttemptResult(
                    outcome=ManagedThreadDeliveryOutcome.DELIVERED,
                ),
            )

        assert claimed_ids == ["d0", "d1", "d2"]

    def test_retry_scheduled_records_replayed_after_restart(
        self,
        tmp_path: Path,
    ) -> None:
        engine = _engine(tmp_path)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None
        engine.record_attempt_result(
            "delivery-1",
            claim_token=claim.claim_token,
            result=ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.RETRY,
                error="transient_failure",
            ),
        )

        ledger.patch_delivery(
            "delivery-1",
            next_attempt_at="2026-04-18T12:30:00Z",
        )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 13, 0, 0, tzinfo=timezone.utc),
        )
        assert sweep.due_retries >= 1

        claim2 = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 13, 0, 0, tzinfo=timezone.utc),
        )
        assert claim2 is not None
        assert claim2.record.delivery_id == "delivery-1"

    def test_records_with_future_not_before_skipped_on_replay(
        self,
        tmp_path: Path,
    ) -> None:
        engine = _engine(tmp_path)
        engine.create_intent(
            _intent(
                delivery_id="d-future",
                adapter_key="telegram",
                not_before="2099-06-01T00:00:00Z",
            )
        )
        engine.create_intent(
            _intent(
                delivery_id="d-ready",
                managed_turn_id="turn-ready",
                surface_key="chat-ready",
                adapter_key="telegram",
            )
        )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert sweep.due_pending == 1

        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None
        assert claim.record.delivery_id == "d-ready"


class TestLeaseRecovery:
    def test_expired_claim_recovered_on_sweep(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="dead-worker-token",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
            attempt_count=1,
        )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
        )
        assert sweep.recovered_claims == 1

        record = ledger.get_delivery("delivery-1")
        assert record is not None
        assert record.state is ManagedThreadDeliveryState.RETRY_SCHEDULED
        assert record.claim_token is None
        assert record.next_attempt_at is not None

    def test_expired_delivering_state_recovered(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-1",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
            attempt_count=1,
        )
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.DELIVERING,
        )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
        )
        assert sweep.recovered_claims == 1

        record = ledger.get_delivery("delivery-1")
        assert record is not None
        assert record.state is ManagedThreadDeliveryState.RETRY_SCHEDULED

    def test_expired_claim_abandoned_when_budget_exhausted(
        self,
        tmp_path: Path,
    ) -> None:
        engine = _engine(tmp_path, max_attempts=3)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="dead-token",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
            attempt_count=3,
        )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
        )
        assert sweep.abandoned_exhausted == 1
        assert sweep.recovered_claims == 0

        record = ledger.get_delivery("delivery-1")
        assert record is not None
        assert record.state is ManagedThreadDeliveryState.ABANDONED
        assert record.claim_token is None

    def test_active_claim_not_recovered(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="active-token",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
            attempt_count=1,
        )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 2, 0, tzinfo=timezone.utc),
        )
        assert sweep.recovered_claims == 0
        assert sweep.abandoned_exhausted == 0

        record = ledger.get_delivery("delivery-1")
        assert record is not None
        assert record.state is ManagedThreadDeliveryState.CLAIMED

    def test_recovery_then_deliver_end_to_end(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="dead-token",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
            attempt_count=1,
        )

        engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
        )

        ledger.patch_delivery(
            "delivery-1",
            next_attempt_at="2026-04-18T12:00:00Z",
        )

        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
        )
        assert claim is not None

        engine.record_attempt_result(
            "delivery-1",
            claim_token=claim.claim_token,
            result=ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.DELIVERED,
            ),
        )

        final = ledger.get_delivery("delivery-1")
        assert final is not None
        assert final.state is ManagedThreadDeliveryState.DELIVERED
        assert final.attempt_count == 2

    def test_mixed_states_recovery_sweep(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, max_attempts=3)
        ledger = _ledger(tmp_path)

        engine.create_intent(
            _intent(delivery_id="d-pending", adapter_key="telegram"),
        )
        engine.create_intent(
            _intent(
                delivery_id="d-expired-claim",
                managed_turn_id="turn-2",
                surface_key="chat-2",
                adapter_key="telegram",
            ),
        )
        engine.create_intent(
            _intent(
                delivery_id="d-exhausted",
                managed_turn_id="turn-3",
                surface_key="chat-3",
                adapter_key="telegram",
            ),
        )

        ledger.patch_delivery(
            "d-expired-claim",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-expired",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
            attempt_count=1,
        )
        ledger.patch_delivery(
            "d-exhausted",
            state=ManagedThreadDeliveryState.DELIVERING,
            claim_token="tok-exhausted",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
            attempt_count=3,
        )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
        )
        assert sweep.due_pending == 1
        assert sweep.recovered_claims == 1
        assert sweep.abandoned_exhausted == 1
        assert sweep.total_scanned >= 3


class TestExponentialBackoff:
    def test_backoff_increases_exponentially(self) -> None:
        base = timedelta(seconds=60)
        delays: list[float] = []
        for attempt in range(5):
            ts = _compute_next_attempt_at(
                attempt + 1,
                base,
                backoff_multiplier=2.0,
                max_backoff=timedelta(hours=1),
            )
            parsed = datetime.fromisoformat(ts)
            delays.append(parsed.timestamp())

        gaps = [delays[i + 1] - delays[i] for i in range(len(delays) - 1)]
        for i in range(1, len(gaps)):
            assert (
                gaps[i] >= gaps[i - 1] * 1.8
            ), f"Gap {i} ({gaps[i]:.0f}s) should be ~2x gap {i-1} ({gaps[i-1]:.0f}s)"

    def test_backoff_capped_at_max(self) -> None:
        base = timedelta(seconds=60)
        max_backoff = timedelta(minutes=5)

        ts_low = _compute_next_attempt_at(
            1, base, backoff_multiplier=2.0, max_backoff=max_backoff
        )
        ts_high = _compute_next_attempt_at(
            10, base, backoff_multiplier=2.0, max_backoff=max_backoff
        )

        parsed_low = datetime.fromisoformat(ts_low)
        parsed_high = datetime.fromisoformat(ts_high)
        diff = parsed_high.timestamp() - parsed_low.timestamp()
        assert diff <= max_backoff.total_seconds() + 1.0

    def test_backoff_observable_in_record(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, retry_backoff_seconds=60)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        claim1 = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert claim1 is not None
        engine.record_attempt_result(
            "delivery-1",
            claim_token=claim1.claim_token,
            result=ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.RETRY,
                error="first_failure",
            ),
        )

        after_first = ledger.get_delivery("delivery-1")
        assert after_first is not None
        assert after_first.next_attempt_at is not None
        assert after_first.attempt_count == 1

        ledger.patch_delivery(
            "delivery-1",
            next_attempt_at="2026-04-18T12:01:00+00:00",
        )
        claim2 = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 2, 0, tzinfo=timezone.utc),
        )
        assert claim2 is not None
        engine.record_attempt_result(
            "delivery-1",
            claim_token=claim2.claim_token,
            result=ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.RETRY,
                error="second_failure",
            ),
        )

        after_second = ledger.get_delivery("delivery-1")
        assert after_second is not None
        assert after_second.attempt_count == 2
        assert after_second.last_error == "second_failure"

    def test_bounded_retry_exhaustion(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, max_attempts=3)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        base_now = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
        for attempt in range(3):
            future_now = base_now + timedelta(hours=attempt + 1)
            claim = engine.claim_delivery("delivery-1", now=future_now)
            if claim is None:
                break
            engine.record_attempt_result(
                "delivery-1",
                claim_token=claim.claim_token,
                result=ManagedThreadDeliveryAttemptResult(
                    outcome=ManagedThreadDeliveryOutcome.FAILED,
                    error=f"attempt_{attempt + 1}",
                ),
            )
            if attempt < 2:
                ledger.patch_delivery(
                    "delivery-1",
                    next_attempt_at=(future_now + timedelta(minutes=1)).isoformat(),
                )

        final = ledger.get_delivery("delivery-1")
        assert final is not None
        assert final.state is ManagedThreadDeliveryState.FAILED
        assert final.attempt_count == 3


class TestIdempotentReplay:
    def test_duplicate_intent_returns_existing_record(
        self,
        tmp_path: Path,
    ) -> None:
        engine = _engine(tmp_path)
        intent = _intent()
        reg1 = engine.create_intent(intent)
        assert reg1.inserted is True

        reg2 = engine.create_intent(intent)
        assert reg2.inserted is False
        assert reg2.record.delivery_id == reg1.record.delivery_id
        assert reg2.record.state is ManagedThreadDeliveryState.PENDING

    def test_replay_after_delivery_is_noop(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        intent = _intent()
        reg = engine.create_intent(intent)
        assert reg.inserted

        claim = engine.claim_delivery(
            "delivery-1",
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert claim is not None
        engine.record_attempt_result(
            "delivery-1",
            claim_token=claim.claim_token,
            result=ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.DELIVERED,
            ),
        )

        reg2 = engine.create_intent(intent)
        assert reg2.inserted is False
        assert reg2.record.state is ManagedThreadDeliveryState.DELIVERED

        claim2 = engine.claim_delivery(
            "delivery-1",
            now=datetime(2026, 4, 18, 13, 0, 0, tzinfo=timezone.utc),
        )
        assert claim2 is None

    def test_replay_after_abandonment_returns_abandoned_record(
        self,
        tmp_path: Path,
    ) -> None:
        engine = _engine(tmp_path)
        intent = _intent()
        reg = engine.create_intent(intent)
        assert reg.inserted

        engine.abandon_delivery("delivery-1", detail="policy")

        reg2 = engine.create_intent(intent)
        assert reg2.inserted is False
        assert reg2.record.state is ManagedThreadDeliveryState.ABANDONED

    def test_idempotency_key_distinguishes_different_surfaces(
        self,
        tmp_path: Path,
    ) -> None:
        engine = _engine(tmp_path)
        reg1 = engine.create_intent(
            _intent(
                delivery_id="d-tg",
                surface_kind="telegram",
                surface_key="chat-1",
            )
        )
        reg2 = engine.create_intent(
            _intent(
                delivery_id="d-dc",
                surface_kind="discord",
                surface_key="channel-1",
            )
        )
        assert reg1.inserted is True
        assert reg2.inserted is True
        assert reg1.record.delivery_id != reg2.record.delivery_id
        assert reg1.record.idempotency_key != reg2.record.idempotency_key


class TestAbandonmentPaths:
    def test_claim_next_abandons_exhausted_record(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, max_attempts=2)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-1",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
            attempt_count=2,
        )

        claim = engine.claim_next_delivery(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
        )
        assert claim is None

        record = ledger.get_delivery("delivery-1")
        assert record is not None
        assert record.state is ManagedThreadDeliveryState.ABANDONED

    def test_recovery_sweep_abandons_exhausted_delivering_records(
        self,
        tmp_path: Path,
    ) -> None:
        engine = _engine(tmp_path, max_attempts=2)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-1",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
            attempt_count=1,
        )
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.DELIVERING,
        )
        ledger.patch_delivery(
            "delivery-1",
            attempt_count=2,
            validate_transition=False,
        )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
        )
        assert sweep.abandoned_exhausted == 1

        record = ledger.get_delivery("delivery-1")
        assert record is not None
        assert record.state is ManagedThreadDeliveryState.ABANDONED

    def test_explicit_abandon_clears_claim(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        ledger = _ledger(tmp_path)
        engine.create_intent(_intent(adapter_key="telegram"))

        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-active",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
        )

        engine.abandon_delivery("delivery-1", detail="manual_abandon")

        record = ledger.get_delivery("delivery-1")
        assert record is not None
        assert record.state is ManagedThreadDeliveryState.ABANDONED
        assert record.claim_token is None
        assert record.last_error == "manual_abandon"

    def test_terminal_states_skipped_by_sweep(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        ledger = _ledger(tmp_path)

        for state, idx in [
            (ManagedThreadDeliveryState.DELIVERED, 0),
            (ManagedThreadDeliveryState.FAILED, 1),
            (ManagedThreadDeliveryState.ABANDONED, 2),
        ]:
            delivery_id = f"d-{state.value}"
            engine.create_intent(
                _intent(
                    delivery_id=delivery_id,
                    managed_turn_id=f"turn-{idx}",
                    surface_key=f"chat-{idx}",
                    adapter_key="telegram",
                )
            )
            ledger.patch_delivery(
                delivery_id,
                state=state,
                validate_transition=False,
            )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert sweep.due_pending == 0
        assert sweep.recovered_claims == 0
        assert sweep.abandoned_exhausted == 0


class TestLedgerExpiredClaimQueries:
    def test_list_records_with_expired_claims_finds_claimed(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent())
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-1",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
        )

        expired = ledger.list_records_with_expired_claims(
            now="2026-04-18T12:10:00Z",
        )
        assert len(expired) == 1
        assert expired[0].delivery_id == "delivery-1"

    def test_list_records_with_expired_claims_excludes_active(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent())
        ledger.patch_delivery(
            "delivery-1",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-1",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
        )

        expired = ledger.list_records_with_expired_claims(
            now="2026-04-18T12:02:00Z",
        )
        assert len(expired) == 0

    def test_list_all_non_terminal_records(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent(delivery_id="d-pending"))
        ledger.register_intent(
            _intent(
                delivery_id="d-terminal",
                managed_turn_id="turn-2",
                surface_key="chat-2",
            )
        )
        ledger.register_intent(
            _intent(
                delivery_id="d-direct",
                managed_turn_id="turn-3",
                surface_key="chat-3",
            )
        )
        ledger.patch_delivery(
            "d-terminal",
            state=ManagedThreadDeliveryState.DELIVERED,
            validate_transition=False,
        )
        ledger.patch_delivery(
            "d-direct",
            state=ManagedThreadDeliveryState.DIRECT_SURFACE_DELIVERED,
            validate_transition=False,
        )

        non_terminal = ledger.list_all_non_terminal_records()
        assert len(non_terminal) == 1
        assert non_terminal[0].delivery_id == "d-pending"

    def test_list_records_with_expired_claims_filters_by_adapter(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = _ledger(tmp_path)
        ledger.register_intent(_intent(adapter_key="telegram"))
        ledger.register_intent(
            _intent(
                delivery_id="d-discord",
                managed_turn_id="turn-2",
                surface_kind="discord",
                surface_key="chan-1",
                adapter_key="discord",
            )
        )
        for did in ("delivery-1", "d-discord"):
            ledger.patch_delivery(
                did,
                state=ManagedThreadDeliveryState.CLAIMED,
                claim_token=f"tok-{did}",
                claimed_at="2026-04-18T12:00:00Z",
                claim_expires_at="2026-04-18T12:05:00Z",
            )

        expired = ledger.list_records_with_expired_claims(
            adapter_key="telegram",
            now="2026-04-18T12:10:00Z",
        )
        assert len(expired) == 1
        assert expired[0].delivery_id == "delivery-1"


class TestRecoveryPlanningEdgeCases:
    def test_delivering_state_with_expired_claim_retries(self) -> None:
        record = record_from_intent(_intent())
        record = record.__class__(
            **{
                **record.__dict__,
                "state": ManagedThreadDeliveryState.DELIVERING,
                "attempt_count": 2,
                "claimed_at": "2026-04-18T12:00:00Z",
                "claim_expires_at": "2026-04-18T12:05:00Z",
            }
        )
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
            max_attempts=5,
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.RETRY

    def test_delivering_state_with_exhausted_budget_abandons(self) -> None:
        record = record_from_intent(_intent())
        record = record.__class__(
            **{
                **record.__dict__,
                "state": ManagedThreadDeliveryState.DELIVERING,
                "attempt_count": 5,
                "claimed_at": "2026-04-18T12:00:00Z",
                "claim_expires_at": "2026-04-18T12:05:00Z",
            }
        )
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
            max_attempts=5,
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.ABANDON
        assert "budget" in decision.reason

    def test_claim_with_no_expiry_derives_from_claimed_at(self) -> None:
        record = record_from_intent(_intent())
        record = record.__class__(
            **{
                **record.__dict__,
                "state": ManagedThreadDeliveryState.CLAIMED,
                "attempt_count": 1,
                "claimed_at": "2026-04-18T12:00:00Z",
                "claim_expires_at": None,
            }
        )
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
            claim_ttl=timedelta(minutes=5),
            max_attempts=5,
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.RETRY
        assert "claim_expired" in decision.reason

    def test_retry_scheduled_due_in_future_is_noop(self) -> None:
        record = record_from_intent(_intent())
        record = record.__class__(
            **{
                **record.__dict__,
                "state": ManagedThreadDeliveryState.RETRY_SCHEDULED,
                "next_attempt_at": "2099-06-01T00:00:00Z",
            }
        )
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.NOOP

    def test_failed_state_is_terminal_and_not_retried(self) -> None:
        record = record_from_intent(_intent())
        record = record.__class__(
            **{
                **record.__dict__,
                "state": ManagedThreadDeliveryState.FAILED,
                "attempt_count": 2,
                "updated_at": "2026-04-18T11:00:00Z",
            }
        )
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
            retry_backoff=timedelta(minutes=1),
            max_attempts=5,
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.NOOP
        assert decision.reason == "terminal_state"

    def test_retry_scheduled_past_due_triggers_claim(self) -> None:
        record = record_from_intent(_intent())
        record = record.__class__(
            **{
                **record.__dict__,
                "state": ManagedThreadDeliveryState.RETRY_SCHEDULED,
                "attempt_count": 2,
                "next_attempt_at": "2026-04-18T11:00:00Z",
            }
        )
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
            max_attempts=5,
        )
        assert decision.action is ManagedThreadDeliveryRecoveryAction.CLAIM


class TestRecoverySweepResultStructure:
    def test_empty_sweep_returns_zeros(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert isinstance(sweep, ManagedThreadDeliveryRecoverySweepResult)
        assert sweep.recovered_claims == 0
        assert sweep.abandoned_exhausted == 0
        assert sweep.due_pending == 0
        assert sweep.due_retries == 0
        assert sweep.total_scanned == 0

    def test_sweep_is_observable(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, max_attempts=3)
        ledger = _ledger(tmp_path)
        engine.create_intent(
            _intent(delivery_id="d-pending", adapter_key="telegram"),
        )
        engine.create_intent(
            _intent(
                delivery_id="d-expired",
                managed_turn_id="turn-2",
                surface_key="chat-2",
                adapter_key="telegram",
            ),
        )

        ledger.patch_delivery(
            "d-expired",
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token="tok-1",
            claimed_at="2026-04-18T12:00:00Z",
            claim_expires_at="2026-04-18T12:05:00Z",
            attempt_count=1,
        )

        sweep = engine.recovery_sweep(
            adapter_key="telegram",
            now=datetime(2026, 4, 18, 12, 30, 0, tzinfo=timezone.utc),
        )
        assert sweep.due_pending == 1
        assert sweep.recovered_claims == 1
        assert sweep.total_scanned >= 2
