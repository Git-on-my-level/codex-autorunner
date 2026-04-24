from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence

from .constants import (
    DEFAULT_PMA_LANE_ID,
    SUBSCRIPTION_STATE_ACTIVE,
    SUBSCRIPTION_STATE_CANCELLED,
    TIMER_TYPE_ONE_SHOT,
)
from .models import PmaSubscription


@dataclass(frozen=True)
class TransitionEvent:
    repo_id: Optional[str] = None
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    reason: str = "transition"
    event_type: str = "transition"
    transition_id: Optional[str] = None
    extra_metadata: dict[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class WakeupIntent:
    source: str = "transition"
    repo_id: Optional[str] = None
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    lane_id: str = DEFAULT_PMA_LANE_ID
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    reason: Optional[str] = None
    timestamp: Optional[str] = None
    idempotency_key: Optional[str] = None
    subscription_id: Optional[str] = None
    event_type: Optional[str] = None
    event_id: Optional[str] = None
    event_data: dict[str, Any] = field(default_factory=dict, hash=False)
    metadata: dict[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class ReduceTransitionResult:
    subscriptions: tuple[PmaSubscription, ...] = ()
    wakeup_intents: tuple[WakeupIntent, ...] = ()
    matched: int = 0
    created: int = 0


def _subscription_matches_event(sub: PmaSubscription, event: TransitionEvent) -> bool:
    if sub.event_types and event.event_type not in sub.event_types:
        return False
    if sub.repo_id is not None and sub.repo_id != event.repo_id:
        return False
    if sub.run_id is not None and sub.run_id != event.run_id:
        return False
    if sub.thread_id is not None and sub.thread_id != event.thread_id:
        return False
    if sub.from_state is not None and sub.from_state != event.from_state:
        return False
    if sub.to_state is not None and sub.to_state != event.to_state:
        return False
    return True


def _build_wakeup_key(event: TransitionEvent, subscription_id: Optional[str]) -> str:
    key = event.transition_id or (
        f"{event.event_type}:{event.repo_id or ''}:"
        f"{event.run_id or ''}:{event.thread_id or ''}:"
        f"{event.from_state or ''}:{event.to_state or ''}"
    )
    return f"transition:{key}:{subscription_id or 'all'}"


def reduce_transition(
    subscriptions: Sequence[PmaSubscription],
    existing_wakeup_keys: frozenset[str],
    event: TransitionEvent,
) -> ReduceTransitionResult:
    updated_subs: list[PmaSubscription] = []
    new_intents: list[WakeupIntent] = []
    matched = 0
    created = 0

    for sub in subscriptions:
        if sub.state != SUBSCRIPTION_STATE_ACTIVE:
            updated_subs.append(sub)
            continue

        if sub.is_exhausted():
            updated_subs.append(replace(sub, state=SUBSCRIPTION_STATE_CANCELLED))
            continue

        if not _subscription_matches_event(sub, event):
            updated_subs.append(sub)
            continue

        matched += 1
        wakeup_key = _build_wakeup_key(event, sub.subscription_id)

        if wakeup_key in existing_wakeup_keys:
            updated_subs.append(sub)
            continue

        wakeup_metadata = dict(sub.metadata)
        wakeup_metadata.update(event.extra_metadata)

        event_id = event.extra_metadata.get("event_id")
        event_data = event.extra_metadata.get("event_data")
        if not isinstance(event_data, dict):
            event_data = {}

        new_intents.append(
            WakeupIntent(
                source="transition",
                repo_id=event.repo_id,
                run_id=event.run_id,
                thread_id=event.thread_id,
                lane_id=sub.lane_id,
                from_state=event.from_state,
                to_state=event.to_state,
                reason=event.reason,
                timestamp="",
                idempotency_key=wakeup_key,
                subscription_id=sub.subscription_id,
                event_type=event.event_type,
                event_id=event_id if isinstance(event_id, str) else None,
                event_data=event_data,
                metadata=wakeup_metadata,
            )
        )
        created += 1

        new_match_count = sub.match_count + 1
        new_state = (
            SUBSCRIPTION_STATE_CANCELLED
            if sub.max_matches is not None and new_match_count >= sub.max_matches
            else sub.state
        )
        updated_subs.append(replace(sub, match_count=new_match_count, state=new_state))

    return ReduceTransitionResult(
        subscriptions=tuple(updated_subs),
        wakeup_intents=tuple(new_intents),
        matched=matched,
        created=created,
    )


@dataclass(frozen=True)
class TimerFiredEvent:
    timer_id: str
    timer_type: str = TIMER_TYPE_ONE_SHOT
    fired_at: Optional[str] = None
    repo_id: Optional[str] = None
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    lane_id: str = DEFAULT_PMA_LANE_ID
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    reason: Optional[str] = None
    subscription_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class ReduceTimerResult:
    wakeup_intent: WakeupIntent
    timer_id: str
    source: str = "timer"


def reduce_timer_fired(event: TimerFiredEvent) -> ReduceTimerResult:
    fired_ts = event.fired_at or ""
    timer_key = f"timer:{event.timer_id}:{fired_ts}"

    wakeup_metadata = dict(event.metadata)
    wakeup_metadata["timer_type"] = event.timer_type
    wakeup_metadata["domain_source"] = "timer_reducer"

    return ReduceTimerResult(
        wakeup_intent=WakeupIntent(
            source="timer",
            repo_id=event.repo_id,
            run_id=event.run_id,
            thread_id=event.thread_id,
            lane_id=event.lane_id,
            from_state=event.from_state,
            to_state=event.to_state,
            reason=event.reason or "timer_due",
            timestamp=fired_ts,
            idempotency_key=timer_key,
            subscription_id=event.subscription_id,
            event_type="timer_fired",
            metadata=wakeup_metadata,
        ),
        timer_id=event.timer_id,
    )
