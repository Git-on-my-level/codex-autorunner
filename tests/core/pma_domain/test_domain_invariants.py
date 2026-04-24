"""Domain invariants: property-style checks that must hold for all PMA domain inputs.

These tests encode structural invariants about the PMA domain that are not
tied to specific scenarios.  If any of these fail, a domain abstraction has
been broken.  They serve as the "hardening layer" between the pure domain
and the adapters/runtime.
"""

from __future__ import annotations

import itertools
from dataclasses import fields
from datetime import timedelta
from typing import Any

import pytest

from codex_autorunner.core.pma_domain.constants import (
    DELIVERY_MODE_AUTO,
    DELIVERY_MODE_BOUND,
    DELIVERY_MODE_NONE,
    DELIVERY_MODE_PRIMARY_PMA,
    DELIVERY_MODE_SUPPRESSED,
    NOTICE_KIND_ESCALATION,
    NOTICE_KIND_NOOP,
    NOTICE_KIND_PROGRESS,
    NOTICE_KIND_TERMINAL_FOLLOWUP,
    ROUTE_BOUND,
    ROUTE_EXPLICIT,
    ROUTE_PRIMARY_PMA,
    SUBSCRIPTION_STATE_ACTIVE,
    SUBSCRIPTION_STATE_CANCELLED,
    SURFACE_KIND_DISCORD,
    SURFACE_KIND_TELEGRAM,
    TIMER_STATE_CANCELLED,
    TIMER_STATE_FIRED,
    TIMER_STATE_PENDING,
    TIMER_TYPE_ONE_SHOT,
    TIMER_TYPE_WATCHDOG,
    WAKEUP_STATE_DISPATCHED,
    WAKEUP_STATE_PENDING,
)
from codex_autorunner.core.pma_domain.delivery_lifecycle import (
    DELIVERY_LIFECYCLE_TERMINAL_STATES,
    DELIVERY_LIFECYCLE_TRANSITIONS,
    DeliveryAttemptOutcome,
    DeliveryLifecycleState,
    DeliveryRetryConfig,
    DeliveryTransition,
    is_terminal_delivery_state,
    is_valid_delivery_transition,
    resolve_delivery_transition,
)
from codex_autorunner.core.pma_domain.events import PmaDomainEventType
from codex_autorunner.core.pma_domain.models import (
    PmaDeliveryAttempt,
    PmaDeliveryIntent,
    PmaDeliveryState,
    PmaDeliveryTarget,
    PmaDispatchAttempt,
    PmaDispatchDecision,
    PmaOriginContext,
    PmaSubscription,
    PmaTimer,
    PmaWakeup,
    PublishNoticeContext,
    PublishSuppressionDecision,
)
from codex_autorunner.core.pma_domain.publish_policy import (
    is_noop_duplicate_message,
)
from codex_autorunner.core.pma_domain.rebinding_policy import (
    RebindingContext,
    RebindingDecision,
    RebindingResult,
    evaluate_rebinding_decision,
)
from codex_autorunner.core.pma_domain.serialization import (
    normalize_pma_dispatch_decision,
    normalize_pma_subscription,
    normalize_pma_timer,
    normalize_pma_wakeup,
    pma_dispatch_decision_to_dict,
    pma_subscription_to_dict,
    pma_timer_to_dict,
    pma_wakeup_to_dict,
)
from codex_autorunner.core.pma_domain.subscription_reducer import (
    TransitionEvent,
    reduce_transition,
)

# ---------------------------------------------------------------------------
# Invariant: All domain models are frozen dataclasses
# ---------------------------------------------------------------------------


_FROZEN_MODELS = [
    PmaOriginContext(),
    PmaSubscription(subscription_id="t", created_at="", updated_at=""),
    PmaTimer(timer_id="t", due_at="", created_at="", updated_at=""),
    PmaWakeup(wakeup_id="t", created_at="", updated_at=""),
    PmaDispatchAttempt(route="r", delivery_mode="m", surface_kind="s"),
    PmaDispatchDecision(requested_delivery="auto"),
    PmaDeliveryTarget(surface_kind="discord"),
    PmaDeliveryAttempt(
        route="r",
        delivery_mode="m",
        target=PmaDeliveryTarget(surface_kind="discord"),
    ),
    PmaDeliveryIntent(
        message="m",
        correlation_id="c",
        source_kind="s",
        requested_delivery="auto",
    ),
    PmaDeliveryState(delivery_id="d"),
    PublishNoticeContext(trigger="t", status="ok", correlation_id="c"),
    PublishSuppressionDecision(suppressed=False),
    DeliveryTransition(
        next_state=DeliveryLifecycleState.PENDING,
        outcome=DeliveryAttemptOutcome.SUCCEEDED,
        domain_reason="test",
        attempt_number=1,
    ),
    RebindingResult(decision=RebindingDecision.KEEP_ORIGINAL, domain_reason="test"),
]


class TestFrozenDataclasses:
    @pytest.mark.parametrize("model", _FROZEN_MODELS, ids=lambda m: type(m).__name__)
    def test_model_is_frozen(self, model: Any) -> None:
        dc_fields = list(fields(model))
        if dc_fields:
            with pytest.raises(AttributeError):
                setattr(model, dc_fields[0].name, "mutated")

    @pytest.mark.parametrize("model", _FROZEN_MODELS, ids=lambda m: type(m).__name__)
    def test_model_is_dataclass(self, model: Any) -> None:
        assert hasattr(model, "__dataclass_fields__")


# ---------------------------------------------------------------------------
# Invariant: Delivery lifecycle transition graph properties
# ---------------------------------------------------------------------------


class TestTransitionGraphInvariants:
    def test_terminal_states_have_empty_allowed_transitions(self) -> None:
        for state in DELIVERY_LIFECYCLE_TERMINAL_STATES:
            assert DELIVERY_LIFECYCLE_TRANSITIONS.get(state, frozenset()) == frozenset()

    def test_non_terminal_states_have_at_least_one_transition(self) -> None:
        non_terminal = set(DeliveryLifecycleState) - DELIVERY_LIFECYCLE_TERMINAL_STATES
        for state in non_terminal:
            allowed = DELIVERY_LIFECYCLE_TRANSITIONS.get(state, frozenset())
            assert len(allowed) > 0, f"{state} has no allowed transitions"

    def test_no_self_transition_in_allowed_set(self) -> None:
        for state, allowed in DELIVERY_LIFECYCLE_TRANSITIONS.items():
            assert state not in allowed, f"{state} can transition to itself"

    def test_terminal_states_are_detected_correctly(self) -> None:
        for state in DeliveryLifecycleState:
            expected = state in DELIVERY_LIFECYCLE_TERMINAL_STATES
            assert is_terminal_delivery_state(state) == expected

    def test_all_states_covered(self) -> None:
        assert set(DELIVERY_LIFECYCLE_TRANSITIONS.keys()) == set(DeliveryLifecycleState)

    def test_is_valid_transition_consistent_with_transition_table(self) -> None:
        for current in DeliveryLifecycleState:
            for target in DeliveryLifecycleState:
                if current == target:
                    assert is_valid_delivery_transition(current, target)
                elif target in DELIVERY_LIFECYCLE_TRANSITIONS.get(current, frozenset()):
                    assert is_valid_delivery_transition(current, target)
                else:
                    assert not is_valid_delivery_transition(current, target)


# ---------------------------------------------------------------------------
# Invariant: resolve_delivery_transition always returns a valid next state
# ---------------------------------------------------------------------------


class TestResolveTransitionInvariants:
    @pytest.mark.parametrize(
        "state,outcome",
        list(
            itertools.product(
                [DeliveryLifecycleState.DELIVERING],
                [
                    DeliveryAttemptOutcome.SUCCEEDED,
                    DeliveryAttemptOutcome.FAILED_TRANSIENT,
                    DeliveryAttemptOutcome.FAILED_PERMANENT,
                    DeliveryAttemptOutcome.ABANDONED_BY_POLICY,
                ],
            )
        ),
    )
    def test_resolve_from_delivering_produces_valid_transition(
        self,
        state: DeliveryLifecycleState,
        outcome: DeliveryAttemptOutcome,
    ) -> None:
        config = DeliveryRetryConfig()
        t = resolve_delivery_transition(
            current_state=state,
            outcome=outcome,
            attempt_number=1,
            retry_config=config,
        )
        assert is_valid_delivery_transition(
            state, t.next_state
        ), f"Invalid transition: {state} -> {t.next_state} for outcome {outcome}"

    @pytest.mark.parametrize(
        "state", sorted(DELIVERY_LIFECYCLE_TERMINAL_STATES, key=lambda s: s.value)
    )
    def test_terminal_state_never_moves(self, state: DeliveryLifecycleState) -> None:
        for outcome in sorted(DeliveryAttemptOutcome, key=lambda o: o.value):
            t = resolve_delivery_transition(
                current_state=state,
                outcome=outcome,
                attempt_number=1,
                retry_config=DeliveryRetryConfig(),
            )
            assert t.next_state == state


# ---------------------------------------------------------------------------
# Invariant: Backoff is monotonically increasing (for transient retries)
# ---------------------------------------------------------------------------


class TestBackoffMonotonicity:
    def test_backoff_increases_with_attempts(self) -> None:
        config = DeliveryRetryConfig(
            max_attempts=10,
            backoff_base=timedelta(seconds=60),
            backoff_multiplier=2.0,
            max_backoff=timedelta(minutes=30),
        )
        retry_times: list[str] = []
        for attempt in range(1, 5):
            t = resolve_delivery_transition(
                current_state=DeliveryLifecycleState.DELIVERING,
                outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
                attempt_number=attempt,
                retry_config=config,
            )
            assert t.retry_at is not None
            retry_times.append(t.retry_at)

    def test_backoff_respects_max(self) -> None:
        config = DeliveryRetryConfig(
            max_attempts=10,
            backoff_base=timedelta(minutes=1),
            backoff_multiplier=10.0,
            max_backoff=timedelta(minutes=30),
        )
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
            attempt_number=5,
            retry_config=config,
        )
        assert t.retry_at is not None


# ---------------------------------------------------------------------------
# Invariant: Reducer purity (no input mutation)
# ---------------------------------------------------------------------------


class TestReducerPurity:
    def test_reduce_transition_does_not_mutate_inputs(self) -> None:
        sub = PmaSubscription(
            subscription_id="s1",
            created_at="",
            updated_at="",
            match_count=5,
        )
        event = TransitionEvent(
            event_type="flow_completed",
            transition_id="t1",
        )
        keys = frozenset()
        reduce_transition([sub], keys, event)
        assert sub.match_count == 5
        assert sub.state == SUBSCRIPTION_STATE_ACTIVE
        assert len(keys) == 0

    def test_reduce_transition_returns_new_subscription_objects(self) -> None:
        sub = PmaSubscription(
            subscription_id="s1",
            created_at="",
            updated_at="",
        )
        event = TransitionEvent(event_type="flow_completed", transition_id="t1")
        result = reduce_transition([sub], frozenset(), event)
        assert result.subscriptions[0] is not sub


# ---------------------------------------------------------------------------
# Invariant: Serialization round-trip
# ---------------------------------------------------------------------------


class TestSerializationRoundTrip:
    def test_subscription_round_trip_preserves_fields(self) -> None:
        data = {
            "subscription_id": "sub-rt",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "state": "active",
            "event_types": ["flow_completed"],
            "repo_id": "repo-1",
            "lane_id": "discord",
            "max_matches": 3,
            "match_count": 1,
            "metadata": {"key": "val"},
        }
        model = normalize_pma_subscription(data)
        assert model is not None
        serialized = pma_subscription_to_dict(model)
        re_parsed = normalize_pma_subscription(serialized)
        assert re_parsed is not None
        assert re_parsed.subscription_id == model.subscription_id
        assert re_parsed.max_matches == model.max_matches
        assert re_parsed.match_count == model.match_count

    def test_timer_round_trip_preserves_fields(self) -> None:
        data = {
            "timer_id": "timer-rt",
            "due_at": "2026-06-01T00:00:00Z",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "timer_type": "watchdog",
            "idle_seconds": 300,
        }
        model = normalize_pma_timer(data)
        assert model is not None
        serialized = pma_timer_to_dict(model)
        re_parsed = normalize_pma_timer(serialized)
        assert re_parsed is not None
        assert re_parsed.timer_id == model.timer_id
        assert re_parsed.timer_type == model.timer_type

    def test_wakeup_round_trip_preserves_fields(self) -> None:
        data = {
            "wakeup_id": "wakeup-rt",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "source": "transition",
            "event_data": {"detail": "test"},
        }
        model = normalize_pma_wakeup(data)
        assert model is not None
        serialized = pma_wakeup_to_dict(model)
        re_parsed = normalize_pma_wakeup(serialized)
        assert re_parsed is not None
        assert re_parsed.wakeup_id == model.wakeup_id
        assert re_parsed.event_data == model.event_data

    def test_dispatch_decision_round_trip(self) -> None:
        data = {
            "requested_delivery": "auto",
            "suppress_publish": False,
            "attempts": [
                {
                    "route": "explicit",
                    "delivery_mode": "bound",
                    "surface_kind": "discord",
                    "surface_key": "c1",
                },
            ],
        }
        model = normalize_pma_dispatch_decision(data)
        assert model is not None
        serialized = pma_dispatch_decision_to_dict(model)
        re_parsed = normalize_pma_dispatch_decision(serialized)
        assert re_parsed is not None
        assert re_parsed.requested_delivery == model.requested_delivery
        assert len(re_parsed.attempts) == len(model.attempts)

    def test_normalization_idempotent(self) -> None:
        data = {
            "subscription_id": "sub-idem",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        m1 = normalize_pma_subscription(data)
        assert m1 is not None
        s1 = pma_subscription_to_dict(m1)
        m2 = normalize_pma_subscription(s1)
        assert m2 is not None
        s2 = pma_subscription_to_dict(m2)
        assert s1 == s2


# ---------------------------------------------------------------------------
# Invariant: Constants are internally consistent
# ---------------------------------------------------------------------------


class TestConstantsConsistency:
    def test_surface_kinds_are_discoverable(self) -> None:
        assert SURFACE_KIND_DISCORD in {"discord", "telegram"}
        assert SURFACE_KIND_TELEGRAM in {"discord", "telegram"}

    def test_notice_kinds_are_distinct(self) -> None:
        kinds = {
            NOTICE_KIND_TERMINAL_FOLLOWUP,
            NOTICE_KIND_NOOP,
            NOTICE_KIND_ESCALATION,
            NOTICE_KIND_PROGRESS,
        }
        assert len(kinds) == 4

    def test_delivery_modes_are_distinct(self) -> None:
        modes = {
            DELIVERY_MODE_AUTO,
            DELIVERY_MODE_NONE,
            DELIVERY_MODE_BOUND,
            DELIVERY_MODE_PRIMARY_PMA,
            DELIVERY_MODE_SUPPRESSED,
        }
        assert len(modes) == 5

    def test_routes_are_distinct(self) -> None:
        routes = {ROUTE_EXPLICIT, ROUTE_PRIMARY_PMA, ROUTE_BOUND}
        assert len(routes) == 3

    def test_subscription_states_are_complementary(self) -> None:
        states = {SUBSCRIPTION_STATE_ACTIVE, SUBSCRIPTION_STATE_CANCELLED}
        assert len(states) == 2

    def test_timer_states_cover_lifecycle(self) -> None:
        states = {TIMER_STATE_PENDING, TIMER_STATE_FIRED, TIMER_STATE_CANCELLED}
        assert len(states) == 3

    def test_timer_types_are_distinct(self) -> None:
        types = {TIMER_TYPE_ONE_SHOT, TIMER_TYPE_WATCHDOG}
        assert len(types) == 2

    def test_wakeup_states_are_distinct(self) -> None:
        states = {WAKEUP_STATE_PENDING, WAKEUP_STATE_DISPATCHED}
        assert len(states) == 2


# ---------------------------------------------------------------------------
# Invariant: Event type enum completeness
# ---------------------------------------------------------------------------


class TestEventTypeCompleteness:
    def test_all_event_types_have_string_value(self) -> None:
        for et in PmaDomainEventType:
            assert isinstance(et.value, str)
            assert len(et.value) > 0

    def test_event_type_values_are_unique(self) -> None:
        values = [et.value for et in PmaDomainEventType]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# Invariant: Rebinding policy edge cases
# ---------------------------------------------------------------------------


class TestRebindingEdgeCaseInvariants:
    def test_all_terminal_delivery_states_keep_original(self) -> None:
        for state in ("succeeded", "failed", "suppressed", "abandoned"):
            ctx = RebindingContext(
                persisted_surface_kind="discord",
                persisted_surface_key="chan-a",
                current_surface_kind="telegram",
                current_surface_key="grp-b",
                delivery_state=state,
            )
            result = evaluate_rebinding_decision(ctx)
            assert result.decision == RebindingDecision.KEEP_ORIGINAL

    def test_no_current_target_falls_back_to_original(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-a",
            persisted_route="bound",
            current_surface_kind=None,
            current_surface_key=None,
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.KEEP_ORIGINAL
        assert result.effective_surface_kind == "discord"
        assert result.effective_surface_key == "chan-a"

    def test_explicit_route_with_drift_always_suppresses(self) -> None:
        for current_kind in ("discord", "telegram"):
            for current_key in ("chan-new", "grp-new"):
                ctx = RebindingContext(
                    persisted_surface_kind="discord",
                    persisted_surface_key="explicit-chan",
                    persisted_route="explicit",
                    current_surface_kind=current_kind,
                    current_surface_key=current_key,
                )
                result = evaluate_rebinding_decision(ctx)
                assert result.decision == RebindingDecision.SUPPRESS


# ---------------------------------------------------------------------------
# Invariant: Noop detection is robust
# ---------------------------------------------------------------------------


class TestNoopDetectionRobustness:
    @pytest.mark.parametrize(
        "message",
        [
            "Already handled. No action needed.",
            "already handled, no action.",
            "  Already   handled  ,  no  action  ",
            "Duplicate — already handled, no action.",
            "Thread already handled. No action required.",
            "Already handled - no action to take.",
        ],
    )
    def test_known_noop_variants(self, message: str) -> None:
        assert is_noop_duplicate_message(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "",
            "Changes pushed to main.",
            "Already handled",
            "No action",
            "Task completed successfully.",
            "Fixed the bug and pushed.",
        ],
    )
    def test_non_noop_variants(self, message: str) -> None:
        assert is_noop_duplicate_message(message) is False

    def test_noop_detection_case_insensitive(self) -> None:
        assert is_noop_duplicate_message("ALREADY HANDLED, NO ACTION")
        assert is_noop_duplicate_message("already handled, no action")
