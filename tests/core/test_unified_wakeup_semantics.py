from __future__ import annotations

from codex_autorunner.core.pma_domain.constants import (
    DEFAULT_PMA_LANE_ID,
    SUBSCRIPTION_STATE_ACTIVE,
    TIMER_TYPE_ONE_SHOT,
    TIMER_TYPE_WATCHDOG,
)
from codex_autorunner.core.pma_domain.models import PmaSubscription
from codex_autorunner.core.pma_domain.subscription_reducer import (
    TimerFiredEvent,
    TransitionEvent,
    reduce_timer_fired,
    reduce_transition,
)


def _sub(
    *,
    subscription_id: str = "sub-1",
    state: str = SUBSCRIPTION_STATE_ACTIVE,
    event_types: tuple[str, ...] = ("flow_completed",),
    repo_id: str | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    lane_id: str = DEFAULT_PMA_LANE_ID,
    from_state: str | None = None,
    to_state: str | None = None,
    max_matches: int | None = None,
    match_count: int = 0,
    metadata: dict | None = None,
) -> PmaSubscription:
    return PmaSubscription(
        subscription_id=subscription_id,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        state=state,
        event_types=event_types,
        repo_id=repo_id,
        run_id=run_id,
        thread_id=thread_id,
        lane_id=lane_id,
        from_state=from_state,
        to_state=to_state,
        max_matches=max_matches,
        match_count=match_count,
        metadata=metadata or {},
    )


class TestTimerReducerPure:
    def test_one_shot_timer_produces_wakeup(self):
        event = TimerFiredEvent(
            timer_id="t-1",
            timer_type=TIMER_TYPE_ONE_SHOT,
            fired_at="2026-01-01T00:05:00Z",
            repo_id="repo-1",
            run_id="run-1",
            reason="idle_timeout",
        )
        result = reduce_timer_fired(event)
        assert result.timer_id == "t-1"
        assert result.source == "timer"
        intent = result.wakeup_intent
        assert intent.source == "timer"
        assert intent.repo_id == "repo-1"
        assert intent.run_id == "run-1"
        assert intent.reason == "idle_timeout"
        assert intent.event_type == "timer_fired"
        assert intent.idempotency_key == "timer:t-1:2026-01-01T00:05:00Z"
        assert intent.metadata["timer_type"] == TIMER_TYPE_ONE_SHOT
        assert intent.metadata["domain_source"] == "timer_reducer"

    def test_watchdog_timer_produces_wakeup(self):
        event = TimerFiredEvent(
            timer_id="t-2",
            timer_type=TIMER_TYPE_WATCHDOG,
            fired_at="2026-01-01T00:10:00Z",
            repo_id="repo-1",
            metadata={"watchdog_key": "value"},
        )
        result = reduce_timer_fired(event)
        intent = result.wakeup_intent
        assert intent.source == "timer"
        assert intent.metadata["timer_type"] == TIMER_TYPE_WATCHDOG
        assert intent.metadata["watchdog_key"] == "value"
        assert intent.reason == "timer_due"

    def test_timer_with_subscription_id(self):
        event = TimerFiredEvent(
            timer_id="t-3",
            subscription_id="sub-42",
        )
        result = reduce_timer_fired(event)
        assert result.wakeup_intent.subscription_id == "sub-42"

    def test_timer_with_scope_fields(self):
        event = TimerFiredEvent(
            timer_id="t-4",
            repo_id="repo-1",
            run_id="run-1",
            thread_id="thread-1",
            lane_id="discord",
            from_state="running",
            to_state="idle",
        )
        result = reduce_timer_fired(event)
        intent = result.wakeup_intent
        assert intent.repo_id == "repo-1"
        assert intent.run_id == "run-1"
        assert intent.thread_id == "thread-1"
        assert intent.lane_id == "discord"
        assert intent.from_state == "running"
        assert intent.to_state == "idle"

    def test_timer_fired_at_used_as_timestamp(self):
        event = TimerFiredEvent(
            timer_id="t-5",
            fired_at="2026-01-01T01:00:00Z",
        )
        result = reduce_timer_fired(event)
        assert result.wakeup_intent.timestamp == "2026-01-01T01:00:00Z"

    def test_timer_without_fired_at_uses_empty_string(self):
        event = TimerFiredEvent(timer_id="t-6")
        result = reduce_timer_fired(event)
        assert result.wakeup_intent.timestamp == ""
        assert "timer:t-6:" in result.wakeup_intent.idempotency_key


class TestTransitionReducerEventIdFlow:
    def test_event_id_flows_from_extra_metadata_to_intent(self):
        result = reduce_transition(
            [_sub()],
            frozenset(),
            TransitionEvent(
                event_type="flow_completed",
                extra_metadata={"event_id": "evt-123"},
            ),
        )
        assert result.wakeup_intents[0].event_id == "evt-123"

    def test_event_data_flows_from_extra_metadata_to_intent(self):
        result = reduce_transition(
            [_sub()],
            frozenset(),
            TransitionEvent(
                event_type="flow_completed",
                extra_metadata={
                    "event_data": {"key": "value"},
                },
            ),
        )
        assert result.wakeup_intents[0].event_data == {"key": "value"}

    def test_non_dict_event_data_treated_as_empty(self):
        result = reduce_transition(
            [_sub()],
            frozenset(),
            TransitionEvent(
                event_type="flow_completed",
                extra_metadata={"event_data": "not a dict"},
            ),
        )
        assert result.wakeup_intents[0].event_data == {}

    def test_no_event_id_yields_none(self):
        result = reduce_transition(
            [_sub()],
            frozenset(),
            TransitionEvent(event_type="flow_completed"),
        )
        assert result.wakeup_intents[0].event_id is None


class TestEquivalentWakeupFields:
    def test_both_sources_produce_consistent_field_set(self):
        transition_result = reduce_transition(
            [_sub(repo_id="repo-1", run_id="run-1", thread_id="thread-1")],
            frozenset(),
            TransitionEvent(
                event_type="flow_completed",
                repo_id="repo-1",
                run_id="run-1",
                thread_id="thread-1",
                from_state="running",
                to_state="completed",
                reason="done",
                extra_metadata={"event_id": "evt-1"},
            ),
        )
        t_intent = transition_result.wakeup_intents[0]

        timer_result = reduce_timer_fired(
            TimerFiredEvent(
                timer_id="t-1",
                repo_id="repo-1",
                run_id="run-1",
                thread_id="thread-1",
                from_state="running",
                to_state="completed",
                reason="timer_due",
            )
        )
        timer_intent = timer_result.wakeup_intent

        shared_fields = [
            "source",
            "repo_id",
            "run_id",
            "thread_id",
            "lane_id",
            "from_state",
            "to_state",
            "reason",
            "timestamp",
            "idempotency_key",
            "event_type",
            "metadata",
        ]
        for field_name in shared_fields:
            assert hasattr(
                t_intent, field_name
            ), f"Transition intent missing {field_name}"
            assert hasattr(
                timer_intent, field_name
            ), f"Timer intent missing {field_name}"

    def test_both_sources_use_domain_owned_idempotency_key(self):
        transition_result = reduce_transition(
            [_sub(subscription_id="sub-1")],
            frozenset(),
            TransitionEvent(
                event_type="flow_completed",
                transition_id="trans-1",
            ),
        )
        assert transition_result.wakeup_intents[0].idempotency_key.startswith(
            "transition:"
        )

        timer_result = reduce_timer_fired(
            TimerFiredEvent(timer_id="t-1", fired_at="2026-01-01T00:00:00Z")
        )
        assert timer_result.wakeup_intent.idempotency_key.startswith("timer:")

    def test_both_sources_have_event_type(self):
        transition_result = reduce_transition(
            [_sub()],
            frozenset(),
            TransitionEvent(event_type="flow_completed"),
        )
        assert transition_result.wakeup_intents[0].event_type == "flow_completed"

        timer_result = reduce_timer_fired(TimerFiredEvent(timer_id="t-1"))
        assert timer_result.wakeup_intent.event_type == "timer_fired"


class TestTimerReducerMetadataConsistency:
    def test_timer_metadata_includes_domain_source_marker(self):
        result = reduce_timer_fired(TimerFiredEvent(timer_id="t-1"))
        assert result.wakeup_intent.metadata["domain_source"] == "timer_reducer"

    def test_timer_metadata_preserves_original_metadata(self):
        result = reduce_timer_fired(
            TimerFiredEvent(
                timer_id="t-1",
                metadata={
                    "custom_key": "custom_val",
                    "delivery_target": {"surface_kind": "discord"},
                },
            )
        )
        meta = result.wakeup_intent.metadata
        assert meta["custom_key"] == "custom_val"
        assert meta["delivery_target"] == {"surface_kind": "discord"}
        assert meta["timer_type"] == TIMER_TYPE_ONE_SHOT

    def test_watchdog_timer_metadata_reflects_watchdog_type(self):
        result = reduce_timer_fired(
            TimerFiredEvent(
                timer_id="t-1",
                timer_type=TIMER_TYPE_WATCHDOG,
            )
        )
        assert result.wakeup_intent.metadata["timer_type"] == TIMER_TYPE_WATCHDOG


class TestTransitionReducerSubscriptionIntegration:
    def test_subscription_metadata_merged_with_event_metadata(self):
        result = reduce_transition(
            [
                _sub(
                    subscription_id="sub-1",
                    metadata={
                        "delivery_target": {"surface_kind": "discord"},
                        "pma_origin": {"thread_id": "origin-t-1"},
                    },
                )
            ],
            frozenset(),
            TransitionEvent(
                event_type="flow_completed",
                extra_metadata={"origin": "lifecycle"},
            ),
        )
        meta = result.wakeup_intents[0].metadata
        assert meta["delivery_target"] == {"surface_kind": "discord"}
        assert meta["pma_origin"] == {"thread_id": "origin-t-1"}
        assert meta["origin"] == "lifecycle"

    def test_multiple_matching_subscriptions_produce_separate_wakeups(self):
        result = reduce_transition(
            [
                _sub(subscription_id="sub-1", lane_id="discord"),
                _sub(subscription_id="sub-2", lane_id="telegram"),
            ],
            frozenset(),
            TransitionEvent(event_type="flow_completed"),
        )
        assert len(result.wakeup_intents) == 2
        assert result.wakeup_intents[0].lane_id == "discord"
        assert result.wakeup_intents[1].lane_id == "telegram"
