"""Lifecycle scenario coverage: rebinding, timers, lifecycle transitions, retries, suppression.

These tests exercise realistic multi-step scenarios through the pure domain
functions.  They represent the "integration layer" of the domain test pyramid
-- each scenario chains multiple domain operations and asserts that the
combined behavior matches the documented policy.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from codex_autorunner.core.pma_domain.constants import (
    SUBSCRIPTION_STATE_CANCELLED,
)
from codex_autorunner.core.pma_domain.delivery_lifecycle import (
    DeliveryAttemptOutcome,
    DeliveryLifecycleState,
    DeliveryRetryConfig,
    advance_to_delivering,
    advance_to_dispatching,
    resolve_delivery_transition,
)
from codex_autorunner.core.pma_domain.models import PmaSubscription
from codex_autorunner.core.pma_domain.rebinding_policy import (
    RebindingContext,
    RebindingDecision,
    evaluate_rebinding_decision,
)
from codex_autorunner.core.pma_domain.subscription_reducer import (
    TimerFiredEvent,
    TransitionEvent,
    reduce_timer_fired,
    reduce_transition,
)

_DEFAULT_CONFIG = DeliveryRetryConfig(
    max_attempts=5,
    backoff_base=timedelta(minutes=1),
    backoff_multiplier=2.0,
    max_backoff=timedelta(minutes=30),
)


# ---------------------------------------------------------------------------
# Scenario: Full delivery lifecycle with rebinding
# ---------------------------------------------------------------------------


class TestLifecycleWithRebinding:
    def test_dispatch_then_rebind_then_deliver(self) -> None:
        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        assert t1.next_state == DeliveryLifecycleState.DISPATCHED

        rebinding = evaluate_rebinding_decision(
            RebindingContext(
                persisted_surface_kind="discord",
                persisted_surface_key="chan-old",
                persisted_route="bound",
                current_surface_kind="discord",
                current_surface_key="chan-new",
                delivery_state="dispatched",
            )
        )
        assert rebinding.decision == RebindingDecision.REBUILD_ROUTES
        assert rebinding.effective_surface_key == "chan-new"

        t2 = advance_to_delivering(t1.next_state, attempt_number=1)
        assert t2.next_state == DeliveryLifecycleState.DELIVERING

        t3 = resolve_delivery_transition(
            current_state=t2.next_state,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=1,
            retry_config=_DEFAULT_CONFIG,
            domain_metadata={
                "rebound_target": rebinding.effective_surface_key,
            },
        )
        assert t3.next_state == DeliveryLifecycleState.SUCCEEDED
        assert t3.domain_metadata["rebound_target"] == "chan-new"

    def test_dispatch_then_explicit_drift_suppressed(self) -> None:
        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        rebinding = evaluate_rebinding_decision(
            RebindingContext(
                persisted_surface_kind="discord",
                persisted_surface_key="user-chan",
                persisted_route="explicit",
                current_surface_kind="discord",
                current_surface_key="new-chan",
                delivery_state="dispatched",
            )
        )
        assert rebinding.decision == RebindingDecision.SUPPRESS

        t2 = resolve_delivery_transition(
            current_state=t1.next_state,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_DUPLICATE,
            attempt_number=1,
            retry_config=_DEFAULT_CONFIG,
            domain_reason="explicit_binding_drift_suppressed",
        )
        assert t2.next_state == DeliveryLifecycleState.SUPPRESSED


# ---------------------------------------------------------------------------
# Scenario: Retry loop with eventual success
# ---------------------------------------------------------------------------


class TestRetryLoopScenarios:
    def test_three_retries_then_success(self) -> None:
        state = DeliveryLifecycleState.PENDING
        attempt = 0
        max_failures = 3

        while True:
            attempt += 1
            if (
                state == DeliveryLifecycleState.PENDING
                or state == DeliveryLifecycleState.RETRY_SCHEDULED
            ):
                t = advance_to_dispatching(state, attempt_number=attempt)
                state = t.next_state

            t = advance_to_delivering(state, attempt_number=attempt)
            state = t.next_state

            if attempt <= max_failures:
                t = resolve_delivery_transition(
                    current_state=state,
                    outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
                    attempt_number=attempt,
                    retry_config=_DEFAULT_CONFIG,
                )
                assert t.next_state == DeliveryLifecycleState.RETRY_SCHEDULED
                state = DeliveryLifecycleState.RETRY_SCHEDULED
            else:
                t = resolve_delivery_transition(
                    current_state=state,
                    outcome=DeliveryAttemptOutcome.SUCCEEDED,
                    attempt_number=attempt,
                    retry_config=_DEFAULT_CONFIG,
                )
                assert t.next_state == DeliveryLifecycleState.SUCCEEDED
                break

        assert attempt == max_failures + 1

    def test_retry_exhaustion_hits_failed(self) -> None:
        config = DeliveryRetryConfig(max_attempts=3)
        state = DeliveryLifecycleState.PENDING
        for attempt in range(1, 5):
            if (
                state == DeliveryLifecycleState.PENDING
                or state == DeliveryLifecycleState.RETRY_SCHEDULED
            ):
                t = advance_to_dispatching(state, attempt_number=attempt)
                state = t.next_state

            t = advance_to_delivering(state, attempt_number=attempt)
            state = t.next_state

            t = resolve_delivery_transition(
                current_state=state,
                outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
                attempt_number=attempt,
                retry_config=config,
            )
            state = t.next_state

            if state == DeliveryLifecycleState.FAILED:
                assert attempt == 3
                return
            assert state == DeliveryLifecycleState.RETRY_SCHEDULED
            state = DeliveryLifecycleState.RETRY_SCHEDULED

        pytest.fail("Should have failed by max_attempts")


# ---------------------------------------------------------------------------
# Scenario: Subscription-triggered wakeup → delivery lifecycle
# ---------------------------------------------------------------------------


class TestSubscriptionWakeupLifecycle:
    def test_subscription_match_triggers_wakeup_then_delivery(self) -> None:
        sub = PmaSubscription(
            subscription_id="sub-1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            event_types=("managed_thread_completed",),
            thread_id="thread-watched",
            metadata={
                "delivery_target": {
                    "surface_kind": "discord",
                    "surface_key": "chan-1",
                }
            },
        )
        event = TransitionEvent(
            event_type="managed_thread_completed",
            thread_id="thread-watched",
            from_state="running",
            to_state="completed",
            transition_id="thread-watched:completed",
        )
        r = reduce_transition([sub], frozenset(), event)
        assert r.created == 1

        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        t2 = advance_to_delivering(t1.next_state, attempt_number=1)
        t3 = resolve_delivery_transition(
            current_state=t2.next_state,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=1,
            retry_config=_DEFAULT_CONFIG,
        )
        assert t3.next_state == DeliveryLifecycleState.SUCCEEDED

    def test_subscription_exhaustion_cancels_after_max(self) -> None:
        sub = PmaSubscription(
            subscription_id="sub-once",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            max_matches=1,
            match_count=0,
        )
        event = TransitionEvent(transition_id="t1")
        r = reduce_transition([sub], frozenset(), event)
        assert r.created == 1
        assert r.subscriptions[0].state == SUBSCRIPTION_STATE_CANCELLED

        event2 = TransitionEvent(transition_id="t2")
        r2 = reduce_transition(list(r.subscriptions), frozenset(), event2)
        assert r2.matched == 0
        assert r2.created == 0


# ---------------------------------------------------------------------------
# Scenario: Timer-fired → wakeup → lifecycle
# ---------------------------------------------------------------------------


class TestTimerFiredLifecycle:
    def test_watchdog_timer_triggers_delivery_lifecycle(self) -> None:
        event = TimerFiredEvent(
            timer_id="wd-1",
            timer_type="watchdog",
            fired_at="2026-01-15T12:05:00Z",
            thread_id="thread-1",
            subscription_id="sub-wd",
        )
        result = reduce_timer_fired(event)
        assert result.wakeup_intent.source == "timer"
        assert result.wakeup_intent.subscription_id == "sub-wd"

        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        t2 = advance_to_delivering(t1.next_state, attempt_number=1)
        t3 = resolve_delivery_transition(
            current_state=t2.next_state,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=1,
            retry_config=_DEFAULT_CONFIG,
        )
        assert t3.next_state == DeliveryLifecycleState.SUCCEEDED

    def test_timer_wakeup_can_be_suppressed(self) -> None:
        event = TimerFiredEvent(
            timer_id="wd-1",
            timer_type="watchdog",
            fired_at="2026-01-15T12:05:00Z",
        )
        reduce_timer_fired(event)

        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        t2 = resolve_delivery_transition(
            current_state=t1.next_state,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_DUPLICATE,
            attempt_number=1,
            retry_config=_DEFAULT_CONFIG,
        )
        assert t2.next_state == DeliveryLifecycleState.SUPPRESSED


# ---------------------------------------------------------------------------
# Scenario: Suppression chains
# ---------------------------------------------------------------------------


class TestSuppressionChainScenarios:
    def test_abandoned_after_policy_check(self) -> None:
        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        t2 = resolve_delivery_transition(
            current_state=t1.next_state,
            outcome=DeliveryAttemptOutcome.ABANDONED_BY_POLICY,
            attempt_number=1,
            retry_config=_DEFAULT_CONFIG,
            domain_reason="safety_limit_exceeded",
        )
        assert t2.next_state == DeliveryLifecycleState.ABANDONED
        assert t2.domain_reason == "safety_limit_exceeded"

    def test_noop_suppression_chain(self) -> None:
        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        t2 = advance_to_delivering(t1.next_state, attempt_number=1)
        t3 = resolve_delivery_transition(
            current_state=t2.next_state,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_NOOP,
            attempt_number=1,
            retry_config=_DEFAULT_CONFIG,
        )
        assert t3.next_state == DeliveryLifecycleState.SUPPRESSED
        assert "noop" in t3.domain_reason

    def test_duplicate_suppression_chain(self) -> None:
        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        t2 = advance_to_delivering(t1.next_state, attempt_number=1)
        t3 = resolve_delivery_transition(
            current_state=t2.next_state,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_DUPLICATE,
            attempt_number=1,
            retry_config=_DEFAULT_CONFIG,
        )
        assert t3.next_state == DeliveryLifecycleState.SUPPRESSED
        assert "duplicate" in t3.domain_reason


# ---------------------------------------------------------------------------
# Scenario: Rebinding across delivery states
# ---------------------------------------------------------------------------


class TestRebindingAcrossStates:
    @pytest.mark.parametrize(
        "delivery_state",
        ["pending", "dispatched", "delivering", "retry_scheduled"],
    )
    def test_binding_drift_in_active_states_triggers_decision(
        self, delivery_state: str
    ) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-old",
            persisted_route="bound",
            current_surface_kind="discord",
            current_surface_key="chan-new",
            delivery_state=delivery_state,
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision in (
            RebindingDecision.REBUILD_ROUTES,
            RebindingDecision.KEEP_ORIGINAL,
            RebindingDecision.SUPPRESS,
        )

    def test_cross_surface_rebuild_in_flight(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="discord-chan",
            persisted_route="bound",
            current_surface_kind="telegram",
            current_surface_key="telegram-grp",
            delivery_state="dispatched",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.REBUILD_ROUTES
        assert result.effective_surface_kind == "telegram"
        assert result.effective_surface_key == "telegram-grp"


# ---------------------------------------------------------------------------
# Scenario: Multiple subscriptions with interleaved events
# ---------------------------------------------------------------------------


class TestMultipleSubscriptionsInterleaved:
    def test_two_subscriptions_separate_events(self) -> None:
        sub_a = PmaSubscription(
            subscription_id="sub-a",
            created_at="",
            updated_at="",
            event_types=("flow_completed",),
            repo_id="repo-1",
        )
        sub_b = PmaSubscription(
            subscription_id="sub-b",
            created_at="",
            updated_at="",
            event_types=("flow_failed",),
            repo_id="repo-2",
        )

        event_a = TransitionEvent(
            event_type="flow_completed",
            repo_id="repo-1",
            transition_id="t-a",
        )
        r1 = reduce_transition([sub_a, sub_b], frozenset(), event_a)
        assert r1.matched == 1
        assert r1.wakeup_intents[0].subscription_id == "sub-a"

        event_b = TransitionEvent(
            event_type="flow_failed",
            repo_id="repo-2",
            transition_id="t-b",
        )
        existing_keys = frozenset(wi.idempotency_key for wi in r1.wakeup_intents)
        r2 = reduce_transition(list(r1.subscriptions), existing_keys, event_b)
        assert r2.matched == 1
        assert r2.wakeup_intents[0].subscription_id == "sub-b"

    def test_wildcard_subscription_matches_both_events(self) -> None:
        sub_wild = PmaSubscription(
            subscription_id="sub-wild",
            created_at="",
            updated_at="",
            event_types=(),
        )
        event1 = TransitionEvent(
            event_type="flow_completed",
            transition_id="t1",
        )
        event2 = TransitionEvent(
            event_type="flow_failed",
            transition_id="t2",
        )
        r1 = reduce_transition([sub_wild], frozenset(), event1)
        existing = frozenset(wi.idempotency_key for wi in r1.wakeup_intents)
        r2 = reduce_transition(list(r1.subscriptions), existing, event2)
        assert r1.created == 1
        assert r2.created == 1

    def test_notify_once_subscription_exhausted_after_first_match(self) -> None:
        sub = PmaSubscription(
            subscription_id="sub-once",
            created_at="",
            updated_at="",
            max_matches=1,
            match_count=0,
        )
        event1 = TransitionEvent(transition_id="t1")
        r1 = reduce_transition([sub], frozenset(), event1)
        assert r1.created == 1
        assert r1.subscriptions[0].state == SUBSCRIPTION_STATE_CANCELLED

        event2 = TransitionEvent(transition_id="t2")
        r2 = reduce_transition(list(r1.subscriptions), frozenset(), event2)
        assert r2.matched == 0
