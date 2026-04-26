from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from .constants import (
    DEFAULT_WATCHDOG_IDLE_SECONDS,
    TIMER_STATE_FIRED,
    TIMER_STATE_PENDING,
    TIMER_TYPE_WATCHDOG,
    WAKEUP_STATE_DISPATCHED,
)
from .models import PmaTimer, PmaWakeup


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso_from_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_after_seconds(seconds: int, base: datetime) -> str:
    return _iso_from_dt(base + timedelta(seconds=max(0, seconds)))


@dataclass(frozen=True)
class TimerDequeueOutput:
    fired_timer: PmaTimer
    reset_timer: Optional[PmaTimer] = None


@dataclass(frozen=True)
class ReduceDequeueResult:
    due: tuple[TimerDequeueOutput, ...] = ()
    updated_timers: tuple[PmaTimer, ...] = ()
    fired_count: int = 0


def reduce_dequeue_due_timers(
    timers: Sequence[PmaTimer],
    now: datetime,
    *,
    limit: int = 100,
) -> ReduceDequeueResult:
    due: list[TimerDequeueOutput] = []
    updated: list[PmaTimer] = []
    now_stamp = _iso_from_dt(now)

    for timer in timers:
        if timer.state != TIMER_STATE_PENDING:
            updated.append(timer)
            continue

        due_at_dt = _parse_iso(timer.due_at)
        if due_at_dt is None or due_at_dt > now:
            updated.append(timer)
            continue

        if len(due) >= limit:
            updated.append(timer)
            continue

        if timer.timer_type == TIMER_TYPE_WATCHDOG:
            fired = replace(
                timer,
                state=TIMER_STATE_FIRED,
                fired_at=now_stamp,
                updated_at=now_stamp,
            )
            idle = (
                timer.idle_seconds
                if timer.idle_seconds and timer.idle_seconds > 0
                else DEFAULT_WATCHDOG_IDLE_SECONDS
            )
            reset = replace(
                timer,
                due_at=_iso_after_seconds(idle, now),
                state=TIMER_STATE_PENDING,
                fired_at=now_stamp,
                updated_at=now_stamp,
                idle_seconds=idle,
            )
            due.append(TimerDequeueOutput(fired_timer=fired, reset_timer=reset))
            updated.append(reset)
        else:
            fired = replace(
                timer,
                state=TIMER_STATE_FIRED,
                fired_at=now_stamp,
                updated_at=now_stamp,
            )
            due.append(TimerDequeueOutput(fired_timer=fired))
            updated.append(fired)

    return ReduceDequeueResult(
        due=tuple(due),
        updated_timers=tuple(updated),
        fired_count=len(due),
    )


@dataclass(frozen=True)
class WakeupDispatchResult:
    wakeup_id: str
    dispatched: bool
    updated_wakeup: Optional[PmaWakeup] = None


def reduce_wakeup_dispatch(
    wakeup: PmaWakeup,
    dispatched_at: str,
) -> WakeupDispatchResult:
    if wakeup.state == WAKEUP_STATE_DISPATCHED:
        return WakeupDispatchResult(wakeup_id=wakeup.wakeup_id, dispatched=False)
    updated = replace(
        wakeup,
        state=WAKEUP_STATE_DISPATCHED,
        dispatched_at=dispatched_at,
        updated_at=dispatched_at,
    )
    return WakeupDispatchResult(
        wakeup_id=wakeup.wakeup_id,
        dispatched=True,
        updated_wakeup=updated,
    )


@dataclass(frozen=True)
class TimerTouchResult:
    touched: bool
    updated_timer: Optional[PmaTimer] = None


def reduce_timer_touch(
    timer: PmaTimer,
    *,
    due_at: Optional[str] = None,
    delay_seconds: Optional[int] = None,
    reason: Optional[str] = None,
    now: datetime,
) -> TimerTouchResult:
    now_stamp = _iso_from_dt(now)

    if due_at is not None:
        resolved_due_at = due_at
    elif delay_seconds is not None:
        resolved_due_at = _iso_after_seconds(delay_seconds, now)
    elif timer.timer_type == TIMER_TYPE_WATCHDOG:
        resolved_due_at = _iso_after_seconds(
            timer.idle_seconds or DEFAULT_WATCHDOG_IDLE_SECONDS, now
        )
    else:
        resolved_due_at = _iso_after_seconds(delay_seconds or 0, now)

    updated = replace(
        timer,
        due_at=resolved_due_at,
        state=TIMER_STATE_PENDING,
        fired_at=None,
        reason=reason if reason is not None else timer.reason,
        updated_at=now_stamp,
    )
    return TimerTouchResult(touched=True, updated_timer=updated)
