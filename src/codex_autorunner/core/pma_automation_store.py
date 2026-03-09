from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
from .pma_automation_persistence import PmaAutomationPersistence
from .pma_automation_types import (
    DEFAULT_PMA_LANE_ID,
    DEFAULT_WATCHDOG_IDLE_SECONDS,
    PMA_AUTOMATION_STORE_FILENAME,
    PMA_AUTOMATION_VERSION,
    TIMER_TYPE_ONE_SHOT,
    TIMER_TYPE_WATCHDOG,
    _iso_after_seconds,
    _iso_now,
    _normalize_bool,
    _normalize_due_timestamp,
    _normalize_lane_id,
    _normalize_non_negative_int,
    _normalize_positive_int,
    _normalize_text,
    _normalize_text_list,
    _normalize_timer_type,
    _parse_iso,
    default_pma_automation_state,
)

logger = logging.getLogger(__name__)


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
        notify_once: Optional[bool] = None,
        max_matches: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "PmaLifecycleSubscription":
        stamp = _iso_now()
        resolved_once = _normalize_bool(notify_once, fallback=None)
        resolved_max = _normalize_positive_int(max_matches, fallback=None)
        if resolved_max is None and resolved_once:
            resolved_max = 1
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
            max_matches=resolved_max,
            match_count=0,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PmaLifecycleSubscription":
        subscription_id = _normalize_text(data.get("subscription_id")) or str(
            uuid.uuid4()
        )
        created_at = _normalize_text(data.get("created_at")) or _iso_now()
        updated_at = _normalize_text(data.get("updated_at")) or created_at
        state = _normalize_text(data.get("state")) or "active"
        max_matches = _normalize_positive_int(data.get("max_matches"), fallback=None)
        if max_matches is None and _normalize_bool(
            data.get("notify_once"), fallback=False
        ):
            max_matches = 1
        match_count = _normalize_non_negative_int(data.get("match_count"), fallback=0)
        if match_count is None:
            match_count = 0
        metadata_raw = data.get("metadata")
        metadata: dict[str, Any] = (
            dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        )
        if max_matches is None and _normalize_bool(
            metadata.get("notify_once"), fallback=False
        ):
            max_matches = 1
        return cls(
            subscription_id=subscription_id,
            created_at=created_at,
            updated_at=updated_at,
            state=state.lower(),
            event_types=_normalize_text_list(data.get("event_types") or []),
            repo_id=_normalize_text(data.get("repo_id")),
            run_id=_normalize_text(data.get("run_id")),
            thread_id=_normalize_text(data.get("thread_id")),
            lane_id=_normalize_lane_id(data.get("lane_id")),
            from_state=_normalize_text(data.get("from_state")),
            to_state=_normalize_text(data.get("to_state")),
            reason=_normalize_text(data.get("reason")),
            idempotency_key=_normalize_text(data.get("idempotency_key")),
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


class PmaAutomationStore:
    def __init__(self, hub_root: Path) -> None:
        self._persistence = PmaAutomationPersistence(hub_root)
        self._path = self._persistence.path

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return self._persistence._lock_path()

    def load(self) -> dict[str, Any]:
        return self._persistence.load()

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        return self._persistence._load_unlocked()

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        self._persistence._save_unlocked(state)

    def _load_structured_unlocked(
        self,
    ) -> tuple[
        dict[str, Any],
        list[PmaLifecycleSubscription],
        list[PmaAutomationTimer],
        list[PmaAutomationWakeup],
    ]:
        state = self._load_unlocked() or default_pma_automation_state()
        subscriptions = self._normalize_subscriptions(state.get("subscriptions"))
        timers = self._normalize_timers(state.get("timers"))
        wakeups = self._normalize_wakeups(state.get("wakeups"))
        return state, subscriptions, timers, wakeups

    def _save_structured_unlocked(
        self,
        state: dict[str, Any],
        subscriptions: list[PmaLifecycleSubscription],
        timers: list[PmaAutomationTimer],
        wakeups: list[PmaAutomationWakeup],
    ) -> None:
        state["updated_at"] = _iso_now()
        state["subscriptions"] = [entry.to_dict() for entry in subscriptions]
        state["timers"] = [entry.to_dict() for entry in timers]
        state["wakeups"] = [entry.to_dict() for entry in wakeups]
        self._save_unlocked(state)

    def _normalize_subscriptions(self, value: Any) -> list[PmaLifecycleSubscription]:
        out: list[PmaLifecycleSubscription] = []
        if not isinstance(value, list):
            return out
        for entry in value:
            if isinstance(entry, PmaLifecycleSubscription):
                out.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            try:
                out.append(PmaLifecycleSubscription.from_dict(entry))
            except Exception:
                continue
        return out

    def _normalize_timers(self, value: Any) -> list[PmaAutomationTimer]:
        out: list[PmaAutomationTimer] = []
        if not isinstance(value, list):
            return out
        for entry in value:
            if isinstance(entry, PmaAutomationTimer):
                out.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            try:
                out.append(PmaAutomationTimer.from_dict(entry))
            except Exception:
                continue
        return out

    def _normalize_wakeups(self, value: Any) -> list[PmaAutomationWakeup]:
        out: list[PmaAutomationWakeup] = []
        if not isinstance(value, list):
            return out
        for entry in value:
            if isinstance(entry, PmaAutomationWakeup):
                out.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            try:
                out.append(PmaAutomationWakeup.from_dict(entry))
            except Exception:
                continue
        return out

    @staticmethod
    def _coerce_payload(
        payload: Optional[dict[str, Any]], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        if isinstance(payload, dict):
            merged.update(payload)
        for key, value in kwargs.items():
            if value is not None:
                merged[key] = value
        return merged

    @staticmethod
    def _coerce_limit(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            parsed = int(value)
        except Exception:
            return None
        if parsed < 0:
            return None
        return parsed

    def upsert_subscription(
        self,
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
        notify_once: Optional[bool] = None,
        max_matches: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[PmaLifecycleSubscription, bool]:
        key = _normalize_text(idempotency_key)
        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
            if key is not None:
                for existing in subscriptions:
                    if existing.state != "active":
                        continue
                    if existing.idempotency_key == key:
                        return existing, True
            created = PmaLifecycleSubscription.create(
                event_types=event_types,
                repo_id=repo_id,
                run_id=run_id,
                thread_id=thread_id,
                lane_id=lane_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                idempotency_key=key,
                notify_once=notify_once,
                max_matches=max_matches,
                metadata=metadata,
            )
            subscriptions.append(created)
            self._save_structured_unlocked(state, subscriptions, timers, wakeups)
            return created, False

    def create_subscription(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        data = self._coerce_payload(payload, kwargs)
        created, deduped = self.upsert_subscription(
            event_types=_normalize_text_list(data.get("event_types")) or None,
            repo_id=_normalize_text(data.get("repo_id")),
            run_id=_normalize_text(data.get("run_id")),
            thread_id=_normalize_text(data.get("thread_id")),
            lane_id=_normalize_text(data.get("lane_id")),
            from_state=_normalize_text(data.get("from_state")),
            to_state=_normalize_text(data.get("to_state")),
            reason=_normalize_text(data.get("reason")),
            idempotency_key=_normalize_text(data.get("idempotency_key")),
            notify_once=_normalize_bool(data.get("notify_once"), fallback=None),
            max_matches=_normalize_positive_int(data.get("max_matches"), fallback=None),
            metadata=(
                data.get("metadata") if isinstance(data.get("metadata"), dict) else None
            ),
        )
        return {"subscription": created.to_dict(), "deduped": deduped}

    def add_subscription(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.create_subscription(payload=payload, **kwargs)

    def cancel_subscription(self, subscription_id: str) -> bool:
        target_id = _normalize_text(subscription_id)
        if target_id is None:
            return False
        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
            changed = False
            for entry in subscriptions:
                if entry.subscription_id != target_id:
                    continue
                if entry.state == "cancelled":
                    continue
                entry.state = "cancelled"
                entry.updated_at = _iso_now()
                changed = True
                break
            if changed:
                self._save_structured_unlocked(state, subscriptions, timers, wakeups)
            return changed

    def delete_subscription(self, subscription_id: str, **_: Any) -> bool:
        return self.cancel_subscription(subscription_id)

    def remove_subscription(self, subscription_id: str, **_: Any) -> bool:
        return self.cancel_subscription(subscription_id)

    def list_subscriptions(
        self,
        *,
        include_inactive: bool = False,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        limit: Optional[int] = None,
        **_: Any,
    ) -> list[dict[str, Any]]:
        state = self.load()
        subscriptions = self._normalize_subscriptions(state.get("subscriptions"))
        repo_id_norm = _normalize_text(repo_id)
        run_id_norm = _normalize_text(run_id)
        thread_id_norm = _normalize_text(thread_id)
        lane_id_norm = _normalize_text(lane_id)
        out: list[dict[str, Any]] = []
        for entry in subscriptions:
            if not include_inactive and entry.state != "active":
                continue
            if repo_id_norm is not None and entry.repo_id != repo_id_norm:
                continue
            if run_id_norm is not None and entry.run_id != run_id_norm:
                continue
            if thread_id_norm is not None and entry.thread_id != thread_id_norm:
                continue
            if lane_id_norm is not None and entry.lane_id != lane_id_norm:
                continue
            out.append(entry.to_dict())
        parsed_limit = self._coerce_limit(limit)
        if parsed_limit is not None:
            return out[:parsed_limit]
        return out

    def get_subscriptions(self, **kwargs: Any) -> dict[str, Any]:
        return {"subscriptions": self.list_subscriptions(**kwargs)}

    def match_lifecycle_subscriptions(
        self,
        *,
        event_type: str,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        event_type_norm = (_normalize_text(event_type) or "").lower()
        repo_id_norm = _normalize_text(repo_id)
        run_id_norm = _normalize_text(run_id)
        thread_id_norm = _normalize_text(thread_id)
        from_state_norm = _normalize_text(from_state)
        to_state_norm = _normalize_text(to_state)

        out: list[dict[str, Any]] = []
        state = self.load()
        subscriptions = self._normalize_subscriptions(state.get("subscriptions"))
        for entry in subscriptions:
            if entry.state != "active":
                continue
            if entry.event_types and event_type_norm not in entry.event_types:
                continue
            if entry.repo_id is not None and entry.repo_id != repo_id_norm:
                continue
            if entry.run_id is not None and entry.run_id != run_id_norm:
                continue
            if entry.thread_id is not None and entry.thread_id != thread_id_norm:
                continue
            if entry.from_state is not None and entry.from_state != from_state_norm:
                continue
            if entry.to_state is not None and entry.to_state != to_state_norm:
                continue
            out.append(entry.to_dict())
        return out

    def upsert_timer(
        self,
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
    ) -> tuple[PmaAutomationTimer, bool]:
        key = _normalize_text(idempotency_key)
        normalized_due_at = _normalize_due_timestamp(due_at, field_name="due_at")
        if normalized_due_at is None:
            raise ValueError("due_at is required")
        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
            if key is not None:
                for existing in timers:
                    if existing.state != "pending":
                        continue
                    if existing.idempotency_key == key:
                        return existing, True
            created = PmaAutomationTimer.create(
                due_at=normalized_due_at,
                timer_type=timer_type,
                idle_seconds=idle_seconds,
                subscription_id=subscription_id,
                repo_id=repo_id,
                run_id=run_id,
                thread_id=thread_id,
                lane_id=lane_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                idempotency_key=key,
                metadata=metadata,
            )
            timers.append(created)
            self._save_structured_unlocked(state, subscriptions, timers, wakeups)
            return created, False

    def create_timer(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        data = self._coerce_payload(payload, kwargs)
        timer_type = _normalize_timer_type(data.get("timer_type"))
        idle_seconds = _normalize_non_negative_int(
            data.get("idle_seconds"), fallback=None
        )
        delay_seconds = _normalize_non_negative_int(
            data.get("delay_seconds"), fallback=None
        )
        due_at = _normalize_due_timestamp(data.get("due_at"), field_name="due_at")
        if due_at is None:
            due_at = _normalize_due_timestamp(
                data.get("timestamp"), field_name="timestamp"
            )
        if due_at is None:
            if timer_type == TIMER_TYPE_WATCHDOG:
                idle_seconds = idle_seconds or DEFAULT_WATCHDOG_IDLE_SECONDS
                due_at = _iso_after_seconds(idle_seconds)
            else:
                due_at = _iso_after_seconds(delay_seconds or 0)
        created, deduped = self.upsert_timer(
            due_at=due_at,
            timer_type=timer_type,
            idle_seconds=idle_seconds,
            subscription_id=_normalize_text(data.get("subscription_id")),
            repo_id=_normalize_text(data.get("repo_id")),
            run_id=_normalize_text(data.get("run_id")),
            thread_id=_normalize_text(data.get("thread_id")),
            lane_id=_normalize_text(data.get("lane_id")),
            from_state=_normalize_text(data.get("from_state")),
            to_state=_normalize_text(data.get("to_state")),
            reason=_normalize_text(data.get("reason")),
            idempotency_key=_normalize_text(data.get("idempotency_key")),
            metadata=(
                data.get("metadata") if isinstance(data.get("metadata"), dict) else None
            ),
        )
        return {"timer": created.to_dict(), "deduped": deduped}

    def add_timer(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.create_timer(payload=payload, **kwargs)

    def cancel_timer(self, timer_id: str) -> bool:
        target_id = _normalize_text(timer_id)
        if target_id is None:
            return False
        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
            changed = False
            for entry in timers:
                if entry.timer_id != target_id:
                    continue
                if entry.state == "cancelled":
                    continue
                entry.state = "cancelled"
                entry.updated_at = _iso_now()
                changed = True
                break
            if changed:
                self._save_structured_unlocked(state, subscriptions, timers, wakeups)
            return changed

    def delete_timer(self, timer_id: str, **_: Any) -> bool:
        return self.cancel_timer(timer_id)

    def remove_timer(self, timer_id: str, **_: Any) -> bool:
        return self.cancel_timer(timer_id)

    def list_timers(
        self,
        *,
        include_inactive: bool = False,
        timer_type: Optional[str] = None,
        subscription_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        limit: Optional[int] = None,
        **_: Any,
    ) -> list[dict[str, Any]]:
        state = self.load()
        timers = self._normalize_timers(state.get("timers"))
        timer_type_norm = _normalize_text(timer_type)
        subscription_id_norm = _normalize_text(subscription_id)
        repo_id_norm = _normalize_text(repo_id)
        run_id_norm = _normalize_text(run_id)
        thread_id_norm = _normalize_text(thread_id)
        lane_id_norm = _normalize_text(lane_id)
        out: list[dict[str, Any]] = []
        for entry in timers:
            if not include_inactive and entry.state != "pending":
                continue
            if timer_type_norm is not None and entry.timer_type != timer_type_norm:
                continue
            if (
                subscription_id_norm is not None
                and entry.subscription_id != subscription_id_norm
            ):
                continue
            if repo_id_norm is not None and entry.repo_id != repo_id_norm:
                continue
            if run_id_norm is not None and entry.run_id != run_id_norm:
                continue
            if thread_id_norm is not None and entry.thread_id != thread_id_norm:
                continue
            if lane_id_norm is not None and entry.lane_id != lane_id_norm:
                continue
            out.append(entry.to_dict())
        parsed_limit = self._coerce_limit(limit)
        if parsed_limit is not None:
            return out[:parsed_limit]
        return out

    def get_timers(self, **kwargs: Any) -> dict[str, Any]:
        return {"timers": self.list_timers(**kwargs)}

    def touch_timer(
        self,
        timer_id: str,
        payload: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        target_id = _normalize_text(timer_id)
        if target_id is None:
            return {"status": "error", "timer_id": timer_id, "touched": False}
        data = self._coerce_payload(payload, kwargs)
        due_at = _normalize_due_timestamp(data.get("timestamp"), field_name="timestamp")
        if due_at is None:
            due_at = _normalize_due_timestamp(data.get("due_at"), field_name="due_at")
        delay_seconds = _normalize_non_negative_int(
            data.get("delay_seconds"), fallback=None
        )
        reason = _normalize_text(data.get("reason"))

        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
            for entry in timers:
                if entry.timer_id != target_id:
                    continue
                if due_at is None:
                    if delay_seconds is not None:
                        entry.due_at = _iso_after_seconds(delay_seconds)
                    elif entry.timer_type == TIMER_TYPE_WATCHDOG:
                        entry.due_at = _iso_after_seconds(
                            entry.idle_seconds or DEFAULT_WATCHDOG_IDLE_SECONDS
                        )
                    else:
                        entry.due_at = _iso_after_seconds(delay_seconds or 0)
                else:
                    entry.due_at = due_at
                entry.state = "pending"
                entry.fired_at = None
                if reason is not None:
                    entry.reason = reason
                entry.updated_at = _iso_now()
                self._save_structured_unlocked(state, subscriptions, timers, wakeups)
                return {"status": "ok", "timer": entry.to_dict(), "touched": True}
        return {"status": "ok", "timer_id": target_id, "touched": False}

    def refresh_timer(
        self,
        timer_id: str,
        payload: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.touch_timer(timer_id, payload=payload, **kwargs)

    def renew_timer(
        self,
        timer_id: str,
        payload: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.touch_timer(timer_id, payload=payload, **kwargs)

    def dequeue_due_timers(
        self,
        *,
        limit: int = 100,
        now_timestamp: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        due_limit = max(0, int(limit))
        if due_limit <= 0:
            return []

        now_dt = _parse_iso(now_timestamp) if now_timestamp else None
        if now_dt is None:
            now_dt = datetime.now(timezone.utc)

        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
            due: list[PmaAutomationTimer] = []
            now_stamp = _iso_now()
            for entry in timers:
                if entry.state != "pending":
                    continue
                due_at_dt = _parse_iso(entry.due_at)
                if due_at_dt is None:
                    continue
                if due_at_dt > now_dt:
                    continue
                if entry.timer_type == TIMER_TYPE_WATCHDOG:
                    entry.fired_at = now_stamp
                    entry.updated_at = now_stamp
                    due.append(
                        PmaAutomationTimer(
                            timer_id=entry.timer_id,
                            due_at=entry.due_at,
                            created_at=entry.created_at,
                            updated_at=entry.updated_at,
                            state="fired",
                            fired_at=now_stamp,
                            timer_type=entry.timer_type,
                            idle_seconds=entry.idle_seconds,
                            subscription_id=entry.subscription_id,
                            repo_id=entry.repo_id,
                            run_id=entry.run_id,
                            thread_id=entry.thread_id,
                            lane_id=entry.lane_id,
                            from_state=entry.from_state,
                            to_state=entry.to_state,
                            reason=entry.reason,
                            idempotency_key=entry.idempotency_key,
                            metadata=dict(entry.metadata or {}),
                        )
                    )
                    if entry.idle_seconds is None or entry.idle_seconds <= 0:
                        entry.idle_seconds = DEFAULT_WATCHDOG_IDLE_SECONDS
                    entry.due_at = _iso_after_seconds(entry.idle_seconds)
                    entry.state = "pending"
                else:
                    entry.state = "fired"
                    entry.fired_at = now_stamp
                    entry.updated_at = now_stamp
                    due.append(entry)
                if len(due) >= due_limit:
                    break
            if due:
                self._save_structured_unlocked(state, subscriptions, timers, wakeups)
            return [entry.to_dict() for entry in due]

    def enqueue_wakeup(
        self,
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
    ) -> tuple[PmaAutomationWakeup, bool]:
        key = _normalize_text(idempotency_key)
        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
            if key is not None:
                for existing in wakeups:
                    if existing.idempotency_key == key:
                        return existing, True
            created = PmaAutomationWakeup.create(
                source=source,
                repo_id=repo_id,
                run_id=run_id,
                thread_id=thread_id,
                lane_id=lane_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                timestamp=timestamp,
                idempotency_key=key,
                subscription_id=subscription_id,
                timer_id=timer_id,
                event_id=event_id,
                event_type=event_type,
                event_data=event_data,
                metadata=metadata,
            )
            wakeups.append(created)
            self._save_structured_unlocked(state, subscriptions, timers, wakeups)
            return created, False

    def enqueue_event(self, **kwargs: Any) -> tuple[PmaAutomationWakeup, bool]:
        return self.enqueue_wakeup(**kwargs)

    def list_wakeups(
        self,
        *,
        state_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        state = self.load()
        wakeups = self._normalize_wakeups(state.get("wakeups"))
        filter_norm = _normalize_text(state_filter)
        if filter_norm is not None:
            wakeups = [entry for entry in wakeups if entry.state == filter_norm]
        if isinstance(limit, int) and limit >= 0:
            wakeups = wakeups[:limit]
        return [entry.to_dict() for entry in wakeups]

    def list_pending_wakeups(self, *, limit: int = 100) -> list[dict[str, Any]]:
        take = max(0, int(limit))
        if take <= 0:
            return []
        state = self.load()
        wakeups = self._normalize_wakeups(state.get("wakeups"))
        pending = [entry.to_dict() for entry in wakeups if entry.state == "pending"]
        return pending[:take]

    def list_pending_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_pending_wakeups(limit=limit)

    def notify_transition(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        data = self._coerce_payload(payload, kwargs)
        repo_id = _normalize_text(data.get("repo_id"))
        run_id = _normalize_text(data.get("run_id"))
        thread_id = _normalize_text(data.get("thread_id"))
        from_state = _normalize_text(data.get("from_state"))
        to_state = _normalize_text(data.get("to_state"))
        reason = _normalize_text(data.get("reason")) or "transition"
        timestamp = _normalize_text(data.get("timestamp")) or _iso_now()
        event_type = (
            _normalize_text(data.get("event_type"))
            or _normalize_text(data.get("to_state"))
            or "transition"
        )
        transition_id = _normalize_text(data.get("transition_id")) or _normalize_text(
            data.get("idempotency_key")
        )
        event_type_norm = event_type.lower()
        metadata_payload = {
            key_name: value
            for key_name, value in data.items()
            if key_name
            not in {
                "repo_id",
                "run_id",
                "thread_id",
                "from_state",
                "to_state",
                "reason",
                "timestamp",
            }
        }
        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
            matched = 0
            created = 0
            changed = False

            for entry in subscriptions:
                if entry.state != "active":
                    continue
                if (
                    entry.max_matches is not None
                    and entry.match_count >= entry.max_matches
                ):
                    entry.state = "cancelled"
                    entry.updated_at = _iso_now()
                    changed = True
                    continue
                if entry.event_types and event_type_norm not in entry.event_types:
                    continue
                if entry.repo_id is not None and entry.repo_id != repo_id:
                    continue
                if entry.run_id is not None and entry.run_id != run_id:
                    continue
                if entry.thread_id is not None and entry.thread_id != thread_id:
                    continue
                if entry.from_state is not None and entry.from_state != from_state:
                    continue
                if entry.to_state is not None and entry.to_state != to_state:
                    continue

                matched += 1
                subscription_id = entry.subscription_id
                key = (
                    transition_id
                    or f"{event_type_norm}:{repo_id or ''}:{run_id or ''}:{thread_id or ''}:{from_state or ''}:{to_state or ''}:{timestamp}"
                )
                wakeup_key = f"transition:{key}:{subscription_id or 'all'}"
                deduped = any(
                    existing.idempotency_key == wakeup_key for existing in wakeups
                )
                if deduped:
                    continue

                wakeups.append(
                    PmaAutomationWakeup.create(
                        source="transition",
                        repo_id=repo_id,
                        run_id=run_id,
                        thread_id=thread_id,
                        lane_id=entry.lane_id,
                        from_state=from_state,
                        to_state=to_state,
                        reason=reason,
                        timestamp=timestamp,
                        idempotency_key=wakeup_key,
                        subscription_id=subscription_id,
                        event_type=event_type_norm,
                        metadata=dict(metadata_payload),
                    )
                )
                created += 1
                if entry.max_matches is not None:
                    entry.match_count = max(0, int(entry.match_count)) + 1
                    entry.updated_at = _iso_now()
                    changed = True
                    if entry.match_count >= entry.max_matches:
                        entry.state = "cancelled"

            if created > 0 or changed:
                self._save_structured_unlocked(state, subscriptions, timers, wakeups)
        return {
            "status": "ok",
            "matched": matched,
            "created": created,
            "repo_id": repo_id,
            "run_id": run_id,
            "thread_id": thread_id,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
            "timestamp": timestamp,
        }

    def record_transition(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.notify_transition(payload=payload, **kwargs)

    def handle_transition(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.notify_transition(payload=payload, **kwargs)

    def on_transition(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.notify_transition(payload=payload, **kwargs)

    def process_transition(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.notify_transition(payload=payload, **kwargs)

    def mark_wakeup_dispatched(
        self, wakeup_id: str, *, dispatched_at: Optional[str] = None
    ) -> bool:
        target_id = _normalize_text(wakeup_id)
        if target_id is None:
            return False
        stamp = _normalize_text(dispatched_at) or _iso_now()
        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
            changed = False
            for entry in wakeups:
                if entry.wakeup_id != target_id:
                    continue
                if entry.state == "dispatched":
                    return False
                entry.state = "dispatched"
                entry.dispatched_at = stamp
                entry.updated_at = stamp
                changed = True
                break
            if changed:
                self._save_structured_unlocked(state, subscriptions, timers, wakeups)
            return changed

    def mark_event_dispatched(
        self, wakeup_id: str, *, dispatched_at: Optional[str] = None
    ) -> bool:
        return self.mark_wakeup_dispatched(wakeup_id, dispatched_at=dispatched_at)


__all__ = [
    "PMA_AUTOMATION_STORE_FILENAME",
    "PMA_AUTOMATION_VERSION",
    "PmaLifecycleSubscription",
    "PmaAutomationTimer",
    "PmaAutomationWakeup",
    "PmaAutomationStore",
    "default_pma_automation_state",
]
