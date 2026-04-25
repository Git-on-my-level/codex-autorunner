"""Replay corpus: durable test fixtures encoding known PMA routing and wakeup incidents.

Each test in this file replays a specific production incident scenario through the
pure domain functions.  The goal is to make regressions impossible by encoding the
exact inputs, decisions, and expected outcomes from real bugs.

Incidents encoded:
  1. Origin-thread Discord wakeup routing (origin thread binding determines delivery)
  2. Duplicate noop suppression to the same channel (suppressed_duplicate)
  3. Binding drift rebinding after persisted dispatch (rebinding_policy)
  4. Retry exhaustion after transient failures (delivery_lifecycle)
  5. Explicit target binding drift suppression (rebinding suppress path)
  6. Watchdog timer fired during idle managed thread
"""

from __future__ import annotations

from codex_autorunner.core.pma_domain.constants import (
    NOTICE_KIND_NOOP,
    NOTICE_KIND_TERMINAL_FOLLOWUP,
    SOURCE_KIND_MANAGED_THREAD_COMPLETED,
    SUBSCRIPTION_STATE_ACTIVE,
)
from codex_autorunner.core.pma_domain.delivery_lifecycle import (
    DeliveryAttemptOutcome,
    DeliveryLifecycleState,
    DeliveryRetryConfig,
    advance_to_delivering,
    advance_to_dispatching,
    resolve_delivery_transition,
)
from codex_autorunner.core.pma_domain.models import (
    PmaDispatchAttempt,
    PmaDispatchDecision,
    PmaSubscription,
    PublishNoticeContext,
)
from codex_autorunner.core.pma_domain.publish_policy import (
    evaluate_publish_suppression,
)
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

# ---------------------------------------------------------------------------
# Incident 1: Origin-thread Discord wakeup routing
#
# A managed thread completes and a subscription triggers a wakeup. The wakeup
# must deliver to the *origin thread's* Discord binding, not the watched thread's
# binding. This was the "origin-thread Discord wakeup bug" where delivery went
# to the wrong channel.
# ---------------------------------------------------------------------------


class TestIncidentOriginThreadDiscordWakeup:
    def test_wakeup_routes_to_origin_thread_binding(self) -> None:
        origin_sub = PmaSubscription(
            subscription_id="sub-origin",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            state=SUBSCRIPTION_STATE_ACTIVE,
            event_types=("managed_thread_completed",),
            thread_id="watched-thread-1",
            lane_id="discord",
            metadata={
                "delivery_target": {
                    "surface_kind": "discord",
                    "surface_key": "origin-discord-chan",
                },
                "pma_origin": {
                    "thread_id": "origin-thread-1",
                },
            },
        )
        event = TransitionEvent(
            event_type="managed_thread_completed",
            repo_id="repo-1",
            thread_id="watched-thread-1",
            from_state="running",
            to_state="completed",
            reason="transition",
            transition_id="watched-thread-1:completed",
        )
        result = reduce_transition(
            [origin_sub],
            frozenset(),
            event,
        )
        assert result.matched == 1
        assert result.created == 1
        wakeup_meta = result.wakeup_intents[0].metadata
        assert wakeup_meta["delivery_target"]["surface_kind"] == "discord"
        assert wakeup_meta["delivery_target"]["surface_key"] == "origin-discord-chan"
        assert wakeup_meta["pma_origin"]["thread_id"] == "origin-thread-1"

    def test_origin_thread_binding_used_for_dispatch_not_watched(self) -> None:
        decision = PmaDispatchDecision(
            requested_delivery="auto",
            suppress_publish=False,
            attempts=(
                PmaDispatchAttempt(
                    route="explicit",
                    delivery_mode="bound",
                    surface_kind="discord",
                    surface_key="origin-discord-chan",
                    repo_id="repo-1",
                ),
            ),
        )
        assert len(decision.attempts) == 1
        assert decision.attempts[0].surface_key == "origin-discord-chan"
        assert decision.attempts[0].route == "explicit"

    def test_wakeup_idempotency_prevents_double_dispatch(self) -> None:
        sub = PmaSubscription(
            subscription_id="sub-origin",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            event_types=("managed_thread_completed",),
            thread_id="watched-thread-1",
        )
        event = TransitionEvent(
            event_type="managed_thread_completed",
            thread_id="watched-thread-1",
            from_state="running",
            to_state="completed",
            transition_id="watched-thread-1:completed",
        )
        r1 = reduce_transition([sub], frozenset(), event)
        existing_keys = frozenset(wi.idempotency_key for wi in r1.wakeup_intents)
        r2 = reduce_transition(list(r1.subscriptions), existing_keys, event)
        assert r2.matched == 1
        assert r2.created == 0


# ---------------------------------------------------------------------------
# Incident 2: Duplicate noop suppression
#
# A managed thread completes with "Already handled. No action needed." and the
# delivery target matches the thread's own binding. This should be suppressed
# to avoid sending a redundant noop back to the originating channel.
# ---------------------------------------------------------------------------


class TestIncidentDuplicateNoopSuppression:
    def test_noop_to_same_binding_is_suppressed(self) -> None:
        decision = evaluate_publish_suppression(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            message_text="Already handled. No action needed.",
            managed_thread_id="thread-1",
            target_matches_thread_binding=True,
        )
        assert decision.suppressed is True
        assert decision.notice_kind == NOTICE_KIND_NOOP

    def test_noop_to_different_binding_is_not_suppressed(self) -> None:
        decision = evaluate_publish_suppression(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            message_text="Already handled. No action needed.",
            managed_thread_id="thread-1",
            target_matches_thread_binding=False,
        )
        assert decision.suppressed is False

    def test_normal_message_to_same_binding_is_not_suppressed(self) -> None:
        decision = evaluate_publish_suppression(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            message_text="Changes pushed to main.",
            managed_thread_id="thread-1",
            target_matches_thread_binding=True,
        )
        assert decision.suppressed is False
        assert decision.notice_kind == NOTICE_KIND_TERMINAL_FOLLOWUP

    def test_noop_without_managed_thread_not_suppressed(self) -> None:
        decision = evaluate_publish_suppression(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            message_text="Already handled. No action needed.",
            managed_thread_id=None,
            target_matches_thread_binding=True,
        )
        assert decision.suppressed is False

    def test_dispatch_decision_reflects_suppression(self) -> None:
        decision = PmaDispatchDecision(
            requested_delivery="suppressed_duplicate",
            suppress_publish=True,
            attempts=(),
        )
        assert decision.suppress_publish is True
        assert decision.attempts == ()

    def test_delivery_lifecycle_suppressed_state_is_terminal(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_NOOP,
            attempt_number=1,
            retry_config=DeliveryRetryConfig(),
        )
        assert t.next_state == DeliveryLifecycleState.SUPPRESSED
        t2 = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.SUPPRESSED,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=1,
            retry_config=DeliveryRetryConfig(),
        )
        assert t2.next_state == DeliveryLifecycleState.SUPPRESSED


# ---------------------------------------------------------------------------
# Incident 3: Binding drift rebinding after persisted dispatch
#
# A dispatch decision is persisted targeting Discord channel A, but by the time
# delivery executes, the binding has moved to Discord channel B. The rebinding
# policy must rebuild routes for non-explicit routes.
# ---------------------------------------------------------------------------


class TestIncidentBindingDriftRebinding:
    def test_bound_route_rebuilds_when_binding_drifts(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-old",
            persisted_route="bound",
            current_surface_kind="discord",
            current_surface_key="chan-new",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.REBUILD_ROUTES
        assert result.effective_surface_key == "chan-new"
        assert result.metadata["original_surface_key"] == "chan-old"

    def test_cross_surface_rebuild(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-old",
            persisted_route="bound",
            current_surface_kind="telegram",
            current_surface_key="grp-new",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.REBUILD_ROUTES
        assert result.effective_surface_kind == "telegram"

    def test_binding_unchanged_does_not_rebuild(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="chan-same",
            persisted_route="bound",
            current_surface_kind="discord",
            current_surface_key="chan-same",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.KEEP_ORIGINAL


# ---------------------------------------------------------------------------
# Incident 4: Retry exhaustion after transient failures
#
# A delivery repeatedly fails with transient errors (e.g., network timeouts).
# After max_attempts, it must transition to FAILED, not loop forever.
# ---------------------------------------------------------------------------


class TestIncidentRetryExhaustion:
    def test_exhaustion_at_default_max_attempts(self) -> None:
        config = DeliveryRetryConfig(max_attempts=5)
        state = DeliveryLifecycleState.DELIVERING
        for attempt in range(1, 6):
            t = resolve_delivery_transition(
                current_state=state,
                outcome=DeliveryAttemptOutcome.FAILED_TRANSIENT,
                attempt_number=attempt,
                retry_config=config,
            )
            state = t.next_state
            if attempt < 5:
                assert state == DeliveryLifecycleState.RETRY_SCHEDULED
                state = DeliveryLifecycleState.DELIVERING
        assert state == DeliveryLifecycleState.FAILED

    def test_exhaustion_at_custom_max_attempts(self) -> None:
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
        assert t2.outcome == DeliveryAttemptOutcome.FAILED_PERMANENT

    def test_permanent_failure_on_first_attempt_does_not_retry(self) -> None:
        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.FAILED_PERMANENT,
            attempt_number=1,
            retry_config=DeliveryRetryConfig(max_attempts=5),
        )
        assert t.next_state == DeliveryLifecycleState.FAILED
        assert t.retry_at is None


# ---------------------------------------------------------------------------
# Incident 5: Explicit target binding drift suppression
#
# An explicit delivery target (user-specified) is persisted, but the binding
# drifts. The policy must suppress rather than rebuild because the user chose
# a specific target that is no longer valid.
# ---------------------------------------------------------------------------


class TestIncidentExplicitTargetDriftSuppression:
    def test_explicit_route_drift_suppresses(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="user-chosen-chan",
            persisted_route="explicit",
            current_surface_kind="discord",
            current_surface_key="different-chan",
            delivery_state="dispatched",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.SUPPRESS
        assert "explicit" in result.domain_reason

    def test_explicit_route_no_drift_keeps_original(self) -> None:
        ctx = RebindingContext(
            persisted_surface_kind="discord",
            persisted_surface_key="user-chosen-chan",
            persisted_route="explicit",
            current_surface_kind="discord",
            current_surface_key="user-chosen-chan",
        )
        result = evaluate_rebinding_decision(ctx)
        assert result.decision == RebindingDecision.KEEP_ORIGINAL

    def test_explicit_suppress_integrates_with_lifecycle(self) -> None:
        rebinding = evaluate_rebinding_decision(
            RebindingContext(
                persisted_surface_kind="discord",
                persisted_surface_key="explicit-chan",
                persisted_route="explicit",
                current_surface_kind="discord",
                current_surface_key="new-chan",
            )
        )
        assert rebinding.decision == RebindingDecision.SUPPRESS

        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DISPATCHED,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_DUPLICATE,
            attempt_number=1,
            retry_config=DeliveryRetryConfig(),
            domain_reason="rebinding_suppressed_explicit_drift",
            domain_metadata=rebinding.metadata,
        )
        assert t.next_state == DeliveryLifecycleState.SUPPRESSED
        assert "rebinding" in t.domain_reason


# ---------------------------------------------------------------------------
# Incident 6: Watchdog timer fired during idle managed thread
#
# A watchdog timer fires after idle_seconds, producing a wakeup that should
# carry the subscription metadata and idle context.
# ---------------------------------------------------------------------------


class TestIncidentWatchdogTimerFired:
    def test_watchdog_timer_produces_wakeup(self) -> None:
        event = TimerFiredEvent(
            timer_id="watchdog-1",
            timer_type="watchdog",
            fired_at="2026-01-15T12:05:00Z",
            repo_id="repo-1",
            thread_id="thread-1",
            lane_id="discord",
            subscription_id="sub-idle",
            metadata={"idle_seconds": 300},
        )
        result = reduce_timer_fired(event)
        intent = result.wakeup_intent
        assert intent.source == "timer"
        assert intent.repo_id == "repo-1"
        assert intent.thread_id == "thread-1"
        assert intent.subscription_id == "sub-idle"
        assert intent.metadata["timer_type"] == "watchdog"
        assert intent.metadata["idle_seconds"] == 300

    def test_watchdog_idempotency_key_deterministic(self) -> None:
        event = TimerFiredEvent(
            timer_id="wd-1",
            timer_type="watchdog",
            fired_at="2026-01-15T12:00:00Z",
        )
        r1 = reduce_timer_fired(event)
        r2 = reduce_timer_fired(event)
        assert r1.wakeup_intent.idempotency_key == r2.wakeup_intent.idempotency_key

    def test_one_shot_timer_fired(self) -> None:
        event = TimerFiredEvent(
            timer_id="oneshot-1",
            timer_type="one_shot",
            fired_at="2026-02-01T00:00:00Z",
            repo_id="repo-2",
            reason="scheduled_reminder",
        )
        result = reduce_timer_fired(event)
        assert result.wakeup_intent.reason == "scheduled_reminder"
        assert result.wakeup_intent.metadata["timer_type"] == "one_shot"


# ---------------------------------------------------------------------------
# Full incident replay: end-to-end lifecycle through domain functions
# ---------------------------------------------------------------------------


class TestFullIncidentReplay:
    def test_replay_happy_path_terminal_followup(self) -> None:
        t1 = advance_to_dispatching(DeliveryLifecycleState.PENDING, attempt_number=1)
        t2 = advance_to_delivering(t1.next_state, attempt_number=1)
        t3 = resolve_delivery_transition(
            current_state=t2.next_state,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=1,
            retry_config=DeliveryRetryConfig(),
        )
        assert t3.next_state == DeliveryLifecycleState.SUCCEEDED

    def test_replay_transient_retry_then_succeed(self) -> None:
        config = DeliveryRetryConfig(max_attempts=3)
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
        t5 = advance_to_delivering(t4.next_state, attempt_number=2)
        t6 = resolve_delivery_transition(
            current_state=t5.next_state,
            outcome=DeliveryAttemptOutcome.SUCCEEDED,
            attempt_number=2,
            retry_config=config,
        )
        assert t6.next_state == DeliveryLifecycleState.SUCCEEDED
        assert t6.attempt_number == 2

    def test_replay_noop_suppressed_lifecycle(self) -> None:
        notice_ctx = PublishNoticeContext(
            trigger="managed_thread_completed",
            status="ok",
            correlation_id="corr-1",
            output="Already handled. No action needed.",
        )
        assert notice_ctx.notice_kind() == NOTICE_KIND_NOOP

        suppression = evaluate_publish_suppression(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            message_text="Already handled. No action needed.",
            managed_thread_id="thread-1",
            target_matches_thread_binding=True,
        )
        assert suppression.suppressed is True

        t = resolve_delivery_transition(
            current_state=DeliveryLifecycleState.DELIVERING,
            outcome=DeliveryAttemptOutcome.SUPPRESSED_NOOP,
            attempt_number=1,
            retry_config=DeliveryRetryConfig(),
        )
        assert t.next_state == DeliveryLifecycleState.SUPPRESSED
