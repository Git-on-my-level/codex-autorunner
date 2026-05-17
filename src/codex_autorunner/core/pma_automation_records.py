from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .pma_automation_types import (
    DEFAULT_PMA_LANE_ID,
    TIMER_TYPE_ONE_SHOT,
    _iso_now,
    _normalize_bool,
    _normalize_lane_id,
    _normalize_non_negative_int,
    _normalize_positive_int,
    _normalize_text,
    _normalize_text_list,
    _normalize_timer_type,
)


def _resolve_subscription_max_matches(
    *,
    max_matches: Any,
    notify_once: Any = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    resolved_max = _normalize_positive_int(max_matches, fallback=None)
    if resolved_max is not None:
        return resolved_max
    if _normalize_bool(notify_once, fallback=False):
        return 1
    if isinstance(metadata, dict) and _normalize_bool(
        metadata.get("notify_once"), fallback=False
    ):
        return 1
    return None


def _canonicalize_subscription_entry(data: dict[str, Any]) -> dict[str, Any]:
    canonical = dict(data)
    canonical.pop("notify_once", None)
    metadata_raw = data.get("metadata")
    metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
    resolved_max = _resolve_subscription_max_matches(
        max_matches=data.get("max_matches"),
        notify_once=data.get("notify_once"),
        metadata=metadata,
    )
    metadata.pop("notify_once", None)
    return {
        **canonical,
        "metadata": metadata,
        "max_matches": resolved_max,
    }


@dataclass
class PmaLifecycleSubscription:
    subscription_id: str
    created_at: str
    updated_at: str
    state: str = "active"
    event_types: list[str] = field(default_factory=list)
    repo_id: Optional[str] = None
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    lane_id: str = DEFAULT_PMA_LANE_ID
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    max_matches: Optional[int] = None
    match_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        event_types: Optional[list[str]] = None,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        max_matches: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "PmaLifecycleSubscription":
        stamp = _iso_now()
        return cls(
            subscription_id=str(uuid.uuid4()),
            created_at=stamp,
            updated_at=stamp,
            state="active",
            event_types=_normalize_text_list(event_types or []),
            repo_id=_normalize_text(repo_id),
            run_id=_normalize_text(run_id),
            thread_id=_normalize_text(thread_id),
            lane_id=_normalize_lane_id(lane_id),
            from_state=_normalize_text(from_state),
            to_state=_normalize_text(to_state),
            reason=_normalize_text(reason),
            idempotency_key=_normalize_text(idempotency_key),
            max_matches=_normalize_positive_int(max_matches, fallback=None),
            match_count=0,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PmaLifecycleSubscription":
        canonical = _canonicalize_subscription_entry(data)
        subscription_id = _normalize_text(data.get("subscription_id")) or str(
            uuid.uuid4()
        )
        created_at = _normalize_text(data.get("created_at")) or _iso_now()
        updated_at = _normalize_text(data.get("updated_at")) or created_at
        state = _normalize_text(data.get("state")) or "active"
        max_matches = _normalize_positive_int(
            canonical.get("max_matches"), fallback=None
        )
        match_count = _normalize_non_negative_int(
            canonical.get("match_count"), fallback=0
        )
        if match_count is None:
            match_count = 0
        metadata_raw = canonical.get("metadata")
        metadata: dict[str, Any] = (
            dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        )
        return cls(
            subscription_id=subscription_id,
            created_at=created_at,
            updated_at=updated_at,
            state=state.lower(),
            event_types=_normalize_text_list(canonical.get("event_types") or []),
            repo_id=_normalize_text(canonical.get("repo_id")),
            run_id=_normalize_text(canonical.get("run_id")),
            thread_id=_normalize_text(canonical.get("thread_id")),
            lane_id=_normalize_lane_id(canonical.get("lane_id")),
            from_state=_normalize_text(canonical.get("from_state")),
            to_state=_normalize_text(canonical.get("to_state")),
            reason=_normalize_text(canonical.get("reason")),
            idempotency_key=_normalize_text(canonical.get("idempotency_key")),
            max_matches=max_matches,
            match_count=int(match_count),
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PmaAutomationTimer:
    timer_id: str
    due_at: str
    created_at: str
    updated_at: str
    state: str = "pending"
    fired_at: Optional[str] = None
    timer_type: str = TIMER_TYPE_ONE_SHOT
    idle_seconds: Optional[int] = None
    subscription_id: Optional[str] = None
    repo_id: Optional[str] = None
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    lane_id: str = DEFAULT_PMA_LANE_ID
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        due_at: str,
        timer_type: Optional[str] = None,
        idle_seconds: Optional[int] = None,
        subscription_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "PmaAutomationTimer":
        stamp = _iso_now()
        return cls(
            timer_id=str(uuid.uuid4()),
            due_at=due_at,
            created_at=stamp,
            updated_at=stamp,
            state="pending",
            fired_at=None,
            timer_type=_normalize_timer_type(timer_type),
            idle_seconds=_normalize_non_negative_int(idle_seconds),
            subscription_id=_normalize_text(subscription_id),
            repo_id=_normalize_text(repo_id),
            run_id=_normalize_text(run_id),
            thread_id=_normalize_text(thread_id),
            lane_id=_normalize_lane_id(lane_id),
            from_state=_normalize_text(from_state),
            to_state=_normalize_text(to_state),
            reason=_normalize_text(reason),
            idempotency_key=_normalize_text(idempotency_key),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PmaAutomationTimer":
        timer_id = _normalize_text(data.get("timer_id")) or str(uuid.uuid4())
        created_at = _normalize_text(data.get("created_at")) or _iso_now()
        updated_at = _normalize_text(data.get("updated_at")) or created_at
        due_at = _normalize_text(data.get("due_at")) or created_at
        state = _normalize_text(data.get("state")) or "pending"
        metadata_raw = data.get("metadata")
        metadata: dict[str, Any] = (
            dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        )
        return cls(
            timer_id=timer_id,
            due_at=due_at,
            created_at=created_at,
            updated_at=updated_at,
            state=state.lower(),
            fired_at=_normalize_text(data.get("fired_at")),
            timer_type=_normalize_timer_type(data.get("timer_type")),
            idle_seconds=_normalize_non_negative_int(data.get("idle_seconds")),
            subscription_id=_normalize_text(data.get("subscription_id")),
            repo_id=_normalize_text(data.get("repo_id")),
            run_id=_normalize_text(data.get("run_id")),
            thread_id=_normalize_text(data.get("thread_id")),
            lane_id=_normalize_lane_id(data.get("lane_id")),
            from_state=_normalize_text(data.get("from_state")),
            to_state=_normalize_text(data.get("to_state")),
            reason=_normalize_text(data.get("reason")),
            idempotency_key=_normalize_text(data.get("idempotency_key")),
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PmaAutomationWakeup:
    wakeup_id: str
    created_at: str
    updated_at: str
    state: str = "pending"
    dispatched_at: Optional[str] = None
    source: str = "automation"
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
    timer_id: Optional[str] = None
    event_id: Optional[str] = None
    event_type: Optional[str] = None
    event_data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        source: str,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        reason: Optional[str] = None,
        timestamp: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        subscription_id: Optional[str] = None,
        timer_id: Optional[str] = None,
        event_id: Optional[str] = None,
        event_type: Optional[str] = None,
        event_data: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "PmaAutomationWakeup":
        stamp = _iso_now()
        return cls(
            wakeup_id=str(uuid.uuid4()),
            created_at=stamp,
            updated_at=stamp,
            state="pending",
            dispatched_at=None,
            source=_normalize_text(source) or "automation",
            repo_id=_normalize_text(repo_id),
            run_id=_normalize_text(run_id),
            thread_id=_normalize_text(thread_id),
            lane_id=_normalize_lane_id(lane_id),
            from_state=_normalize_text(from_state),
            to_state=_normalize_text(to_state),
            reason=_normalize_text(reason),
            timestamp=_normalize_text(timestamp) or stamp,
            idempotency_key=_normalize_text(idempotency_key),
            subscription_id=_normalize_text(subscription_id),
            timer_id=_normalize_text(timer_id),
            event_id=_normalize_text(event_id),
            event_type=_normalize_text(event_type),
            event_data=dict(event_data or {}),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PmaAutomationWakeup":
        wakeup_id = _normalize_text(data.get("wakeup_id")) or str(uuid.uuid4())
        created_at = _normalize_text(data.get("created_at")) or _iso_now()
        updated_at = _normalize_text(data.get("updated_at")) or created_at
        state = _normalize_text(data.get("state")) or "pending"
        source = _normalize_text(data.get("source")) or "automation"
        event_data_raw = data.get("event_data")
        event_data: dict[str, Any] = (
            dict(event_data_raw) if isinstance(event_data_raw, dict) else {}
        )
        metadata_raw = data.get("metadata")
        metadata: dict[str, Any] = (
            dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        )
        return cls(
            wakeup_id=wakeup_id,
            created_at=created_at,
            updated_at=updated_at,
            state=state.lower(),
            dispatched_at=_normalize_text(data.get("dispatched_at")),
            source=source,
            repo_id=_normalize_text(data.get("repo_id")),
            run_id=_normalize_text(data.get("run_id")),
            thread_id=_normalize_text(data.get("thread_id")),
            lane_id=_normalize_lane_id(data.get("lane_id")),
            from_state=_normalize_text(data.get("from_state")),
            to_state=_normalize_text(data.get("to_state")),
            reason=_normalize_text(data.get("reason")),
            timestamp=_normalize_text(data.get("timestamp")),
            idempotency_key=_normalize_text(data.get("idempotency_key")),
            subscription_id=_normalize_text(data.get("subscription_id")),
            timer_id=_normalize_text(data.get("timer_id")),
            event_id=_normalize_text(data.get("event_id")),
            event_type=_normalize_text(data.get("event_type")),
            event_data=event_data,
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "PmaAutomationTimer",
    "PmaAutomationWakeup",
    "PmaLifecycleSubscription",
    "_resolve_subscription_max_matches",
]
