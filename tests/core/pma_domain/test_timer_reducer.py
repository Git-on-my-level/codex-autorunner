"""Timer reducer tests: coverage for reduce_timer_fired.

The timer reducer converts timer-fired events into wakeup intents.  These
tests cover one-shot timers, watchdog timers, metadata propagation,
idempotency key construction, and lane inheritance.
"""

from __future__ import annotations

from typing import Any

import pytest

from codex_autorunner.core.pma_domain.constants import (
    DEFAULT_PMA_LANE_ID,
    TIMER_TYPE_ONE_SHOT,
    TIMER_TYPE_WATCHDOG,
)
from codex_autorunner.core.pma_domain.subscription_reducer import (
    TimerFiredEvent,
    reduce_timer_fired,
)


def _timer_event(
    *,
    timer_id: str = "timer-1",
    timer_type: str = TIMER_TYPE_ONE_SHOT,
    fired_at: str = "2026-01-15T12:00:00Z",
    repo_id: str | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    lane_id: str = DEFAULT_PMA_LANE_ID,
    from_state: str | None = None,
    to_state: str | None = None,
    reason: str | None = None,
    subscription_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TimerFiredEvent:
    return TimerFiredEvent(
        timer_id=timer_id,
        timer_type=timer_type,
        fired_at=fired_at,
        repo_id=repo_id,
        run_id=run_id,
        thread_id=thread_id,
        lane_id=lane_id,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        subscription_id=subscription_id,
        metadata=metadata or {},
    )


class TestTimerReducerBasic:
    def test_one_shot_produces_wakeup(self) -> None:
        event = _timer_event()
        result = reduce_timer_fired(event)
        assert result.wakeup_intent is not None
        assert result.timer_id == "timer-1"
        assert result.source == "timer"

    def test_watchdog_produces_wakeup(self) -> None:
        event = _timer_event(timer_type=TIMER_TYPE_WATCHDOG)
        result = reduce_timer_fired(event)
        assert result.wakeup_intent is not None

    def test_wakeup_source_is_timer(self) -> None:
        result = reduce_timer_fired(_timer_event())
        assert result.wakeup_intent.source == "timer"

    def test_wakeup_event_type_is_timer_fired(self) -> None:
        result = reduce_timer_fired(_timer_event())
        assert result.wakeup_intent.event_type == "timer_fired"


class TestTimerReducerFieldPropagation:
    @pytest.mark.parametrize(
        "event_kw,field,expected",
        [
            ({"repo_id": "repo-1"}, "repo_id", "repo-1"),
            ({"run_id": "run-1"}, "run_id", "run-1"),
            ({"thread_id": "thread-1"}, "thread_id", "thread-1"),
            ({"from_state": "running"}, "from_state", "running"),
            ({"to_state": "idle"}, "to_state", "idle"),
            ({"lane_id": "discord"}, "lane_id", "discord"),
            ({"subscription_id": "sub-1"}, "subscription_id", "sub-1"),
        ],
    )
    def test_scope_fields_propagate(
        self,
        event_kw: dict[str, Any],
        field: str,
        expected: str,
    ) -> None:
        result = reduce_timer_fired(_timer_event(**event_kw))
        assert getattr(result.wakeup_intent, field) == expected

    def test_reason_propagates(self) -> None:
        result = reduce_timer_fired(_timer_event(reason="idle_watchdog"))
        assert result.wakeup_intent.reason == "idle_watchdog"

    def test_default_reason_is_timer_due(self) -> None:
        result = reduce_timer_fired(_timer_event(reason=None))
        assert result.wakeup_intent.reason == "timer_due"

    def test_fired_at_propagates_as_timestamp(self) -> None:
        result = reduce_timer_fired(_timer_event(fired_at="2026-03-15T08:30:00Z"))
        assert result.wakeup_intent.timestamp == "2026-03-15T08:30:00Z"

    def test_empty_fired_at_gives_empty_timestamp(self) -> None:
        result = reduce_timer_fired(_timer_event(fired_at=""))
        assert result.wakeup_intent.timestamp == ""


class TestTimerReducerIdempotency:
    def test_idempotency_key_includes_timer_id(self) -> None:
        result = reduce_timer_fired(_timer_event(timer_id="t-42"))
        assert "t-42" in result.wakeup_intent.idempotency_key

    def test_idempotency_key_includes_fired_at(self) -> None:
        result = reduce_timer_fired(_timer_event(fired_at="2026-06-01T00:00:00Z"))
        assert "2026-06-01T00:00:00Z" in result.wakeup_intent.idempotency_key

    def test_idempotency_key_format(self) -> None:
        result = reduce_timer_fired(
            _timer_event(timer_id="abc", fired_at="2026-01-01T00:00:00Z")
        )
        assert result.wakeup_intent.idempotency_key == "timer:abc:2026-01-01T00:00:00Z"

    def test_idempotency_key_deterministic(self) -> None:
        event = _timer_event(timer_id="same", fired_at="2026-01-01T00:00:00Z")
        r1 = reduce_timer_fired(event)
        r2 = reduce_timer_fired(event)
        assert r1.wakeup_intent.idempotency_key == r2.wakeup_intent.idempotency_key

    def test_different_fired_at_produces_different_keys(self) -> None:
        r1 = reduce_timer_fired(_timer_event(fired_at="2026-01-01T00:00:00Z"))
        r2 = reduce_timer_fired(_timer_event(fired_at="2026-01-01T01:00:00Z"))
        assert r1.wakeup_intent.idempotency_key != r2.wakeup_intent.idempotency_key


class TestTimerReducerMetadata:
    def test_timer_type_in_metadata(self) -> None:
        result = reduce_timer_fired(_timer_event(timer_type=TIMER_TYPE_WATCHDOG))
        assert result.wakeup_intent.metadata["timer_type"] == TIMER_TYPE_WATCHDOG

    def test_domain_source_in_metadata(self) -> None:
        result = reduce_timer_fired(_timer_event())
        assert result.wakeup_intent.metadata["domain_source"] == "timer_reducer"

    def test_original_metadata_preserved(self) -> None:
        result = reduce_timer_fired(_timer_event(metadata={"custom_key": "custom_val"}))
        assert result.wakeup_intent.metadata["custom_key"] == "custom_val"

    def test_timer_type_does_not_overwrite_existing(self) -> None:
        result = reduce_timer_fired(
            _timer_event(
                timer_type=TIMER_TYPE_WATCHDOG,
                metadata={"timer_type": "should_be_overwritten"},
            )
        )
        assert result.wakeup_intent.metadata["timer_type"] == TIMER_TYPE_WATCHDOG


class TestTimerReducerSubscriptionLink:
    def test_subscription_id_propagated_to_wakeup(self) -> None:
        result = reduce_timer_fired(_timer_event(subscription_id="sub-99"))
        assert result.wakeup_intent.subscription_id == "sub-99"

    def test_no_subscription_id_is_none(self) -> None:
        result = reduce_timer_fired(_timer_event(subscription_id=None))
        assert result.wakeup_intent.subscription_id is None


class TestTimerReducerWatchdogSemantics:
    def test_watchdog_idle_timer_produces_wakeup(self) -> None:
        event = _timer_event(
            timer_type=TIMER_TYPE_WATCHDOG,
            reason="idle_watchdog",
            subscription_id="sub-watch",
            metadata={"idle_seconds": 300},
        )
        result = reduce_timer_fired(event)
        assert result.wakeup_intent.source == "timer"
        assert result.wakeup_intent.subscription_id == "sub-watch"
        assert result.wakeup_intent.metadata["timer_type"] == TIMER_TYPE_WATCHDOG

    def test_one_shot_timer_uses_default_type_in_metadata(self) -> None:
        event = _timer_event(timer_type=TIMER_TYPE_ONE_SHOT)
        result = reduce_timer_fired(event)
        assert result.wakeup_intent.metadata["timer_type"] == TIMER_TYPE_ONE_SHOT
