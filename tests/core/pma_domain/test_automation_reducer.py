from __future__ import annotations

from datetime import datetime, timedelta, timezone

from codex_autorunner.core.pma_domain.automation_reducer import (
    reduce_dequeue_due_timers,
    reduce_timer_touch,
    reduce_wakeup_dispatch,
)
from codex_autorunner.core.pma_domain.constants import (
    DEFAULT_WATCHDOG_IDLE_SECONDS,
    TIMER_STATE_FIRED,
    TIMER_STATE_PENDING,
    TIMER_TYPE_ONE_SHOT,
    TIMER_TYPE_WATCHDOG,
    WAKEUP_STATE_DISPATCHED,
    WAKEUP_STATE_PENDING,
)
from codex_autorunner.core.pma_domain.models import PmaTimer, PmaWakeup


def _iso_from_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_timer(
    *,
    timer_id: str = "timer-1",
    timer_type: str = TIMER_TYPE_ONE_SHOT,
    state: str = TIMER_STATE_PENDING,
    due_at: str | None = None,
    idle_seconds: int | None = None,
    fired_at: str | None = None,
    reason: str | None = None,
) -> PmaTimer:
    now = _iso_from_dt(datetime.now(timezone.utc))
    return PmaTimer(
        timer_id=timer_id,
        due_at=due_at or now,
        created_at=now,
        updated_at=now,
        state=state,
        fired_at=fired_at,
        timer_type=timer_type,
        idle_seconds=idle_seconds,
        reason=reason,
    )


def _make_wakeup(
    *,
    state: str = WAKEUP_STATE_PENDING,
    dispatched_at: str | None = None,
) -> PmaWakeup:
    now = _iso_from_dt(datetime.now(timezone.utc))
    return PmaWakeup(
        wakeup_id="wakeup-1",
        created_at=now,
        updated_at=now,
        state=state,
        dispatched_at=dispatched_at,
    )


class TestReduceDequeueDueTimers:
    def test_empty_timers(self):
        result = reduce_dequeue_due_timers([], datetime.now(timezone.utc))
        assert result.due == ()
        assert result.updated_timers == ()
        assert result.fired_count == 0

    def test_one_shot_due(self):
        past = _iso_from_dt(datetime.now(timezone.utc) - timedelta(hours=1))
        timer = _make_timer(due_at=past)
        now = datetime.now(timezone.utc)

        result = reduce_dequeue_due_timers([timer], now)

        assert result.fired_count == 1
        assert result.due[0].fired_timer.state == TIMER_STATE_FIRED
        assert result.due[0].fired_timer.fired_at is not None
        assert result.due[0].reset_timer is None
        assert result.updated_timers[0].state == TIMER_STATE_FIRED

    def test_one_shot_not_yet_due(self):
        future = _iso_from_dt(datetime.now(timezone.utc) + timedelta(hours=1))
        timer = _make_timer(due_at=future)
        now = datetime.now(timezone.utc)

        result = reduce_dequeue_due_timers([timer], now)

        assert result.fired_count == 0
        assert len(result.due) == 0
        assert result.updated_timers[0].state == TIMER_STATE_PENDING

    def test_already_fired_timer_skipped(self):
        past = _iso_from_dt(datetime.now(timezone.utc) - timedelta(hours=1))
        timer = _make_timer(due_at=past, state=TIMER_STATE_FIRED)
        now = datetime.now(timezone.utc)

        result = reduce_dequeue_due_timers([timer], now)

        assert result.fired_count == 0
        assert result.updated_timers[0].state == TIMER_STATE_FIRED

    def test_watchdog_due_creates_fired_and_reset(self):
        past = _iso_from_dt(datetime.now(timezone.utc) - timedelta(hours=1))
        timer = _make_timer(
            timer_type=TIMER_TYPE_WATCHDOG,
            due_at=past,
            idle_seconds=60,
        )
        now = datetime.now(timezone.utc)

        result = reduce_dequeue_due_timers([timer], now)

        assert result.fired_count == 1
        fired = result.due[0].fired_timer
        reset = result.due[0].reset_timer
        assert fired is not None
        assert reset is not None
        assert fired.state == TIMER_STATE_FIRED
        assert fired.fired_at is not None
        assert fired.timer_type == TIMER_TYPE_WATCHDOG
        assert reset.state == TIMER_STATE_PENDING
        assert reset.idle_seconds == 60
        assert reset.timer_id == "timer-1"
        reset_due = datetime.fromisoformat(reset.due_at.replace("Z", "+00:00"))
        assert reset_due > now

    def test_watchdog_with_zero_idle_seconds_uses_default(self):
        past = _iso_from_dt(datetime.now(timezone.utc) - timedelta(hours=1))
        timer = _make_timer(
            timer_type=TIMER_TYPE_WATCHDOG,
            due_at=past,
            idle_seconds=0,
        )
        now = datetime.now(timezone.utc)

        result = reduce_dequeue_due_timers([timer], now)

        assert result.fired_count == 1
        reset = result.due[0].reset_timer
        assert reset is not None
        assert reset.idle_seconds == DEFAULT_WATCHDOG_IDLE_SECONDS

    def test_watchdog_with_none_idle_seconds_uses_default(self):
        past = _iso_from_dt(datetime.now(timezone.utc) - timedelta(hours=1))
        timer = _make_timer(
            timer_type=TIMER_TYPE_WATCHDOG,
            due_at=past,
            idle_seconds=None,
        )
        now = datetime.now(timezone.utc)

        result = reduce_dequeue_due_timers([timer], now)

        assert result.fired_count == 1
        reset = result.due[0].reset_timer
        assert reset is not None
        assert reset.idle_seconds == DEFAULT_WATCHDOG_IDLE_SECONDS

    def test_limit_respected(self):
        past = _iso_from_dt(datetime.now(timezone.utc) - timedelta(hours=1))
        timers = [_make_timer(due_at=past, timer_id=f"timer-{i}") for i in range(5)]
        now = datetime.now(timezone.utc)

        result = reduce_dequeue_due_timers(timers, now, limit=2)

        assert result.fired_count == 2
        assert len(result.due) == 2
        assert len(result.updated_timers) == 5

    def test_invalid_due_at_skipped(self):
        timer = _make_timer(due_at="not-a-timestamp")
        now = datetime.now(timezone.utc)

        result = reduce_dequeue_due_timers([timer], now)

        assert result.fired_count == 0

    def test_mixed_pending_and_fired(self):
        past = _iso_from_dt(datetime.now(timezone.utc) - timedelta(hours=1))
        pending = _make_timer(due_at=past)
        fired = _make_timer(
            due_at=past,
            state=TIMER_STATE_FIRED,
            fired_at=past,
        )
        now = datetime.now(timezone.utc)

        result = reduce_dequeue_due_timers([pending, fired], now)

        assert result.fired_count == 1
        assert len(result.updated_timers) == 2
        assert result.updated_timers[0].state == TIMER_STATE_FIRED
        assert result.updated_timers[1].state == TIMER_STATE_FIRED

    def test_order_preserved_in_updated_timers(self):
        past = _iso_from_dt(datetime.now(timezone.utc) - timedelta(hours=1))
        timers = [
            _make_timer(due_at=past, timer_id="timer-a"),
            _make_timer(due_at=past, timer_id="timer-b"),
        ]
        now = datetime.now(timezone.utc)

        result = reduce_dequeue_due_timers(timers, now)

        assert result.updated_timers[0].timer_id == "timer-a"
        assert result.updated_timers[1].timer_id == "timer-b"


class TestReduceWakeupDispatch:
    def test_dispatch_pending_wakeup(self):
        wakeup = _make_wakeup()
        stamp = _iso_from_dt(datetime.now(timezone.utc))

        result = reduce_wakeup_dispatch(wakeup, stamp)

        assert result.dispatched is True
        assert result.wakeup_id == "wakeup-1"
        assert result.updated_wakeup is not None
        assert result.updated_wakeup.state == WAKEUP_STATE_DISPATCHED
        assert result.updated_wakeup.dispatched_at == stamp
        assert result.updated_wakeup.updated_at == stamp

    def test_dispatch_already_dispatched_is_noop(self):
        stamp = _iso_from_dt(datetime.now(timezone.utc))
        wakeup = _make_wakeup(state=WAKEUP_STATE_DISPATCHED, dispatched_at=stamp)

        result = reduce_wakeup_dispatch(wakeup, stamp)

        assert result.dispatched is False
        assert result.updated_wakeup is None

    def test_dispatch_preserves_other_fields(self):
        now_str = _iso_from_dt(datetime.now(timezone.utc))
        wakeup = PmaWakeup(
            wakeup_id="wakeup-1",
            created_at=now_str,
            updated_at=now_str,
            state=WAKEUP_STATE_PENDING,
            source="transition",
            repo_id="repo-1",
            run_id="run-1",
            thread_id="thread-1",
            lane_id="discord:123",
            reason="test",
            subscription_id="sub-1",
        )

        result = reduce_wakeup_dispatch(wakeup, now_str)

        assert result.updated_wakeup is not None
        assert result.updated_wakeup.source == "transition"
        assert result.updated_wakeup.repo_id == "repo-1"
        assert result.updated_wakeup.thread_id == "thread-1"
        assert result.updated_wakeup.lane_id == "discord:123"
        assert result.updated_wakeup.subscription_id == "sub-1"


class TestReduceTimerTouch:
    def test_touch_with_explicit_due_at(self):
        timer = _make_timer()
        now = datetime.now(timezone.utc)
        new_due = _iso_from_dt(now + timedelta(hours=1))

        result = reduce_timer_touch(timer, due_at=new_due, now=now)

        assert result.touched is True
        assert result.updated_timer is not None
        assert result.updated_timer.due_at == new_due
        assert result.updated_timer.state == TIMER_STATE_PENDING
        assert result.updated_timer.fired_at is None

    def test_touch_with_delay_seconds(self):
        timer = _make_timer()
        now = datetime.now(timezone.utc)

        result = reduce_timer_touch(timer, delay_seconds=120, now=now)

        assert result.touched is True
        assert result.updated_timer is not None
        expected_due = now + timedelta(seconds=120)
        actual_due = datetime.fromisoformat(
            result.updated_timer.due_at.replace("Z", "+00:00")
        )
        assert abs((actual_due - expected_due).total_seconds()) < 2

    def test_touch_watchdog_uses_idle_seconds(self):
        timer = _make_timer(timer_type=TIMER_TYPE_WATCHDOG, idle_seconds=60)
        now = datetime.now(timezone.utc)

        result = reduce_timer_touch(timer, now=now)

        assert result.touched is True
        assert result.updated_timer is not None
        expected_due = now + timedelta(seconds=60)
        actual_due = datetime.fromisoformat(
            result.updated_timer.due_at.replace("Z", "+00:00")
        )
        assert abs((actual_due - expected_due).total_seconds()) < 2

    def test_touch_watchdog_with_none_idle_uses_default(self):
        timer = _make_timer(timer_type=TIMER_TYPE_WATCHDOG, idle_seconds=None)
        now = datetime.now(timezone.utc)

        result = reduce_timer_touch(timer, now=now)

        assert result.touched is True
        assert result.updated_timer is not None
        expected_due = now + timedelta(seconds=DEFAULT_WATCHDOG_IDLE_SECONDS)
        actual_due = datetime.fromisoformat(
            result.updated_timer.due_at.replace("Z", "+00:00")
        )
        assert abs((actual_due - expected_due).total_seconds()) < 2

    def test_touch_one_shot_no_params_due_now(self):
        timer = _make_timer()
        now = datetime.now(timezone.utc)

        result = reduce_timer_touch(timer, now=now)

        assert result.touched is True
        assert result.updated_timer is not None
        actual_due = datetime.fromisoformat(
            result.updated_timer.due_at.replace("Z", "+00:00")
        )
        assert abs((actual_due - now).total_seconds()) < 2

    def test_touch_updates_reason(self):
        timer = _make_timer(reason="old-reason")
        now = datetime.now(timezone.utc)

        result = reduce_timer_touch(timer, reason="heartbeat", now=now)

        assert result.updated_timer.reason == "heartbeat"

    def test_touch_preserves_reason_when_none(self):
        timer = _make_timer(reason="original")
        now = datetime.now(timezone.utc)

        result = reduce_timer_touch(timer, now=now)

        assert result.updated_timer.reason == "original"

    def test_touch_resets_fired_at(self):
        past = _iso_from_dt(datetime.now(timezone.utc) - timedelta(hours=1))
        timer = _make_timer(
            state=TIMER_STATE_FIRED,
            fired_at=past,
        )
        now = datetime.now(timezone.utc)

        result = reduce_timer_touch(timer, now=now)

        assert result.updated_timer.state == TIMER_STATE_PENDING
        assert result.updated_timer.fired_at is None

    def test_explicit_due_at_takes_precedence_over_delay_seconds(self):
        timer = _make_timer()
        now = datetime.now(timezone.utc)
        explicit_due = _iso_from_dt(now + timedelta(hours=2))

        result = reduce_timer_touch(
            timer, due_at=explicit_due, delay_seconds=120, now=now
        )

        assert result.updated_timer.due_at == explicit_due

    def test_touch_preserves_timer_id_and_metadata(self):
        timer = PmaTimer(
            timer_id="timer-abc",
            due_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            state=TIMER_STATE_FIRED,
            timer_type=TIMER_TYPE_ONE_SHOT,
            metadata={"key": "value"},
        )
        now = datetime.now(timezone.utc)

        result = reduce_timer_touch(timer, now=now)

        assert result.updated_timer.timer_id == "timer-abc"
        assert result.updated_timer.metadata == {"key": "value"}
