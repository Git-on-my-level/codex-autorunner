from __future__ import annotations

from codex_autorunner.core.pma_domain.constants import (
    DEFAULT_PMA_LANE_ID,
    SUBSCRIPTION_STATE_ACTIVE,
    SUBSCRIPTION_STATE_CANCELLED,
)
from codex_autorunner.core.pma_domain.models import PmaSubscription
from codex_autorunner.core.pma_domain.subscription_reducer import (
    TransitionEvent,
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
    reason: str | None = None,
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
        reason=reason,
        max_matches=max_matches,
        match_count=match_count,
        metadata=metadata or {},
    )


def _event(
    *,
    event_type: str = "flow_completed",
    repo_id: str | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    reason: str = "transition",
    transition_id: str | None = None,
    extra_metadata: dict | None = None,
) -> TransitionEvent:
    return TransitionEvent(
        event_type=event_type,
        repo_id=repo_id,
        run_id=run_id,
        thread_id=thread_id,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        transition_id=transition_id,
        extra_metadata=extra_metadata or {},
    )


class TestSubscriptionMatchingScope:
    def test_no_subscriptions_yields_empty(self):
        result = reduce_transition([], frozenset(), _event())
        assert result.matched == 0
        assert result.created == 0
        assert result.subscriptions == ()
        assert result.wakeup_intents == ()

    def test_event_type_mismatch(self):
        result = reduce_transition(
            [_sub(event_types=("flow_failed",))],
            frozenset(),
            _event(event_type="flow_completed"),
        )
        assert result.matched == 0
        assert result.created == 0

    def test_event_type_match(self):
        result = reduce_transition(
            [_sub(event_types=("flow_completed",))],
            frozenset(),
            _event(event_type="flow_completed"),
        )
        assert result.matched == 1
        assert result.created == 1

    def test_empty_event_types_matches_all(self):
        result = reduce_transition(
            [_sub(event_types=())],
            frozenset(),
            _event(event_type="anything"),
        )
        assert result.matched == 1
        assert result.created == 1

    def test_repo_id_mismatch(self):
        result = reduce_transition(
            [_sub(repo_id="repo-1")],
            frozenset(),
            _event(repo_id="repo-2"),
        )
        assert result.matched == 0

    def test_repo_id_match(self):
        result = reduce_transition(
            [_sub(repo_id="repo-1")],
            frozenset(),
            _event(repo_id="repo-1"),
        )
        assert result.matched == 1

    def test_repo_id_wildcard_sub_matches_any_repo(self):
        result = reduce_transition(
            [_sub(repo_id=None)],
            frozenset(),
            _event(repo_id="any-repo"),
        )
        assert result.matched == 1

    def test_run_id_mismatch(self):
        result = reduce_transition(
            [_sub(run_id="run-1")],
            frozenset(),
            _event(run_id="run-2"),
        )
        assert result.matched == 0

    def test_run_id_match(self):
        result = reduce_transition(
            [_sub(run_id="run-1")],
            frozenset(),
            _event(run_id="run-1"),
        )
        assert result.matched == 1

    def test_thread_id_mismatch(self):
        result = reduce_transition(
            [_sub(thread_id="thread-1")],
            frozenset(),
            _event(thread_id="thread-2"),
        )
        assert result.matched == 0

    def test_thread_id_match(self):
        result = reduce_transition(
            [_sub(thread_id="thread-1")],
            frozenset(),
            _event(thread_id="thread-1"),
        )
        assert result.matched == 1

    def test_from_state_mismatch(self):
        result = reduce_transition(
            [_sub(from_state="running")],
            frozenset(),
            _event(from_state="pending"),
        )
        assert result.matched == 0

    def test_from_state_match(self):
        result = reduce_transition(
            [_sub(from_state="running")],
            frozenset(),
            _event(from_state="running"),
        )
        assert result.matched == 1

    def test_to_state_mismatch(self):
        result = reduce_transition(
            [_sub(to_state="completed")],
            frozenset(),
            _event(to_state="failed"),
        )
        assert result.matched == 0

    def test_to_state_match(self):
        result = reduce_transition(
            [_sub(to_state="completed")],
            frozenset(),
            _event(to_state="completed"),
        )
        assert result.matched == 1

    def test_all_scope_dimensions_match(self):
        result = reduce_transition(
            [
                _sub(
                    event_types=("flow_completed",),
                    repo_id="repo-1",
                    run_id="run-1",
                    thread_id="thread-1",
                    from_state="running",
                    to_state="completed",
                )
            ],
            frozenset(),
            _event(
                event_type="flow_completed",
                repo_id="repo-1",
                run_id="run-1",
                thread_id="thread-1",
                from_state="running",
                to_state="completed",
            ),
        )
        assert result.matched == 1
        assert result.created == 1

    def test_partial_scope_mismatch(self):
        result = reduce_transition(
            [
                _sub(
                    repo_id="repo-1",
                    run_id="run-1",
                    to_state="completed",
                )
            ],
            frozenset(),
            _event(
                repo_id="repo-1",
                run_id="run-2",
                to_state="completed",
            ),
        )
        assert result.matched == 0


class TestSubscriptionState:
    def test_cancelled_subscription_skipped(self):
        result = reduce_transition(
            [_sub(state=SUBSCRIPTION_STATE_CANCELLED)],
            frozenset(),
            _event(),
        )
        assert result.matched == 0
        assert result.subscriptions[0].state == SUBSCRIPTION_STATE_CANCELLED

    def test_pre_exhausted_subscription_cancelled(self):
        result = reduce_transition(
            [_sub(max_matches=2, match_count=2)],
            frozenset(),
            _event(),
        )
        assert result.matched == 0
        assert result.subscriptions[0].state == SUBSCRIPTION_STATE_CANCELLED

    def test_active_subscription_matches(self):
        result = reduce_transition(
            [_sub()],
            frozenset(),
            _event(),
        )
        assert result.matched == 1
        assert result.created == 1


class TestDedup:
    def test_duplicate_wakeup_key_skipped(self):
        sub = _sub(subscription_id="sub-1")
        event = _event(transition_id="trans-1")
        expected_key = "transition:trans-1:sub-1"
        existing = frozenset({expected_key})

        result = reduce_transition([sub], existing, event)
        assert result.matched == 1
        assert result.created == 0

    def test_different_transition_id_creates_wakeup(self):
        sub = _sub(subscription_id="sub-1")
        existing = frozenset({"transition:trans-0:sub-1"})

        result = reduce_transition([sub], existing, _event(transition_id="trans-1"))
        assert result.matched == 1
        assert result.created == 1

    def test_no_existing_keys_no_dedup(self):
        result = reduce_transition(
            [_sub()], frozenset(), _event(transition_id="trans-1")
        )
        assert result.matched == 1
        assert result.created == 1

    def test_wakeup_key_uses_all_scope_when_no_transition_id(self):
        sub = _sub(subscription_id="sub-1")
        event = _event(
            event_type="flow_completed",
            repo_id="repo-1",
            run_id="run-1",
            thread_id="thread-1",
            from_state="running",
            to_state="completed",
            transition_id=None,
        )
        result = reduce_transition([sub], frozenset(), event)
        assert result.created == 1
        key = result.wakeup_intents[0].idempotency_key
        assert "transition:" in key
        assert "flow_completed" in key
        assert "repo-1" in key


class TestLaneResolution:
    def test_wakeup_inherits_subscription_lane_id(self):
        result = reduce_transition(
            [_sub(lane_id="discord")],
            frozenset(),
            _event(),
        )
        assert len(result.wakeup_intents) == 1
        assert result.wakeup_intents[0].lane_id == "discord"

    def test_wakeup_uses_default_lane_when_subscription_has_default(self):
        result = reduce_transition(
            [_sub()],
            frozenset(),
            _event(),
        )
        assert result.wakeup_intents[0].lane_id == DEFAULT_PMA_LANE_ID

    def test_wakeup_does_not_use_event_scope_for_lane(self):
        result = reduce_transition(
            [_sub(lane_id="telegram")],
            frozenset(),
            _event(),
        )
        assert result.wakeup_intents[0].lane_id == "telegram"


class TestOriginMetadata:
    def test_subscription_metadata_copies_to_wakeup(self):
        result = reduce_transition(
            [
                _sub(
                    metadata={
                        "delivery_target": {
                            "surface_kind": "discord",
                            "surface_key": "discord:ch-1",
                        },
                        "pma_origin": {
                            "thread_id": "origin-thread-1",
                            "lane_id": "discord",
                            "agent": "hermes",
                            "profile": "m4-pma",
                        },
                    }
                )
            ],
            frozenset(),
            _event(),
        )
        assert len(result.wakeup_intents) == 1
        meta = result.wakeup_intents[0].metadata
        assert meta["delivery_target"] == {
            "surface_kind": "discord",
            "surface_key": "discord:ch-1",
        }
        assert meta["pma_origin"] == {
            "thread_id": "origin-thread-1",
            "lane_id": "discord",
            "agent": "hermes",
            "profile": "m4-pma",
        }

    def test_event_extra_metadata_merges_into_wakeup(self):
        result = reduce_transition(
            [_sub(metadata={"existing": "value"})],
            frozenset(),
            _event(extra_metadata={"extra_key": "extra_val"}),
        )
        meta = result.wakeup_intents[0].metadata
        assert meta["existing"] == "value"
        assert meta["extra_key"] == "extra_val"

    def test_event_metadata_overwrites_subscription_metadata(self):
        result = reduce_transition(
            [_sub(metadata={"key": "from_sub"})],
            frozenset(),
            _event(extra_metadata={"key": "from_event"}),
        )
        assert result.wakeup_intents[0].metadata["key"] == "from_event"


class TestWakeupEmission:
    def test_wakeup_intent_fields_match_event(self):
        result = reduce_transition(
            [_sub()],
            frozenset(),
            _event(
                event_type="flow_completed",
                repo_id="repo-1",
                run_id="run-1",
                thread_id="thread-1",
                from_state="running",
                to_state="completed",
                reason="completion",
                transition_id="trans-1",
            ),
        )
        intent = result.wakeup_intents[0]
        assert intent.source == "transition"
        assert intent.repo_id == "repo-1"
        assert intent.run_id == "run-1"
        assert intent.thread_id == "thread-1"
        assert intent.from_state == "running"
        assert intent.to_state == "completed"
        assert intent.reason == "completion"
        assert intent.event_type == "flow_completed"
        assert intent.subscription_id == "sub-1"
        assert intent.idempotency_key == "transition:trans-1:sub-1"

    def test_subscription_reason_overrides_event_reason(self):
        result = reduce_transition(
            [_sub(reason="from-subscription")],
            frozenset(),
            _event(reason="from-event"),
        )
        assert result.wakeup_intents[0].reason == "from-subscription"

    def test_multiple_matching_subscriptions_produce_multiple_wakeups(self):
        result = reduce_transition(
            [
                _sub(subscription_id="sub-1"),
                _sub(subscription_id="sub-2"),
                _sub(subscription_id="sub-3"),
            ],
            frozenset(),
            _event(event_type="flow_completed"),
        )
        assert result.matched == 3
        assert result.created == 3
        assert len(result.wakeup_intents) == 3
        ids = {wi.subscription_id for wi in result.wakeup_intents}
        assert ids == {"sub-1", "sub-2", "sub-3"}


class TestMatchCountAndExhaustion:
    def test_match_count_incremented(self):
        result = reduce_transition(
            [_sub(match_count=3)],
            frozenset(),
            _event(),
        )
        assert result.subscriptions[0].match_count == 4

    def test_subscription_cancelled_on_exhaustion(self):
        result = reduce_transition(
            [_sub(max_matches=1, match_count=0)],
            frozenset(),
            _event(),
        )
        assert result.subscriptions[0].match_count == 1
        assert result.subscriptions[0].state == SUBSCRIPTION_STATE_CANCELLED

    def test_subscription_not_cancelled_before_exhaustion(self):
        result = reduce_transition(
            [_sub(max_matches=3, match_count=1)],
            frozenset(),
            _event(),
        )
        assert result.subscriptions[0].match_count == 2
        assert result.subscriptions[0].state == SUBSCRIPTION_STATE_ACTIVE

    def test_max_matches_none_never_exhausts(self):
        result = reduce_transition(
            [_sub(max_matches=None, match_count=999)],
            frozenset(),
            _event(),
        )
        assert result.subscriptions[0].match_count == 1000
        assert result.subscriptions[0].state == SUBSCRIPTION_STATE_ACTIVE


class TestNonMatchingSubscriptionsUnchanged:
    def test_non_matching_subscriptions_preserved_verbatim(self):
        sub = _sub(
            subscription_id="sub-nope",
            repo_id="repo-1",
            match_count=5,
        )
        result = reduce_transition(
            [sub],
            frozenset(),
            _event(repo_id="repo-2"),
        )
        assert len(result.subscriptions) == 1
        out = result.subscriptions[0]
        assert out.subscription_id == "sub-nope"
        assert out.match_count == 5
        assert out.state == SUBSCRIPTION_STATE_ACTIVE


class TestMultipleSubscriptionsMixed:
    def test_mixed_matching_and_non_matching(self):
        result = reduce_transition(
            [
                _sub(subscription_id="sub-match", repo_id="repo-1"),
                _sub(subscription_id="sub-skip", repo_id="repo-2"),
                _sub(
                    subscription_id="sub-cancelled",
                    state=SUBSCRIPTION_STATE_CANCELLED,
                ),
                _sub(subscription_id="sub-wild"),
            ],
            frozenset(),
            _event(event_type="flow_completed", repo_id="repo-1"),
        )
        assert result.matched == 2
        assert result.created == 2
        ids = {wi.subscription_id for wi in result.wakeup_intents}
        assert ids == {"sub-match", "sub-wild"}


class TestImmutability:
    def test_input_subscriptions_not_mutated(self):
        sub = _sub(match_count=0)
        reduce_transition([sub], frozenset(), _event())
        assert sub.match_count == 0
        assert sub.state == SUBSCRIPTION_STATE_ACTIVE

    def test_result_subscriptions_are_new_objects(self):
        sub = _sub()
        result = reduce_transition([sub], frozenset(), _event())
        assert result.subscriptions[0] is not sub


class TestWakeupKeyConstruction:
    def test_uses_transition_id_when_present(self):
        event = _event(transition_id="my-trans-id")
        result = reduce_transition([_sub(subscription_id="s1")], frozenset(), event)
        assert result.wakeup_intents[0].idempotency_key == "transition:my-trans-id:s1"

    def test_uses_scope_composite_when_no_transition_id(self):
        event = _event(
            event_type="ev",
            repo_id="r",
            run_id="u",
            thread_id="t",
            from_state="f",
            to_state="to",
            transition_id=None,
        )
        result = reduce_transition(
            [_sub(subscription_id="s1", event_types=("ev",))],
            frozenset(),
            event,
        )
        key = result.wakeup_intents[0].idempotency_key
        assert key == "transition:ev:r:u:t:f:to:s1"

    def test_uses_all_placeholder_when_no_transition_id_and_no_scope(self):
        event = _event(transition_id=None, event_type="transition")
        result = reduce_transition(
            [_sub(subscription_id="s1", event_types=("transition",))],
            frozenset(),
            event,
        )
        key = result.wakeup_intents[0].idempotency_key
        assert key == "transition:transition::::::s1"
