"""Tests for delivery lifecycle transitions, retry policy, and rebinding semantics.

Covers:
- All valid and invalid state transitions
- Retry behavior with backoff and exhaustion
- Adapter failure -> retry escalation
- Suppression semantics (duplicate, noop)
- Delivery success/failure state transitions
- Rebinding after persisted dispatch decisions
- Domain language recording in transitions
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from codex_autorunner.core.pma_domain.delivery_lifecycle import (
    DELIVERY_LIFECYCLE_TERMINAL_STATES,
    DELIVERY_LIFECYCLE_TRANSITIONS,
    DeliveryAttemptOutcome,
    DeliveryLifecycleState,
    DeliveryRetryConfig,
    DeliveryTransition,
    advance_to_delivering,
    advance_to_dispatching,
    is_terminal_delivery_state,
    is_valid_delivery_transition,
    resolve_delivery_transition,
)
from codex_autorunner.core.pma_domain.rebinding_policy import (
    RebindingContext,
    RebindingDecision,
    RebindingResult,
    evaluate_rebinding_decision,
)

_DEFAULT_RETRY_CONFIG = DeliveryRetryConfig(
    max_attempts=5,
    backoff_base=timedelta(minutes=1),
    backoff_multiplier=2.0,
    max_backoff=timedelta(minutes=30),
)


# ---------------------------------------------------------------------------
# State machine transition validation
# ---------------------------------------------------------------------------


class TestStateTransitionValidation:
    @pytest.mark.parametrize(
        ("current", "target", "expected"),
        (
            (DeliveryLifecycleState.PENDING, DeliveryLifecycleState.DISPATCHED, True),
            (DeliveryLifecycleState.PENDING, DeliveryLifecycleState.DELIVERING, True),
            (DeliveryLifecycleState.PENDING, DeliveryLifecycleState.SUPPRESSED, True),
            (DeliveryLifecycleState.PENDING, DeliveryLifecycleState.ABANDONED, True),
            (
                DeliveryLifecycleState.DISPATCHED,
                DeliveryLifecycleState.DELIVERING,
                True,
            ),
            (
                DeliveryLifecycleState.DISPATCHED,
                DeliveryLifecycleState.RETRY_SCHEDULED,
                True,
            ),
            (
                DeliveryLifecycleState.DISPATCHED,
                DeliveryLifecycleState.SUPPRESSED,
                True,
            ),
            (DeliveryLifecycleState.DELIVERING, DeliveryLifecycleState.SUCCEEDED, True),
            (
                DeliveryLifecycleState.DELIVERING,
                DeliveryLifecycleState.RETRY_SCHEDULED,
                True,
            ),
            (DeliveryLifecycleState.DELIVERING, DeliveryLifecycleState.FAILED, True),
            (DeliveryLifecycleState.DELIVERING, DeliveryLifecycleState.ABANDONED, True),
            (
                DeliveryLifecycleState.RETRY_SCHEDULED,
                DeliveryLifecycleState.DISPATCHED,
                True,
            ),
            (
                DeliveryLifecycleState.RETRY_SCHEDULED,
                DeliveryLifecycleState.DELIVERING,
                True,
            ),
            (
                DeliveryLifecycleState.RETRY_SCHEDULED,
                DeliveryLifecycleState.SUPPRESSED,
                True,
            ),
            (
                DeliveryLifecycleState.RETRY_SCHEDULED,
                DeliveryLifecycleState.ABANDONED,
                True,
            ),
        ),
    )
    def test_valid_transitions(
        self,
        current: DeliveryLifecycleState,
        target: DeliveryLifecycleState,
        expected: bool,
    ) -> None:
        assert is_valid_delivery_transition(current, target) is expected

    @pytest.mark.parametrize(
        ("current", "target"),
        (
            (DeliveryLifecycleState.SUCCEEDED, DeliveryLifecycleState.PENDING),
            (DeliveryLifecycleState.SUCCEEDED, DeliveryLifecycleState.DELIVERING),
            (DeliveryLifecycleState.FAILED, DeliveryLifecycleState.PENDING),
            (DeliveryLifecycleState.FAILED, DeliveryLifecycleState.DISPATCHED),
            (DeliveryLifecycleState.SUPPRESSED, DeliveryLifecycleState.DELIVERING),
            (DeliveryLifecycleState.ABANDONED, DeliveryLifecycleState.DISPATCHED),
            (DeliveryLifecycleState.SUCCEEDED, DeliveryLifecycleState.RETRY_SCHEDULED),
            (DeliveryLifecycleState.FAILED, DeliveryLifecycleState.RETRY_SCHEDULED),
        ),
    )
    def test_terminal_states_reject_outgoing(
        self,
        current: DeliveryLifecycleState,
        target: DeliveryLifecycleState,
    ) -> None:
        assert is_valid_delivery_transition(current, target) is False

    def test_same_state_is_valid(self) -> None:
        assert (
            is_valid_delivery_transition(
                DeliveryLifecycleState.PENDING,
                DeliveryLifecycleState.PENDING,
            )
            is True
        )

    def test_all_terminal_states_have_empty_transitions(self) -> None:
        for state in DELIVERY_LIFECYCLE_TERMINAL_STATES:
            assert DELIVERY_LIFECYCLE_TRANSITIONS[state] == frozenset()

    def test_terminal_states_are_detected(self) -> None:
        for state in DELIVERY_LIFECYCLE_TERMINAL_STATES:
            assert is_terminal_delivery_state(state) is True
        non_terminal = {
            DeliveryLifecycleState.PENDING,
            DeliveryLifecycleState.DISPATCHED,
            DeliveryLifecycleState.DELIVERING,
            DeliveryLifecycleState.RETRY_SCHEDULED,
        }
        for state in non_terminal:
            assert is_terminal_delivery_state(state) is False

    def test_pending_cannot_transition_to_succeeded_directly(self) -> None:
        assert (
            is_valid_delivery_transition(
                DeliveryLifecycleState.PENDING,
                DeliveryLifecycleState.SUCCEEDED,
            )
            is False
        )

    def test_pending_cannot_transition_to_failed_directly(self) -> None:
        assert (
            is_valid_delivery_transition(
                DeliveryLifecycleState.PENDING,
                DeliveryLifecycleState.FAILED,
            )
            is False
        )

    def test_dispatched_cannot_transition_to_succeeded_directly(self) -> None:
        assert (
            is_valid_delivery_transition(
                DeliveryLifecycleState.DISPATCHED,
                DeliveryLifecycleState.SUCCEEDED,
            )
            is False
        )


# ---------------------------------------------------------------------------
# resolve_delivery_transition
# ---------------------------------------------------------------------------


class TestResolveDeliveryTransition:
    def test_succeeded_outcome(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t.next_state == DeliveryLifecycleState.SUCCEEDED
        assert t.outcome == DeliveryAttemptOutcome.SUCCEEDED
        assert t.domain_reason == "delivery_succeeded"
        assert t.retry_at is None

    def test_succeeded_with_custom_reason(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
            domain_reason="adapter_confirmed_delivery",
        )
        assert t.domain_reason == "adapter_confirmed_delivery"

    def test_failed_transient_within_budget(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
            attempt_number=2,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t.next_state == DeliveryLifecycleState.RETRY_SCHEDULED
        assert t.outcome == DeliveryAttemptOutcome.FAILED_TRANSIENT
        assert t.retry_at is not None
        assert t.domain_reason == "transient_failure_retry_scheduled"

    def test_failed_transient_exhausted_budget(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
            attempt_number=5,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t.next_state == DeliveryLifecycleState.FAILED
        assert t.outcome == DeliveryAttemptOutcome.FAILED_PERMANENT
        assert t.retry_at is None
        assert "exhausted" in t.domain_reason
        assert "5" in t.domain_reason

    def test_failed_transient_custom_max_attempts(self) -> None:
        config = DeliveryRetryConfig(max_attempts=3)
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
            attempt_number=3,
            retry_config=config,
        )
        assert t.next_state == DeliveryLifecycleState.FAILED
        assert t.outcome == DeliveryAttemptOutcome.FAILED_PERMANENT

    def test_failed_permanent(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_PERMANENT,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t.next_state == DeliveryLifecycleState.FAILED
        assert t.domain_reason == "permanent_failure"
        assert t.retry_at is None

    def test_suppressed_duplicate(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_DUPLICATE,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t.next_state == DeliveryLifecycleState.SUPPRESSED
        assert t.domain_reason == "duplicate_suppressed"

    def test_suppressed_noop(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_NOOP,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t.next_state == DeliveryLifecycleState.SUPPRESSED
        assert t.domain_reason == "noop_suppressed"

    def test_abandoned_by_policy(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.ABANDONED_BY_POLICY,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t.next_state == DeliveryLifecycleState.ABANDONED
        assert t.domain_reason == "abandoned_by_policy"

    def test_terminal_state_stays_terminal(self) -> None:
        for state in DELIVERY_LIFECYCLE_TERMINAL_STATES:
            t = resolve_delivery_transition(
                current_state=state,
                outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
                attempt_number=1,
                retry_config=_DEFAULT_RETRY_CONFIG,
            )
            assert t.next_state == state
            assert "already_terminal" in t.domain_reason

    def test_domain_metadata_passed_through(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
            domain_metadata={"surface_kind": "discord", "message_id": "123"},
        )
        assert t.domain_metadata["surface_kind"] == "discord"
        assert t.domain_metadata["message_id"] == "123"

    def test_attempt_number_preserved(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=7,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t.attempt_number == 7


# ---------------------------------------------------------------------------
# Retry escalation
# ---------------------------------------------------------------------------


class TestRetryEscalation:
    def test_retry_backoff_increases(self) -> None:
        config = DeliveryRetryConfig(
            max_attempts=10,
            backoff_base=timedelta(seconds=60),
            backoff_multiplier=2.0,
            max_backoff=timedelta(minutes=30),
        )
        results = []
        for attempt in range(1, 5):
            t = resolve_delivery_transition(
                current_state=DeliveryLifecycleState.DELIVERING,
                outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
                attempt_number=attempt,
                retry_config=config,
            )
            results.append(t)

        assert all(
            r.next_state == DeliveryLifecycleState.RETRY_SCHEDULED for r in results
        )
        assert results[0].attempt_number < results[1].attempt_number

    def test_retry_does_not_exceed_max_attempts(self) -> None:
        config = DeliveryRetryConfig(max_attempts=2)
        t1 = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
            attempt_number=1,
            retry_config=config,
        )
        assert t1.next_state == DeliveryLifecycleState.RETRY_SCHEDULED

        t2 = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
            attempt_number=2,
            retry_config=config,
        )
        assert t2.next_state == DeliveryLifecycleState.FAILED

    def test_retries_cannot_mutate_routing_semantics(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
            domain_metadata={"route": "explicit", "surface_kind": "discord"},
        )
        assert t.domain_metadata["route"] == "explicit"
        assert t.domain_metadata["surface_kind"] == "discord"
        assert t.next_state == DeliveryLifecycleState.RETRY_SCHEDULED


# ---------------------------------------------------------------------------
# Advance helpers
# ---------------------------------------------------------------------------


class TestAdvanceToDispatching:
    def test_from_pending(self) -> None:
        t = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        assert t.next_state == DeliveryLifecycleState.DISPATCHED
        assert "dispatch_initiated_from_pending" in t.domain_reason

    def test_from_retry_scheduled(self) -> None:
        t = advance_to_dispatching(
            DeliveryLifecycleState.RETRY_SCHEDULED, attempt_number=3
        )
        assert t.next_state == DeliveryLifecycleState.DISPATCHED
        assert "dispatch_initiated_from_retry" in t.domain_reason

    def test_from_delivering_stays_put(self) -> None:
        t = advance_to_dispatching(DeliveryLifecycleState.DELIVERING, attempt_number=1)
        assert t.next_state == DeliveryLifecycleState.DELIVERING
        assert "dispatch_skipped" in t.domain_reason

    def test_from_succeeded_stays_put(self) -> None:
        t = advance_to_dispatching(DeliveryLifecycleState.SUCCEEDED, attempt_number=1)
        assert t.next_state == DeliveryLifecycleState.SUCCEEDED


class TestAdvanceToDelivering:
    def test_from_dispatched(self) -> None:
        t = advance_to_delivering(DeliveryLifecycleState.DISPATCHED, attempt_number=1)
        assert t.next_state == DeliveryLifecycleState.DELIVERING
        assert "adapter_delivery_started" in t.domain_reason

    def test_from_pending(self) -> None:
        t = advance_to_delivering(DeliveryLifecycleState.PENDING, attempt_number=1)
        assert t.next_state == DeliveryLifecycleState.DELIVERING

    def test_from_retry_scheduled(self) -> None:
        t = advance_to_delivering(
            DeliveryLifecycleState.RETRY_SCHEDULED, attempt_number=2
        )
        assert t.next_state == DeliveryLifecycleState.DELIVERING

    def test_from_delivering_stays_put(self) -> None:
        t = advance_to_delivering(DeliveryLifecycleState.DELIVERING, attempt_number=1)
        assert t.next_state == DeliveryLifecycleState.DELIVERING
        assert "delivery_start_skipped" in t.domain_reason


# ---------------------------------------------------------------------------
# Full lifecycle scenarios
# ---------------------------------------------------------------------------


class TestFullLifecycleScenarios:
    def test_happy_path_pending_to_succeeded(self) -> None:
        config = _DEFAULT_RETRY_CONFIG
        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        assert t1.next_state == DeliveryLifecycleState.DISPATCHED

        t2 = advance_to_delivering(t1.next_state, attempt_number=1)
        assert t2.next_state == DeliveryLifecycleState.DELIVERING

        t3 = resolve_delivery_transition(
            current_state=t2.next_state,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=1,
            retry_config=config,
        )
        assert t3.next_state == DeliveryLifecycleState.SUCCEEDED

    def test_retry_then_succeed(self) -> None:
        config = _DEFAULT_RETRY_CONFIG
        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        t2 = advance_to_delivering(t1.next_state, attempt_number=1)
        t3 = resolve_delivery_transition(
            current_state=t2.next_state,
            outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
            attempt_number=1,
            retry_config=config,
        )
        assert t3.next_state == DeliveryLifecycleState.RETRY_SCHEDULED

        t4 = advance_to_dispatching(t3.next_state, attempt_number=2)
        assert t4.next_state == DeliveryLifecycleState.DISPATCHED

        t5 = advance_to_delivering(t4.next_state, attempt_number=2)
        t6 = resolve_delivery_transition(
            current_state=t5.next_state,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=2,
            retry_config=config,
        )
        assert t6.next_state == DeliveryLifecycleState.SUCCEEDED
        assert t6.attempt_number == 2

    def test_permanent_failure_on_first_attempt(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_PERMANENT,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t.next_state == DeliveryLifecycleState.FAILED

    def test_suppressed_duplicate_lifecycle(self) -> None:
        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        t2 = advance_to_delivering(t1.next_state, attempt_number=1)
        t3 = resolve_delivery_transition(
            current_state=t2.next_state,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_DUPLICATE,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t3.next_state == DeliveryLifecycleState.SUPPRESSED
        assert "duplicate" in t3.domain_reason

    def test_abandoned_by_policy_lifecycle(self) -> None:
        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        t2 = resolve_delivery_transition(
            current_state=t1.next_state,
            outcome=DeliveryAttemptOutcome.ABANDONED_BY_POLICY,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t2.next_state == DeliveryLifecycleState.ABANDONED

    def test_exhausted_retries(self) -> None:
        config = DeliveryRetryConfig(max_attempts=3)
        state = DeliveryLifecycleState.DELIVERING
        for attempt in range(1, 4):
            t = resolve_delivery_transition(
                current_state=state,
                outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
                attempt_number=attempt,
                retry_config=config,
            )
            if t.next_state == DeliveryLifecycleState.RETRY_SCHEDULED:
                state = DeliveryLifecycleState.DELIVERING
            else:
                state = t.next_state
        assert state == DeliveryLifecycleState.FAILED


# ---------------------------------------------------------------------------
# Rebinding policy
# ---------------------------------------------------------------------------


class TestRebindingPolicy:
    def test_binding_unchanged_keeps_original(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-1",
            current_surface_kind="discord",
            current_surface_key="chan-1",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.KEEP_ORIGINAL
        assert result.domain_reason == "binding_unchanged"
        assert result.effective_surface_kind == "discord"
        assert result.effective_surface_key == "chan-1"

    def test_binding_changed_rebuilds_for_non_explicit_route(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-1",
            persisted_route="bound",
            current_surface_kind="discord",
            current_surface_key="chan-2",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.REBUILD_ROUTES
        assert result.effective_surface_kind == "discord"
        assert result.effective_surface_key == "chan-2"
        assert "binding_changed" in result.domain_reason
        assert result.metadata["rebinding_trigger"] == "binding_drift"

    def test_binding_changed_suppresses_for_explicit_route(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-1",
            persisted_route="explicit",
            current_surface_kind="discord",
            current_surface_key="chan-2",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.SUPPRESS
        assert "explicit" in result.domain_reason

    def test_terminal_state_keeps_original(self) -> None:
        for state in ("succeeded", "failed", "suppressed", "abandoned"):
            ctx = RebindingContext(
                persisted_surface_kind="discord",
                persisted_surface_key="chan-1",
                current_surface_kind="telegram",
                current_surface_key="grp-1",
                delivery_state=state,
            )
            result = evaluate_rebinding_decision(ctx)
            assert result.decision == RebindingDecision.KEEP_ORIGINAL
            assert "terminal" in result.domain_reason

    def test_binding_changed_no_current_target_falls_back(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-1",
            persisted_route="bound",
            current_surface_kind=None,
            current_surface_key=None,
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.KEEP_ORIGINAL
        assert "fallback" in result.domain_reason
        assert result.effective_surface_kind == "discord"
        assert result.effective_surface_key == "chan-1"

    def test_rebinding_preserves_original_in_metadata(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-old",
            persisted_route="bound",
            current_surface_kind="telegram",
            current_surface_key="grp-new",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.metadata["original_surface_kind"] == "discord"
        assert result.metadata["original_surface_key"] == "chan-old"

    def test_surface_kind_change_triggers_rebuild(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-1",
            persisted_route="primary_pma",
            current_surface_kind="telegram",
            current_surface_key="grp-1",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.REBUILD_ROUTES
        assert result.effective_surface_kind == "telegram"
        assert result.effective_surface_key == "grp-1"


# ---------------------------------------------------------------------------
# Domain observability
# ---------------------------------------------------------------------------


class TestDomainObservability:
    def test_transition_records_domain_reason(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
            domain_reason="adapter_http_503",
        )
        assert t.domain_reason == "adapter_http_503"
        assert t.next_state == DeliveryLifecycleState.RETRY_SCHEDULED

    def test_transition_records_outcome_classification(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_NOOP,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
        )
        assert t.outcome == DeliveryAttemptOutcome.SUPPRESSED_NOOP
        assert t.next_state == DeliveryLifecycleState.SUPPRESSED

    def test_operator_can_inspect_why_route_failed(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_PERMANENT,
            attempt_number=3,
            retry_config=_DEFAULT_RETRY_CONFIG,
            domain_reason="channel_deleted_by_admin",
            domain_metadata={
                "surface_kind": "discord",
                "surface_key": "chan-1",
                "adapter_error_code": "10003",
            },
        )
        assert t.domain_reason == "channel_deleted_by_admin"
        assert t.domain_metadata["adapter_error_code"] == "10003"
        assert t.attempt_number == 3
        assert t.next_state == DeliveryLifecycleState.FAILED

    def test_operator_can_inspect_retry_schedule(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
            attempt_number=2,
            retry_config=_DEFAULT_RETRY_CONFIG,
            domain_reason="network_timeout",
        )
        assert t.retry_at is not None
        assert t.domain_reason == "network_timeout"
        assert t.next_state == DeliveryLifecycleState.RETRY_SCHEDULED

    def test_operator_can_inspect_suppression_reason(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_DUPLICATE,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
            domain_reason="message_fingerprint_match",
            domain_metadata={"original_message_id": "msg-123"},
        )
        assert t.next_state == DeliveryLifecycleState.SUPPRESSED
        assert t.domain_reason == "message_fingerprint_match"
        assert t.domain_metadata["original_message_id"] == "msg-123"


# ---------------------------------------------------------------------------
# Cross-cutting: rebinding + lifecycle integration
# ---------------------------------------------------------------------------


class TestRebindingWithLifecycle:
    def test_rebinding_suppress_before_delivery(self) -> None:
        rebinding = evaluate_rebinding_decision(
            RebindingContext(
                persisted_surface_kind="discord",
                persisted_surface_key="chan-1",
                persisted_route="explicit",
                current_surface_kind="discord",
                current_surface_key="chan-2",
                delivery_state="dispatched",
            )
        )
        assert rebinding.decision == RebindingDecision.SUPPRESS

        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DISPATCHED,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_DUPLICATE,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
            domain_reason="rebinding_suppressed_explicit_drift",
            domain_metadata=rebinding.metadata,
        )
        assert t.next_state == DeliveryLifecycleState.SUPPRESSED
        assert "rebinding" in t.domain_reason
        assert t.domain_metadata["rebinding_trigger"] == "explicit_binding_drift"

    def test_rebinding_rebuild_then_deliver(self) -> None:
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

        t = advance_to_delivering(DeliveryLifecycleState.DISPATCHED, attempt_number=1)
        assert t.next_state == DeliveryLifecycleState.DELIVERING

        t2 = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=1,
            retry_config=_DEFAULT_RETRY_CONFIG,
            domain_reason="delivered_to_rebound_target",
            domain_metadata={
                "original_target": "chan-old",
                "rebound_target": rebinding.effective_surface_key,
            },
        )
        assert t2.next_state == DeliveryLifecycleState.SUCCEEDED
        assert t2.domain_metadata["rebound_target"] == "chan-new"


# ---------------------------------------------------------------------------
# DeliveryTransition is a frozen dataclass
# ---------------------------------------------------------------------------


class TestDeliveryTransitionImmutability:
    def test_transition_is_frozen(self) -> None:
        t = DeliveryTransition(
            next_state=DeliveryLifecycleState.SUCCEEDED,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            domain_reason="test",
            attempt_number=1,
        )
        with pytest.raises(AttributeError):
            t.domain_reason = "mutated"  # type: ignore[misc]

    def test_rebinding_result_is_frozen(self) -> None:
        r = RebindingResult(
            decision=RebindingDecision.KEEP_ORIGINAL,
            domain_reason="test",
        )
        with pytest.raises(AttributeError):
            r.domain_reason = "mutated"  # type: ignore[misc]
