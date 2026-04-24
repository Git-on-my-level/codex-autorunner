"""Reducer matrix: exhaustive parametrized coverage of the subscription reducer.

This file encodes the full cross-product of subscription dimensions against
transition events so that any future policy change that alters matching
semantics will break a specific matrix cell rather than an ad hoc test.

Matrix dimensions:
  - subscription state: active, cancelled, pre-exhausted
  - scope filters: event_type, repo_id, run_id, thread_id, from_state, to_state
  - match boundary: max_matches (None, 1, 3), match_count at boundary
  - idempotency: fresh key vs duplicate key
  - metadata merge: subscription metadata, event extra_metadata, overlap
"""

from __future__ import annotations

from typing import Any

import pytest

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
    subscription_id: str = "sub-matrix",
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
    metadata: dict[str, Any] | None = None,
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


def _event(
    *,
    event_type: str = "flow_completed",
    repo_id: str | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    reason: str = "transition",
    transition_id: str | None = "trans-matrix",
    extra_metadata: dict[str, Any] | None = None,
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


# ---------------------------------------------------------------------------
# Matrix: subscription state × event match
# ---------------------------------------------------------------------------


class TestStateMatrix:
    @pytest.mark.parametrize(
        "sub_state,expect_match",
        [
            (SUBSCRIPTION_STATE_ACTIVE, True),
            (SUBSCRIPTION_STATE_CANCELLED, False),
        ],
    )
    def test_state_gate(self, sub_state: str, expect_match: bool) -> None:
        result = reduce_transition(
            [_sub(state=sub_state)],
            frozenset(),
            _event(),
        )
        assert result.matched == (1 if expect_match else 0)
        assert result.created == (1 if expect_match else 0)

    def test_pre_exhausted_subscription_auto_cancels(self) -> None:
        result = reduce_transition(
            [_sub(max_matches=3, match_count=3)],
            frozenset(),
            _event(),
        )
        assert result.matched == 0
        assert result.subscriptions[0].state == SUBSCRIPTION_STATE_CANCELLED

    def test_pre_exhausted_subscription_auto_cancels_even_if_active(self) -> None:
        sub = _sub(
            state=SUBSCRIPTION_STATE_ACTIVE,
            max_matches=1,
            match_count=1,
        )
        result = reduce_transition([sub], frozenset(), _event())
        assert result.matched == 0
        assert result.subscriptions[0].state == SUBSCRIPTION_STATE_CANCELLED


# ---------------------------------------------------------------------------
# Matrix: scope dimension filters
# ---------------------------------------------------------------------------


class TestScopeFilterMatrix:
    @pytest.mark.parametrize(
        "sub_kw,event_kw,expect_match",
        [
            ({"repo_id": "r1"}, {"repo_id": "r1"}, True),
            ({"repo_id": "r1"}, {"repo_id": "r2"}, False),
            ({"repo_id": "r1"}, {"repo_id": "r1"}, True),
            ({"repo_id": None}, {"repo_id": "r1"}, True),
            ({"repo_id": None}, {"repo_id": None}, True),
            ({"run_id": "u1"}, {"run_id": "u1"}, True),
            ({"run_id": "u1"}, {"run_id": "u2"}, False),
            ({"run_id": None}, {"run_id": "u1"}, True),
            ({"thread_id": "t1"}, {"thread_id": "t1"}, True),
            ({"thread_id": "t1"}, {"thread_id": "t2"}, False),
            ({"thread_id": None}, {"thread_id": "t1"}, True),
            ({"from_state": "running"}, {"from_state": "running"}, True),
            ({"from_state": "running"}, {"from_state": "pending"}, False),
            ({"from_state": None}, {"from_state": "running"}, True),
            ({"to_state": "completed"}, {"to_state": "completed"}, True),
            ({"to_state": "completed"}, {"to_state": "failed"}, False),
            ({"to_state": None}, {"to_state": "completed"}, True),
        ],
    )
    def test_single_scope_dimension(
        self,
        sub_kw: dict[str, Any],
        event_kw: dict[str, Any],
        expect_match: bool,
    ) -> None:
        result = reduce_transition(
            [_sub(**sub_kw)],
            frozenset(),
            _event(**event_kw),
        )
        assert result.matched == (1 if expect_match else 0)

    def test_all_scope_dimensions_must_match(self) -> None:
        sub = _sub(
            repo_id="r1",
            run_id="u1",
            thread_id="t1",
            from_state="running",
            to_state="completed",
        )
        result = reduce_transition(
            [sub],
            frozenset(),
            _event(
                repo_id="r1",
                run_id="u1",
                thread_id="t1",
                from_state="running",
                to_state="completed",
            ),
        )
        assert result.matched == 1

    def test_single_scope_mismatch_blocks_all(self) -> None:
        sub = _sub(
            repo_id="r1",
            run_id="u1",
            thread_id="t1",
        )
        result = reduce_transition(
            [sub],
            frozenset(),
            _event(
                repo_id="r1",
                run_id="u1",
                thread_id="WRONG",
            ),
        )
        assert result.matched == 0


# ---------------------------------------------------------------------------
# Matrix: event_type filter
# ---------------------------------------------------------------------------


class TestEventTypeMatrix:
    @pytest.mark.parametrize(
        "sub_types,event_type,expect_match",
        [
            (("flow_completed",), "flow_completed", True),
            (("flow_completed",), "flow_failed", False),
            (("flow_completed", "flow_failed"), "flow_failed", True),
            (("flow_completed", "flow_failed"), "flow_completed", True),
            (("managed_thread_completed",), "managed_thread_completed", True),
            ((), "flow_completed", True),
            ((), "anything", True),
        ],
    )
    def test_event_type_filter(
        self,
        sub_types: tuple[str, ...],
        event_type: str,
        expect_match: bool,
    ) -> None:
        result = reduce_transition(
            [_sub(event_types=sub_types)],
            frozenset(),
            _event(event_type=event_type),
        )
        assert result.matched == (1 if expect_match else 0)


# ---------------------------------------------------------------------------
# Matrix: match count and exhaustion boundary
# ---------------------------------------------------------------------------


class TestExhaustionBoundaryMatrix:
    @pytest.mark.parametrize(
        "max_matches,match_count,expect_cancelled_after_match",
        [
            (None, 0, False),
            (None, 100, False),
            (1, 0, True),
            (1, 1, True),
            (3, 0, False),
            (3, 1, False),
            (3, 2, True),
            (3, 3, True),
        ],
    )
    def test_match_count_boundary(
        self,
        max_matches: int | None,
        match_count: int,
        expect_cancelled_after_match: bool,
    ) -> None:
        result = reduce_transition(
            [_sub(max_matches=max_matches, match_count=match_count)],
            frozenset(),
            _event(),
        )
        if expect_cancelled_after_match:
            assert result.subscriptions[0].state == SUBSCRIPTION_STATE_CANCELLED
        else:
            assert result.subscriptions[0].state == SUBSCRIPTION_STATE_ACTIVE

    def test_match_count_increments_correctly(self) -> None:
        for initial_count in (0, 5, 99):
            result = reduce_transition(
                [_sub(match_count=initial_count)],
                frozenset(),
                _event(),
            )
            assert result.subscriptions[0].match_count == initial_count + 1


# ---------------------------------------------------------------------------
# Matrix: idempotency (existing wakeup keys)
# ---------------------------------------------------------------------------


class TestIdempotencyMatrix:
    def test_fresh_key_creates_wakeup(self) -> None:
        result = reduce_transition(
            [_sub(subscription_id="s1")],
            frozenset(),
            _event(transition_id="t1"),
        )
        assert result.created == 1
        assert result.wakeup_intents[0].idempotency_key == "transition:t1:s1"

    def test_duplicate_key_suppresses_creation(self) -> None:
        dup_key = "transition:t1:s1"
        result = reduce_transition(
            [_sub(subscription_id="s1")],
            frozenset({dup_key}),
            _event(transition_id="t1"),
        )
        assert result.matched == 1
        assert result.created == 0
        assert len(result.wakeup_intents) == 0

    def test_different_sub_same_event_creates_both(self) -> None:
        result = reduce_transition(
            [_sub(subscription_id="s1"), _sub(subscription_id="s2")],
            frozenset(),
            _event(transition_id="t1"),
        )
        assert result.created == 2
        keys = {wi.idempotency_key for wi in result.wakeup_intents}
        assert keys == {"transition:t1:s1", "transition:t1:s2"}

    def test_one_duplicate_one_fresh(self) -> None:
        result = reduce_transition(
            [_sub(subscription_id="s1"), _sub(subscription_id="s2")],
            frozenset({"transition:t1:s1"}),
            _event(transition_id="t1"),
        )
        assert result.matched == 2
        assert result.created == 1
        assert result.wakeup_intents[0].subscription_id == "s2"


# ---------------------------------------------------------------------------
# Matrix: metadata merge behavior
# ---------------------------------------------------------------------------


class TestMetadataMergeMatrix:
    @pytest.mark.parametrize(
        "sub_meta,event_meta,expected_key,expected_val",
        [
            ({"a": "1"}, {"b": "2"}, "a", "1"),
            ({"a": "1"}, {"b": "2"}, "b", "2"),
            ({"k": "sub"}, {"k": "evt"}, "k", "evt"),
            ({}, {}, "missing", None),
            ({"origin": {"thread_id": "t1"}}, {}, "origin", {"thread_id": "t1"}),
        ],
    )
    def test_metadata_merge_semantics(
        self,
        sub_meta: dict[str, Any],
        event_meta: dict[str, Any],
        expected_key: str,
        expected_val: Any,
    ) -> None:
        result = reduce_transition(
            [_sub(metadata=sub_meta)],
            frozenset(),
            _event(extra_metadata=event_meta),
        )
        assert result.created == 1
        meta = result.wakeup_intents[0].metadata
        if expected_val is None:
            assert expected_key not in meta
        else:
            assert meta[expected_key] == expected_val

    def test_subscription_metadata_not_mutated(self) -> None:
        original = {"key": "val", "nested": {"a": 1}}
        sub = _sub(metadata=dict(original))
        reduce_transition(
            [sub], frozenset(), _event(extra_metadata={"key": "override"})
        )
        assert sub.metadata == original


# ---------------------------------------------------------------------------
# Matrix: multiple subscriptions with mixed characteristics
# ---------------------------------------------------------------------------


class TestMultiSubscriptionMatrix:
    def test_five_subscriptions_mixed_matching(self) -> None:
        subs = [
            _sub(subscription_id="s-active-match", repo_id="repo-1"),
            _sub(subscription_id="s-active-no-match", repo_id="repo-2"),
            _sub(subscription_id="s-cancelled", state=SUBSCRIPTION_STATE_CANCELLED),
            _sub(subscription_id="s-wildcard"),
            _sub(
                subscription_id="s-exhausted",
                max_matches=1,
                match_count=1,
            ),
        ]
        result = reduce_transition(subs, frozenset(), _event(repo_id="repo-1"))
        assert result.matched == 2
        assert result.created == 2
        matched_ids = {wi.subscription_id for wi in result.wakeup_intents}
        assert matched_ids == {"s-active-match", "s-wildcard"}

        states = {s.subscription_id: s.state for s in result.subscriptions}
        assert states["s-active-no-match"] == SUBSCRIPTION_STATE_ACTIVE
        assert states["s-cancelled"] == SUBSCRIPTION_STATE_CANCELLED
        assert states["s-exhausted"] == SUBSCRIPTION_STATE_CANCELLED

    def test_consecutive_reduces_on_same_subscriptions(self) -> None:
        subs = [_sub(subscription_id="s1", max_matches=2, match_count=0)]
        event1 = _event(transition_id="t1")
        r1 = reduce_transition(subs, frozenset(), event1)
        assert r1.created == 1

        subs2 = list(r1.subscriptions)
        event2 = _event(transition_id="t2")
        existing_keys = frozenset(wi.idempotency_key for wi in r1.wakeup_intents)
        r2 = reduce_transition(subs2, existing_keys, event2)
        assert r2.created == 1

        assert r2.subscriptions[0].match_count == 2
        assert r2.subscriptions[0].state == SUBSCRIPTION_STATE_CANCELLED

        r3 = reduce_transition(
            list(r2.subscriptions),
            existing_keys | {wi.idempotency_key for wi in r2.wakeup_intents},
            _event(transition_id="t3"),
        )
        assert r3.matched == 0
        assert r3.created == 0


# ---------------------------------------------------------------------------
# Matrix: wakeup intent field correctness
# ---------------------------------------------------------------------------


class TestWakeupIntentFieldMatrix:
    @pytest.mark.parametrize(
        "event_kw,expected_field,expected_value",
        [
            ({"event_type": "flow_completed"}, "event_type", "flow_completed"),
            ({"repo_id": "repo-x"}, "repo_id", "repo-x"),
            ({"run_id": "run-x"}, "run_id", "run-x"),
            ({"thread_id": "thread-x"}, "thread_id", "thread-x"),
            ({"from_state": "idle"}, "from_state", "idle"),
            ({"to_state": "completed"}, "to_state", "completed"),
            ({"reason": "watchdog_triggered"}, "reason", "watchdog_triggered"),
        ],
    )
    def test_wakeup_inherits_event_fields(
        self,
        event_kw: dict[str, Any],
        expected_field: str,
        expected_value: str,
    ) -> None:
        result = reduce_transition([_sub()], frozenset(), _event(**event_kw))
        assert result.created == 1
        intent = result.wakeup_intents[0]
        assert getattr(intent, expected_field) == expected_value

    def test_wakeup_source_is_transition(self) -> None:
        result = reduce_transition([_sub()], frozenset(), _event())
        assert result.wakeup_intents[0].source == "transition"

    def test_wakeup_lane_comes_from_subscription_not_event(self) -> None:
        result = reduce_transition(
            [_sub(lane_id="telegram")],
            frozenset(),
            _event(),
        )
        assert result.wakeup_intents[0].lane_id == "telegram"

    def test_wakeup_event_data_from_extra_metadata(self) -> None:
        result = reduce_transition(
            [_sub()],
            frozenset(),
            _event(
                extra_metadata={
                    "event_id": "evt-123",
                    "event_data": {"key": "val"},
                },
            ),
        )
        intent = result.wakeup_intents[0]
        assert intent.event_id == "evt-123"
        assert intent.event_data == {"key": "val"}
