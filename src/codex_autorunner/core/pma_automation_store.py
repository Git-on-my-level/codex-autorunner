from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast

from .chat_bindings import (
    active_chat_binding_metadata_by_thread,
    preferred_non_pma_chat_notification_source_for_workspace,
)
from .config import load_hub_config
from .config_contract import ConfigError
from .locks import file_lock
from .orchestration.sqlite import open_orchestration_sqlite
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
from .pma_dispatch_decision import (
    build_pma_dispatch_decision,
    pma_dispatch_decision_to_dict,
)
from .pma_domain.automation_reducer import (
    reduce_dequeue_due_timers,
    reduce_timer_touch,
    reduce_wakeup_dispatch,
)
from .pma_domain.models import PmaSubscription, PmaTimer, PmaWakeup
from .pma_domain.subscription_reducer import (
    ReduceTransitionResult,
    TimerFiredEvent,
    TransitionEvent,
    reduce_timer_fired,
    reduce_transition,
)
from .pma_origin import extract_pma_origin_metadata, merge_pma_origin_metadata
from .pma_thread_store import PmaThreadStore
from .text_utils import _normalize_pma_delivery_target, lock_path_for

logger = logging.getLogger(__name__)

MANAGED_THREAD_AUTO_SUBSCRIPTION_PREFIXES = (
    "managed-thread-notify:",
    "managed-thread-send-notify:",
)


class PmaAutomationThreadNotFoundError(ValueError):
    def __init__(self, thread_id: str) -> None:
        super().__init__(f"Unknown thread_id: {thread_id}")
        self.thread_id = thread_id


def _normalize_delivery_target(value: Any) -> Optional[dict[str, str]]:
    normalized = _normalize_pma_delivery_target(value)
    if normalized is None:
        return None
    surface_kind, surface_key = normalized
    return {
        "surface_kind": surface_kind,
        "surface_key": surface_key,
    }


def _repo_scoped_subscription_warning(
    *,
    hub_root: Path,
    repo_id: Optional[str],
    thread_id: Optional[str],
) -> Optional[str]:
    normalized_repo_id = _normalize_text(repo_id)
    normalized_thread_id = _normalize_text(thread_id)
    if normalized_repo_id is None or normalized_thread_id is not None:
        return None
    thread_count = (
        PmaThreadStore(hub_root)
        .count_threads_by_repo(status="active")
        .get(
            normalized_repo_id,
            0,
        )
    )
    if thread_count <= 1:
        return None
    return (
        "thread_id omitted; this subscription is repo-scoped and may match any of "
        f"{thread_count} active managed threads in repo {normalized_repo_id}. "
        "Pass thread_id to scope it to one managed thread."
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


class PmaAutomationStore:
    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = hub_root
        self._durable = durable
        self._path = (
            hub_root / ".codex-autorunner" / "pma" / PMA_AUTOMATION_STORE_FILENAME
        )

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return lock_path_for(self._path)

    def load(self) -> dict[str, Any]:
        with file_lock(self._lock_path()):
            state = self._load_unlocked()
            if state is not None:
                return state
            state = default_pma_automation_state()
            return state

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        try:
            with open_orchestration_sqlite(
                self._hub_root,
                durable=self._durable,
                migrate=False,
            ) as conn:
                subscriptions = self._load_subscriptions_from_sqlite(conn)
                timers = self._load_timers_from_sqlite(conn)
                wakeups = self._load_wakeups_from_sqlite(conn)
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            return None
        updated_values: list[str] = []
        updated_values.extend(entry.updated_at for entry in subscriptions)
        updated_values.extend(entry.updated_at for entry in timers)
        updated_values.extend(entry.updated_at for entry in wakeups)
        state = default_pma_automation_state()
        if updated_values:
            state["updated_at"] = max(updated_values)
        state["subscriptions"] = [entry.to_dict() for entry in subscriptions]
        state["timers"] = [entry.to_dict() for entry in timers]
        state["wakeups"] = [entry.to_dict() for entry in wakeups]
        return state

    def _load_structured_unlocked(
        self,
    ) -> tuple[
        dict[str, Any],
        list[PmaLifecycleSubscription],
        list[PmaAutomationTimer],
        list[PmaAutomationWakeup],
    ]:
        state = self._load_unlocked()
        if state is None:
            state = default_pma_automation_state()
        return (
            state,
            self._normalize_subscriptions(state.get("subscriptions")),
            self._normalize_timers(state.get("timers")),
            self._normalize_wakeups(state.get("wakeups")),
        )

    def _save_structured_unlocked(
        self,
        state: dict[str, Any],
        subscriptions: list[PmaLifecycleSubscription],
        timers: list[PmaAutomationTimer],
        wakeups: list[PmaAutomationWakeup],
    ) -> None:
        subscription_ids = {
            entry.subscription_id
            for entry in subscriptions
            if entry.subscription_id.strip()
        }
        filtered_timers = [
            entry
            for entry in timers
            if entry.subscription_id is None
            or entry.subscription_id in subscription_ids
        ]
        filtered_wakeups = [
            entry
            for entry in wakeups
            if entry.subscription_id is None
            or entry.subscription_id in subscription_ids
        ]
        dropped_timers = len(timers) - len(filtered_timers)
        dropped_wakeups = len(wakeups) - len(filtered_wakeups)
        if dropped_timers or dropped_wakeups:
            logger.warning(
                "Dropping orphaned automation rows before save (timers=%s, wakeups=%s)",
                dropped_timers,
                dropped_wakeups,
            )
        state["updated_at"] = _iso_now()
        state["subscriptions"] = [entry.to_dict() for entry in subscriptions]
        state["timers"] = [entry.to_dict() for entry in filtered_timers]
        state["wakeups"] = [entry.to_dict() for entry in filtered_wakeups]
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                conn.execute("DELETE FROM orch_automation_wakeups")
                conn.execute("DELETE FROM orch_automation_timers")
                conn.execute("DELETE FROM orch_automation_subscriptions")
                for subscription in subscriptions:
                    conn.execute(
                        """
                        INSERT INTO orch_automation_subscriptions (
                            subscription_id,
                            event_types_json,
                            repo_id,
                            run_id,
                            thread_target_id,
                            binding_id,
                            lane_id,
                            from_state,
                            to_state,
                            notify_once,
                            state,
                            match_count,
                            metadata_json,
                            created_at,
                            updated_at,
                            disabled_at,
                            reason_text,
                            idempotency_key,
                            max_matches
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            subscription.subscription_id,
                            json.dumps(subscription.event_types),
                            subscription.repo_id,
                            subscription.run_id,
                            subscription.thread_id,
                            None,
                            subscription.lane_id,
                            subscription.from_state,
                            subscription.to_state,
                            1 if subscription.max_matches == 1 else 0,
                            subscription.state,
                            subscription.match_count,
                            json.dumps(subscription.metadata),
                            subscription.created_at,
                            subscription.updated_at,
                            (
                                subscription.updated_at
                                if subscription.state != "active"
                                else None
                            ),
                            subscription.reason,
                            subscription.idempotency_key,
                            subscription.max_matches,
                        ),
                    )
                for timer in filtered_timers:
                    conn.execute(
                        """
                        INSERT INTO orch_automation_timers (
                            timer_id,
                            subscription_id,
                            repo_id,
                            run_id,
                            thread_target_id,
                            timer_kind,
                            schedule_key,
                            available_at,
                            payload_json,
                            state,
                            created_at,
                            updated_at,
                            fired_at,
                            reason_text,
                            idempotency_key,
                            idle_seconds
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            timer.timer_id,
                            timer.subscription_id,
                            timer.repo_id,
                            timer.run_id,
                            timer.thread_id,
                            timer.timer_type,
                            timer.subscription_id or timer.idempotency_key,
                            timer.due_at,
                            json.dumps(
                                {
                                    "metadata": timer.metadata,
                                    "from_state": timer.from_state,
                                    "to_state": timer.to_state,
                                }
                            ),
                            timer.state,
                            timer.created_at,
                            timer.updated_at,
                            timer.fired_at,
                            timer.reason,
                            timer.idempotency_key,
                            timer.idle_seconds,
                        ),
                    )
                for wakeup in filtered_wakeups:
                    conn.execute(
                        """
                        INSERT INTO orch_automation_wakeups (
                            wakeup_id,
                            subscription_id,
                            repo_id,
                            run_id,
                            thread_target_id,
                            lane_id,
                            wakeup_kind,
                            state,
                            available_at,
                            claimed_at,
                            completed_at,
                            reason_text,
                            payload_json,
                            created_at,
                            updated_at,
                            dispatched_at,
                            timestamp,
                            idempotency_key,
                            timer_id,
                            event_id,
                            event_type
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            wakeup.wakeup_id,
                            wakeup.subscription_id,
                            wakeup.repo_id,
                            wakeup.run_id,
                            wakeup.thread_id,
                            wakeup.lane_id,
                            wakeup.source,
                            wakeup.state,
                            wakeup.timestamp,
                            None,
                            None,
                            wakeup.reason,
                            json.dumps(
                                {
                                    "metadata": wakeup.metadata,
                                    "event_data": wakeup.event_data,
                                    "from_state": wakeup.from_state,
                                    "to_state": wakeup.to_state,
                                }
                            ),
                            wakeup.created_at,
                            wakeup.updated_at,
                            wakeup.dispatched_at,
                            wakeup.timestamp,
                            wakeup.idempotency_key,
                            wakeup.timer_id,
                            wakeup.event_id,
                            wakeup.event_type,
                        ),
                    )

    @staticmethod
    def _json_load(raw: str | None) -> dict[str, Any]:
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _json_load_list(raw: str | None) -> list[str]:
        if not isinstance(raw, str) or not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [str(item).lower() for item in parsed if isinstance(item, str)]

    def _row_to_subscription(self, row: Any) -> PmaLifecycleSubscription:
        return PmaLifecycleSubscription(
            subscription_id=str(row["subscription_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            state=str(row["state"] or "active"),
            event_types=self._json_load_list(row["event_types_json"]),
            repo_id=row["repo_id"],
            run_id=row["run_id"],
            thread_id=row["thread_target_id"],
            lane_id=str(row["lane_id"] or DEFAULT_PMA_LANE_ID),
            from_state=row["from_state"],
            to_state=row["to_state"],
            reason=row["reason_text"],
            idempotency_key=row["idempotency_key"],
            max_matches=(
                int(row["max_matches"]) if row["max_matches"] is not None else None
            ),
            match_count=int(row["match_count"] or 0),
            metadata=self._json_load(row["metadata_json"]),
        )

    def _row_to_timer(self, row: Any) -> PmaAutomationTimer:
        payload = self._json_load(row["payload_json"])
        metadata = cast(
            dict[str, Any],
            (
                payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {}
            ),
        )
        return PmaAutomationTimer(
            timer_id=str(row["timer_id"]),
            due_at=str(row["available_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            state=str(row["state"] or "pending"),
            fired_at=row["fired_at"],
            timer_type=str(row["timer_kind"] or TIMER_TYPE_ONE_SHOT),
            idle_seconds=(
                int(row["idle_seconds"]) if row["idle_seconds"] is not None else None
            ),
            subscription_id=row["subscription_id"],
            repo_id=row["repo_id"],
            run_id=row["run_id"],
            thread_id=row["thread_target_id"],
            lane_id=_normalize_lane_id(payload.get("lane_id")),
            from_state=_normalize_text(payload.get("from_state")),
            to_state=_normalize_text(payload.get("to_state")),
            reason=row["reason_text"],
            idempotency_key=row["idempotency_key"],
            metadata=metadata,
        )

    def _row_to_wakeup(self, row: Any) -> PmaAutomationWakeup:
        payload = self._json_load(row["payload_json"])
        event_data = cast(
            dict[str, Any],
            (
                payload.get("event_data")
                if isinstance(payload.get("event_data"), dict)
                else {}
            ),
        )
        metadata = cast(
            dict[str, Any],
            (
                payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {}
            ),
        )
        return PmaAutomationWakeup(
            wakeup_id=str(row["wakeup_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            state=str(row["state"] or "pending"),
            dispatched_at=row["dispatched_at"],
            source=str(row["wakeup_kind"] or "automation"),
            repo_id=row["repo_id"],
            run_id=row["run_id"],
            thread_id=row["thread_target_id"],
            lane_id=str(row["lane_id"] or DEFAULT_PMA_LANE_ID),
            from_state=_normalize_text(payload.get("from_state")),
            to_state=_normalize_text(payload.get("to_state")),
            reason=row["reason_text"],
            timestamp=row["timestamp"],
            idempotency_key=row["idempotency_key"],
            subscription_id=row["subscription_id"],
            timer_id=row["timer_id"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            event_data=event_data,
            metadata=metadata,
        )

    def _load_subscriptions_from_sqlite(
        self,
        conn: Any,
    ) -> list[PmaLifecycleSubscription]:
        rows = conn.execute(
            """
            SELECT *
              FROM orch_automation_subscriptions
             ORDER BY created_at ASC, subscription_id ASC
            """
        ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def _load_timers_from_sqlite(self, conn: Any) -> list[PmaAutomationTimer]:
        rows = conn.execute(
            """
            SELECT *
              FROM orch_automation_timers
             ORDER BY created_at ASC, timer_id ASC
            """
        ).fetchall()
        return [self._row_to_timer(row) for row in rows]

    def _load_wakeups_from_sqlite(self, conn: Any) -> list[PmaAutomationWakeup]:
        rows = conn.execute(
            """
            SELECT *
              FROM orch_automation_wakeups
             ORDER BY created_at ASC, wakeup_id ASC
            """
        ).fetchall()
        return [self._row_to_wakeup(row) for row in rows]

    def _insert_subscription_row(
        self, conn: Any, subscription: PmaLifecycleSubscription
    ) -> None:
        conn.execute(
            """
            INSERT INTO orch_automation_subscriptions (
                subscription_id, event_types_json, repo_id, run_id,
                thread_target_id, binding_id, lane_id, from_state, to_state,
                notify_once, state, match_count, metadata_json,
                created_at, updated_at, disabled_at,
                reason_text, idempotency_key, max_matches
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subscription.subscription_id,
                json.dumps(subscription.event_types),
                subscription.repo_id,
                subscription.run_id,
                subscription.thread_id,
                None,
                subscription.lane_id,
                subscription.from_state,
                subscription.to_state,
                1 if subscription.max_matches == 1 else 0,
                subscription.state,
                subscription.match_count,
                json.dumps(subscription.metadata),
                subscription.created_at,
                subscription.updated_at,
                (subscription.updated_at if subscription.state != "active" else None),
                subscription.reason,
                subscription.idempotency_key,
                subscription.max_matches,
            ),
        )

    def _insert_timer_row(self, conn: Any, timer: PmaAutomationTimer) -> None:
        conn.execute(
            """
            INSERT INTO orch_automation_timers (
                timer_id, subscription_id, repo_id, run_id,
                thread_target_id, timer_kind, schedule_key, available_at,
                payload_json, state, created_at, updated_at,
                fired_at, reason_text, idempotency_key, idle_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timer.timer_id,
                timer.subscription_id,
                timer.repo_id,
                timer.run_id,
                timer.thread_id,
                timer.timer_type,
                timer.subscription_id or timer.idempotency_key,
                timer.due_at,
                json.dumps(
                    {
                        "metadata": timer.metadata,
                        "from_state": timer.from_state,
                        "to_state": timer.to_state,
                    }
                ),
                timer.state,
                timer.created_at,
                timer.updated_at,
                timer.fired_at,
                timer.reason,
                timer.idempotency_key,
                timer.idle_seconds,
            ),
        )

    def _find_active_subscription_by_key(
        self, conn: Any, key: str
    ) -> Optional[PmaLifecycleSubscription]:
        rows = conn.execute(
            """
            SELECT *
              FROM orch_automation_subscriptions
             WHERE idempotency_key = ? AND state = 'active'
             LIMIT 1
            """,
            (key,),
        ).fetchall()
        return self._row_to_subscription(rows[0]) if rows else None

    def _find_pending_timer_by_key(
        self, conn: Any, key: str
    ) -> Optional[PmaAutomationTimer]:
        rows = conn.execute(
            """
            SELECT *
              FROM orch_automation_timers
             WHERE idempotency_key = ? AND state = 'pending'
             LIMIT 1
            """,
            (key,),
        ).fetchall()
        return self._row_to_timer(rows[0]) if rows else None

    def _subscription_id_exists(self, conn: Any, sub_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM orch_automation_subscriptions WHERE subscription_id = ?",
            (sub_id,),
        ).fetchone()
        return row is not None

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
            except (TypeError, ValueError, KeyError):
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
            except (TypeError, ValueError, KeyError):
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
            except (TypeError, ValueError, KeyError):
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

    def _compute_dispatch_decision_for_wakeup(
        self, wakeup: PmaAutomationWakeup
    ) -> None:
        try:
            binding_metadata_by_thread = active_chat_binding_metadata_by_thread(
                hub_root=self._hub_root
            )
        except (OSError, RuntimeError, ValueError):
            binding_metadata_by_thread = {}

        delivery_target = wakeup.metadata.get("delivery_target")
        workspace_root: Optional[Path] = None
        if wakeup.thread_id:
            try:
                thread_store = PmaThreadStore(self._hub_root)
                thread = thread_store.get_thread(wakeup.thread_id)
                if isinstance(thread, dict):
                    raw_ws = _normalize_text(thread.get("workspace_root"))
                    if raw_ws:
                        workspace_root = Path(raw_ws)
            except (OSError, RuntimeError, ValueError):
                pass

        preferred_bound_surface_kinds: tuple[str, ...] = ("discord", "telegram")
        if workspace_root is not None:
            try:
                raw_config = load_hub_config(self._hub_root).raw
            except (OSError, ValueError, ConfigError):
                raw_config = {}
            try:
                preferred = preferred_non_pma_chat_notification_source_for_workspace(
                    hub_root=self._hub_root,
                    raw_config=raw_config,
                    workspace_root=workspace_root,
                )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                preferred = None
            if preferred in {"discord", "telegram"}:
                ordered = [preferred]
                ordered.extend(s for s in ("discord", "telegram") if s != preferred)
                preferred_bound_surface_kinds = tuple(ordered)

        decision = build_pma_dispatch_decision(
            message="",
            requested_delivery="auto",
            source_kind=wakeup.source or "automation",
            repo_id=wakeup.repo_id,
            workspace_root=workspace_root,
            lane_id=wakeup.lane_id,
            managed_thread_id=wakeup.thread_id,
            delivery_target=(
                delivery_target if isinstance(delivery_target, dict) else None
            ),
            context_payload={"wake_up": wakeup.to_dict()},
            binding_metadata_by_thread=binding_metadata_by_thread,
            preferred_bound_surface_kinds=preferred_bound_surface_kinds,
        )
        wakeup.metadata["dispatch_decision"] = pma_dispatch_decision_to_dict(decision)

    @staticmethod
    def _lifecycle_sub_to_domain(
        entry: PmaLifecycleSubscription,
    ) -> PmaSubscription:
        return PmaSubscription(
            subscription_id=entry.subscription_id,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            state=entry.state,
            event_types=tuple(entry.event_types),
            repo_id=entry.repo_id,
            run_id=entry.run_id,
            thread_id=entry.thread_id,
            lane_id=entry.lane_id,
            from_state=entry.from_state,
            to_state=entry.to_state,
            reason=entry.reason,
            idempotency_key=entry.idempotency_key,
            max_matches=entry.max_matches,
            match_count=entry.match_count,
            metadata=dict(entry.metadata),
        )

    @staticmethod
    def _store_timer_to_domain(entry: PmaAutomationTimer) -> PmaTimer:
        return PmaTimer(
            timer_id=entry.timer_id,
            due_at=entry.due_at,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            state=entry.state,
            fired_at=entry.fired_at,
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

    @staticmethod
    def _store_wakeup_to_domain(entry: PmaAutomationWakeup) -> PmaWakeup:
        return PmaWakeup(
            wakeup_id=entry.wakeup_id,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            state=entry.state,
            dispatched_at=entry.dispatched_at,
            source=entry.source,
            repo_id=entry.repo_id,
            run_id=entry.run_id,
            thread_id=entry.thread_id,
            lane_id=entry.lane_id,
            from_state=entry.from_state,
            to_state=entry.to_state,
            reason=entry.reason,
            timestamp=entry.timestamp,
            idempotency_key=entry.idempotency_key,
            subscription_id=entry.subscription_id,
            timer_id=entry.timer_id,
            event_id=entry.event_id,
            event_type=entry.event_type,
            event_data=dict(entry.event_data or {}),
            metadata=dict(entry.metadata or {}),
        )

    @staticmethod
    def _apply_domain_timer_to_store(
        store_timer: PmaAutomationTimer, domain_timer: PmaTimer
    ) -> None:
        store_timer.due_at = domain_timer.due_at
        store_timer.updated_at = domain_timer.updated_at
        store_timer.state = domain_timer.state
        store_timer.fired_at = domain_timer.fired_at
        store_timer.idle_seconds = domain_timer.idle_seconds
        store_timer.reason = domain_timer.reason
        store_timer.metadata = dict(domain_timer.metadata or {})

    @staticmethod
    def _apply_domain_wakeup_to_store(
        store_wakeup: PmaAutomationWakeup, domain_wakeup: PmaWakeup
    ) -> None:
        store_wakeup.state = domain_wakeup.state
        store_wakeup.dispatched_at = domain_wakeup.dispatched_at
        store_wakeup.updated_at = domain_wakeup.updated_at
        store_wakeup.metadata = dict(domain_wakeup.metadata or {})

    def _apply_reduce_result(
        self,
        subscriptions: list[PmaLifecycleSubscription],
        wakeups: list[PmaAutomationWakeup],
        result: ReduceTransitionResult,
        timestamp: str,
        *,
        compute_dispatch: bool = True,
    ) -> list[PmaAutomationWakeup]:
        sub_by_id = {entry.subscription_id: entry for entry in subscriptions}
        now = _iso_now()
        for domain_sub in result.subscriptions:
            existing = sub_by_id.get(domain_sub.subscription_id)
            if existing is None:
                continue
            if existing.match_count != domain_sub.match_count:
                existing.match_count = domain_sub.match_count
                existing.updated_at = now
            if existing.state != domain_sub.state:
                existing.state = domain_sub.state
                existing.updated_at = now
        appended: list[PmaAutomationWakeup] = []
        for intent in result.wakeup_intents:
            wakeup = PmaAutomationWakeup.create(
                source=intent.source,
                repo_id=intent.repo_id,
                run_id=intent.run_id,
                thread_id=intent.thread_id,
                lane_id=intent.lane_id,
                from_state=intent.from_state,
                to_state=intent.to_state,
                reason=intent.reason,
                timestamp=timestamp,
                idempotency_key=intent.idempotency_key,
                subscription_id=intent.subscription_id,
                event_type=intent.event_type,
                event_id=intent.event_id,
                event_data=intent.event_data,
                metadata=intent.metadata,
            )
            if compute_dispatch:
                self._compute_dispatch_decision_for_wakeup(wakeup)
            wakeups.append(wakeup)
            appended.append(wakeup)
        return appended

    @staticmethod
    def _normalize_subscription_event_types(
        value: Any,
        *,
        singular: Any = None,
    ) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        def _append(candidate: Any) -> None:
            text = _normalize_text(candidate)
            if text is None:
                return
            lowered = text.lower()
            if lowered in seen:
                return
            seen.add(lowered)
            normalized.append(lowered)

        if isinstance(value, (list, tuple, set)):
            for item in value:
                _append(item)
        else:
            _append(value)
        _append(singular)
        return normalized

    @staticmethod
    def _coerce_limit(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            parsed = int(value)
        except (ValueError, TypeError):
            return None
        if parsed < 0:
            return None
        return parsed

    def _resolve_thread_lane_id(self, *, thread_id: str) -> str:
        thread_store = PmaThreadStore(self._hub_root)
        thread = thread_store.get_thread(thread_id)
        if thread is None:
            raise PmaAutomationThreadNotFoundError(thread_id)

        binding_metadata = active_chat_binding_metadata_by_thread(
            hub_root=self._hub_root
        ).get(thread_id)
        binding_kind = (
            _normalize_text(binding_metadata.get("binding_kind"))
            if isinstance(binding_metadata, dict)
            else None
        )
        if binding_kind in {"discord", "telegram"}:
            return binding_kind
        return DEFAULT_PMA_LANE_ID

    def _resolve_thread_delivery_target(
        self, *, thread_id: str
    ) -> Optional[dict[str, str]]:
        thread_store = PmaThreadStore(self._hub_root)
        thread = thread_store.get_thread(thread_id)
        if thread is None:
            raise PmaAutomationThreadNotFoundError(thread_id)

        binding_metadata = active_chat_binding_metadata_by_thread(
            hub_root=self._hub_root
        ).get(thread_id)
        if not isinstance(binding_metadata, dict):
            return None
        return _normalize_delivery_target(
            {
                "surface_kind": binding_metadata.get("binding_kind"),
                "surface_key": binding_metadata.get("binding_id"),
            }
        )

    def _resolve_subscription_lane_id(
        self,
        *,
        thread_id: Optional[str],
        lane_id: Optional[str],
        metadata: Optional[dict[str, Any]] = None,
        origin_thread_id: Optional[str] = None,
        origin_lane_id: Optional[str] = None,
    ) -> str:
        origin_metadata = extract_pma_origin_metadata(metadata)
        normalized_thread_id = _normalize_text(thread_id)
        normalized_lane_id = _normalize_text(lane_id)
        normalized_origin_thread_id = _normalize_text(origin_thread_id) or (
            origin_metadata.thread_id if origin_metadata else None
        )
        normalized_origin_lane_id = _normalize_text(origin_lane_id) or (
            origin_metadata.lane_id if origin_metadata else None
        )
        if normalized_lane_id is not None:
            return _normalize_lane_id(normalized_lane_id)
        if normalized_origin_lane_id is not None:
            return _normalize_lane_id(normalized_origin_lane_id)

        if normalized_origin_thread_id is not None:
            try:
                resolved_origin_lane_id = self._resolve_thread_lane_id(
                    thread_id=normalized_origin_thread_id
                )
            except PmaAutomationThreadNotFoundError:
                resolved_origin_lane_id = DEFAULT_PMA_LANE_ID
            if resolved_origin_lane_id != DEFAULT_PMA_LANE_ID:
                return resolved_origin_lane_id

        if normalized_thread_id is not None:
            resolved_lane_id = self._resolve_thread_lane_id(
                thread_id=normalized_thread_id
            )
            if resolved_lane_id != DEFAULT_PMA_LANE_ID:
                return resolved_lane_id
        if normalized_thread_id is None:
            return DEFAULT_PMA_LANE_ID
        return DEFAULT_PMA_LANE_ID

    @staticmethod
    def _is_auto_subscription_key(idempotency_key: Optional[str]) -> bool:
        normalized_key = _normalize_text(idempotency_key)
        if normalized_key is None:
            return False
        return normalized_key.startswith(MANAGED_THREAD_AUTO_SUBSCRIPTION_PREFIXES)

    @staticmethod
    def _scope_value_is_covered(
        *, requested: Optional[str], existing: Optional[str]
    ) -> bool:
        if existing is None:
            return True
        if requested is None:
            return False
        return existing == requested

    @staticmethod
    def _event_types_are_covered(
        *,
        requested: list[str],
        existing: list[str],
    ) -> bool:
        if not existing:
            return True
        if not requested:
            return False
        existing_set = set(existing)
        return all(event_type in existing_set for event_type in requested)

    def _find_covering_auto_subscription(
        self,
        *,
        event_types: list[str],
        repo_id: Optional[str],
        run_id: Optional[str],
        thread_id: Optional[str],
        from_state: Optional[str],
        to_state: Optional[str],
    ) -> Optional[PmaLifecycleSubscription]:
        state = self.load()
        subscriptions = self._normalize_subscriptions(state.get("subscriptions"))
        for entry in subscriptions:
            if entry.state != "active":
                continue
            if not self._is_auto_subscription_key(entry.idempotency_key):
                continue
            if not self._event_types_are_covered(
                requested=event_types,
                existing=entry.event_types,
            ):
                continue
            if not self._scope_value_is_covered(
                requested=repo_id,
                existing=entry.repo_id,
            ):
                continue
            if not self._scope_value_is_covered(
                requested=run_id,
                existing=entry.run_id,
            ):
                continue
            if not self._scope_value_is_covered(
                requested=thread_id,
                existing=entry.thread_id,
            ):
                continue
            if not self._scope_value_is_covered(
                requested=from_state,
                existing=entry.from_state,
            ):
                continue
            if not self._scope_value_is_covered(
                requested=to_state,
                existing=entry.to_state,
            ):
                continue
            return entry
        return None

    def _resolve_subscription_metadata(
        self,
        *,
        thread_id: Optional[str],
        metadata: Optional[dict[str, Any]],
        origin_thread_id: Optional[str] = None,
        origin_lane_id: Optional[str] = None,
    ) -> dict[str, Any]:
        resolved_metadata = merge_pma_origin_metadata(
            metadata,
            origin_thread_id=origin_thread_id,
            origin_lane_id=origin_lane_id,
        )
        delivery_target = _normalize_delivery_target(
            resolved_metadata.get("delivery_target")
        )
        normalized_thread_id = _normalize_text(thread_id)
        origin_metadata = extract_pma_origin_metadata(resolved_metadata)
        normalized_origin_thread_id = _normalize_text(origin_thread_id) or (
            origin_metadata.thread_id if origin_metadata else None
        )
        if delivery_target is None and normalized_origin_thread_id is not None:
            try:
                delivery_target = self._resolve_thread_delivery_target(
                    thread_id=normalized_origin_thread_id
                )
            except PmaAutomationThreadNotFoundError:
                delivery_target = None
        if delivery_target is None and normalized_thread_id is not None:
            try:
                delivery_target = self._resolve_thread_delivery_target(
                    thread_id=normalized_thread_id
                )
            except PmaAutomationThreadNotFoundError:
                delivery_target = None
        if delivery_target is not None:
            resolved_metadata["delivery_target"] = delivery_target
        else:
            resolved_metadata.pop("delivery_target", None)
        return resolved_metadata

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
        origin_thread_id: Optional[str] = None,
        origin_lane_id: Optional[str] = None,
    ) -> tuple[PmaLifecycleSubscription, bool]:
        key = _normalize_text(idempotency_key)
        normalized_event_types = self._normalize_subscription_event_types(event_types)
        normalized_thread_id = _normalize_text(thread_id)
        resolved_lane_id = self._resolve_subscription_lane_id(
            thread_id=normalized_thread_id,
            lane_id=lane_id,
            metadata=metadata,
            origin_thread_id=origin_thread_id,
            origin_lane_id=origin_lane_id,
        )
        resolved_metadata = self._resolve_subscription_metadata(
            thread_id=normalized_thread_id,
            metadata=metadata,
            origin_thread_id=origin_thread_id,
            origin_lane_id=origin_lane_id,
        )
        if not normalized_event_types:
            logger.warning(
                "Creating PMA subscription with empty event_types; subscription will match all events"
            )
        with file_lock(self._lock_path()):
            with open_orchestration_sqlite(
                self._hub_root, durable=self._durable
            ) as conn:
                with conn:
                    if key is not None:
                        existing = self._find_active_subscription_by_key(conn, key)
                        if existing is not None:
                            return existing, True
                    created = PmaLifecycleSubscription.create(
                        event_types=normalized_event_types,
                        repo_id=repo_id,
                        run_id=run_id,
                        thread_id=normalized_thread_id,
                        lane_id=resolved_lane_id,
                        from_state=from_state,
                        to_state=to_state,
                        reason=reason,
                        idempotency_key=key,
                        max_matches=_resolve_subscription_max_matches(
                            max_matches=max_matches,
                            notify_once=notify_once,
                            metadata=resolved_metadata,
                        ),
                        metadata=resolved_metadata,
                    )
                    self._insert_subscription_row(conn, created)
            return created, False

    def create_subscription(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        data = self._coerce_payload(payload, kwargs)
        normalized_event_types = self._normalize_subscription_event_types(
            data.get("event_types"),
            singular=data.get("event_type"),
        )
        normalized_repo_id = _normalize_text(data.get("repo_id"))
        normalized_run_id = _normalize_text(data.get("run_id"))
        normalized_thread_id = _normalize_text(data.get("thread_id"))
        normalized_from_state = _normalize_text(data.get("from_state"))
        normalized_to_state = _normalize_text(data.get("to_state"))
        normalized_idempotency_key = _normalize_text(data.get("idempotency_key"))
        normalized_origin_thread_id = _normalize_text(data.get("origin_thread_id"))
        normalized_origin_lane_id = _normalize_text(data.get("origin_lane_id"))
        confirm_duplicate = _normalize_bool(data.get("confirm"), fallback=False)
        is_auto_subscription = self._is_auto_subscription_key(
            normalized_idempotency_key
        )
        if not confirm_duplicate:
            existing_auto = self._find_covering_auto_subscription(
                event_types=normalized_event_types,
                repo_id=normalized_repo_id,
                run_id=normalized_run_id,
                thread_id=normalized_thread_id,
                from_state=normalized_from_state,
                to_state=normalized_to_state,
            )
            if existing_auto is not None:
                if is_auto_subscription:
                    return {
                        "subscription": existing_auto.to_dict(),
                        "deduped": True,
                    }
                scope_label = "this scope"
                if normalized_thread_id is not None:
                    scope_label = "this thread"
                elif normalized_run_id is not None:
                    scope_label = "this run"
                elif normalized_repo_id is not None:
                    scope_label = "this repo"
                event_label = (
                    normalized_event_types[0]
                    if len(normalized_event_types) == 1
                    else "the requested event scope"
                )
                return {
                    "subscription": existing_auto.to_dict(),
                    "deduped": True,
                    "warning": (
                        "An active auto-subscription "
                        f"({existing_auto.idempotency_key or existing_auto.subscription_id}) "
                        f"already covers {event_label} for {scope_label}. "
                        "Pass confirm=true to create a duplicate subscription."
                    ),
                }
        created, deduped = self.upsert_subscription(
            event_types=normalized_event_types or None,
            repo_id=normalized_repo_id,
            run_id=normalized_run_id,
            thread_id=normalized_thread_id,
            lane_id=_normalize_text(data.get("lane_id")),
            from_state=normalized_from_state,
            to_state=normalized_to_state,
            reason=_normalize_text(data.get("reason")),
            idempotency_key=normalized_idempotency_key,
            notify_once=_normalize_bool(data.get("notify_once"), fallback=None),
            max_matches=_normalize_positive_int(data.get("max_matches"), fallback=None),
            metadata=(
                data.get("metadata") if isinstance(data.get("metadata"), dict) else None
            ),
            origin_thread_id=normalized_origin_thread_id,
            origin_lane_id=normalized_origin_lane_id,
        )
        result = {"subscription": created.to_dict(), "deduped": deduped}
        scope_warning = _repo_scoped_subscription_warning(
            hub_root=self._hub_root,
            repo_id=normalized_repo_id,
            thread_id=normalized_thread_id,
        )
        if scope_warning:
            existing = result.get("warning")
            if isinstance(existing, str) and existing.strip():
                result["warning"] = f"{existing}\n{scope_warning}"
            else:
                result["warning"] = scope_warning
        return result

    def cancel_subscription(self, subscription_id: str) -> bool:
        target_id = _normalize_text(subscription_id)
        if target_id is None:
            return False
        stamp = _iso_now()
        with file_lock(self._lock_path()):
            with open_orchestration_sqlite(
                self._hub_root, durable=self._durable
            ) as conn:
                with conn:
                    cursor = conn.execute(
                        """
                        UPDATE orch_automation_subscriptions
                           SET state = 'cancelled',
                               updated_at = ?,
                               disabled_at = ?
                         WHERE subscription_id = ?
                           AND state != 'cancelled'
                        """,
                        (stamp, stamp, target_id),
                    )
                    changed = cursor.rowcount > 0
            return changed

    def purge_subscription(
        self, subscription_id: str, *, require_inactive: bool = True
    ) -> bool:
        target_id = _normalize_text(subscription_id)
        if target_id is None:
            return False
        with file_lock(self._lock_path()):
            with open_orchestration_sqlite(
                self._hub_root, durable=self._durable
            ) as conn:
                with conn:
                    row = conn.execute(
                        "SELECT state FROM orch_automation_subscriptions WHERE subscription_id = ?",
                        (target_id,),
                    ).fetchone()
                    if row is None:
                        return False
                    if require_inactive and str(row["state"]) == "active":
                        return False
                    orphaned_timers = conn.execute(
                        "SELECT COUNT(*) AS c FROM orch_automation_timers WHERE subscription_id = ?",
                        (target_id,),
                    ).fetchone()["c"]
                    orphaned_wakeups = conn.execute(
                        "SELECT COUNT(*) AS c FROM orch_automation_wakeups WHERE subscription_id = ?",
                        (target_id,),
                    ).fetchone()["c"]
                    if orphaned_timers or orphaned_wakeups:
                        logger.warning(
                            "Dropping orphaned automation rows before save (timers=%s, wakeups=%s)",
                            orphaned_timers,
                            orphaned_wakeups,
                        )
                    conn.execute(
                        "DELETE FROM orch_automation_wakeups WHERE subscription_id = ?",
                        (target_id,),
                    )
                    conn.execute(
                        "DELETE FROM orch_automation_timers WHERE subscription_id = ?",
                        (target_id,),
                    )
                    conn.execute(
                        "DELETE FROM orch_automation_subscriptions WHERE subscription_id = ?",
                        (target_id,),
                    )
            return True

    def purge_subscriptions(
        self,
        *,
        state_filter: Optional[str] = None,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        target_state = _normalize_text(state_filter)
        with file_lock(self._lock_path()):
            with open_orchestration_sqlite(
                self._hub_root, durable=self._durable
            ) as conn:
                query = "SELECT * FROM orch_automation_subscriptions"
                params: tuple[Any, ...] = ()
                if target_state is not None:
                    query += " WHERE state = ?"
                    params = (target_state,)
                rows = conn.execute(query, params).fetchall()
                removed = [self._row_to_subscription(row) for row in rows]
                if removed and not dry_run:
                    removed_ids = [entry.subscription_id for entry in removed]
                    total_orphaned_timers = 0
                    total_orphaned_wakeups = 0
                    with conn:
                        for sub_id in removed_ids:
                            orphaned_timers = conn.execute(
                                "SELECT COUNT(*) AS c FROM orch_automation_timers WHERE subscription_id = ?",
                                (sub_id,),
                            ).fetchone()["c"]
                            orphaned_wakeups = conn.execute(
                                "SELECT COUNT(*) AS c FROM orch_automation_wakeups WHERE subscription_id = ?",
                                (sub_id,),
                            ).fetchone()["c"]
                            total_orphaned_timers += orphaned_timers
                            total_orphaned_wakeups += orphaned_wakeups
                            conn.execute(
                                "DELETE FROM orch_automation_wakeups WHERE subscription_id = ?",
                                (sub_id,),
                            )
                            conn.execute(
                                "DELETE FROM orch_automation_timers WHERE subscription_id = ?",
                                (sub_id,),
                            )
                        placeholders = ",".join("?" for _ in removed_ids)
                        conn.execute(
                            f"DELETE FROM orch_automation_subscriptions WHERE subscription_id IN ({placeholders})",
                            tuple(removed_ids),
                        )
                    if total_orphaned_timers or total_orphaned_wakeups:
                        logger.warning(
                            "Dropping orphaned automation rows before save (timers=%s, wakeups=%s)",
                            total_orphaned_timers,
                            total_orphaned_wakeups,
                        )
            return [entry.to_dict() for entry in removed]

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
            with open_orchestration_sqlite(
                self._hub_root, durable=self._durable
            ) as conn:
                with conn:
                    normalized_subscription_id = _normalize_text(subscription_id)
                    if normalized_subscription_id is not None:
                        if not self._subscription_id_exists(
                            conn, normalized_subscription_id
                        ):
                            raise ValueError(
                                f"Unknown subscription_id: {normalized_subscription_id}"
                            )
                    if key is not None:
                        existing = self._find_pending_timer_by_key(conn, key)
                        if existing is not None:
                            return existing, True
                    created = PmaAutomationTimer.create(
                        due_at=normalized_due_at,
                        timer_type=timer_type,
                        idle_seconds=idle_seconds,
                        subscription_id=normalized_subscription_id,
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
                    self._insert_timer_row(conn, created)
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

    def cancel_timer(
        self,
        timer_id: str,
        payload: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> bool:
        target_id = _normalize_text(timer_id)
        if target_id is None:
            return False
        data = self._coerce_payload(payload, kwargs)
        reason = _normalize_text(data.get("reason"))
        cancelled_at = _normalize_due_timestamp(
            data.get("timestamp"), field_name="timestamp"
        )
        if cancelled_at is None:
            cancelled_at = _iso_now()
        with file_lock(self._lock_path()):
            with open_orchestration_sqlite(
                self._hub_root, durable=self._durable
            ) as conn:
                with conn:
                    if reason is not None:
                        cursor = conn.execute(
                            """
                            UPDATE orch_automation_timers
                               SET state = 'cancelled',
                                   updated_at = ?,
                                   reason_text = ?
                             WHERE timer_id = ?
                               AND state != 'cancelled'
                            """,
                            (cancelled_at, reason, target_id),
                        )
                    else:
                        cursor = conn.execute(
                            """
                            UPDATE orch_automation_timers
                               SET state = 'cancelled',
                                   updated_at = ?
                             WHERE timer_id = ?
                               AND state != 'cancelled'
                            """,
                            (cancelled_at, target_id),
                        )
                    changed = cursor.rowcount > 0
            return changed

    def purge_timer(self, timer_id: str, *, require_inactive: bool = True) -> bool:
        target_id = _normalize_text(timer_id)
        if target_id is None:
            return False
        with file_lock(self._lock_path()):
            with open_orchestration_sqlite(
                self._hub_root, durable=self._durable
            ) as conn:
                with conn:
                    row = conn.execute(
                        "SELECT state FROM orch_automation_timers WHERE timer_id = ?",
                        (target_id,),
                    ).fetchone()
                    if row is None:
                        return False
                    if require_inactive and str(row["state"]) == "pending":
                        return False
                    conn.execute(
                        "DELETE FROM orch_automation_timers WHERE timer_id = ?",
                        (target_id,),
                    )
            return True

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
                domain_timer = self._store_timer_to_domain(entry)
                now_dt = datetime.now(timezone.utc)
                result = reduce_timer_touch(
                    domain_timer,
                    due_at=due_at,
                    delay_seconds=delay_seconds,
                    reason=reason,
                    now=now_dt,
                )
                if result.touched and result.updated_timer is not None:
                    self._apply_domain_timer_to_store(entry, result.updated_timer)
                    self._save_structured_unlocked(
                        state, subscriptions, timers, wakeups
                    )
                    return {"status": "ok", "timer": entry.to_dict(), "touched": True}
        return {"status": "ok", "timer_id": target_id, "touched": False}

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
            domain_timers = [self._store_timer_to_domain(t) for t in timers]
            result = reduce_dequeue_due_timers(domain_timers, now_dt, limit=due_limit)
            for i, domain_timer in enumerate(result.updated_timers):
                if i < len(timers):
                    self._apply_domain_timer_to_store(timers[i], domain_timer)
            if result.fired_count > 0:
                self._save_structured_unlocked(state, subscriptions, timers, wakeups)
            return [asdict(output.fired_timer) for output in result.due]

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
            self._compute_dispatch_decision_for_wakeup(created)
            wakeups.append(created)
            self._save_structured_unlocked(state, subscriptions, timers, wakeups)
        return created, False

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

    def list_pending_wakeups(
        self, *, limit: int = 100, require_dispatch_decision: bool = False
    ) -> list[dict[str, Any]]:
        take = max(0, int(limit))
        if take <= 0:
            return []
        state = self.load()
        wakeups = self._normalize_wakeups(state.get("wakeups"))
        pending = [entry.to_dict() for entry in wakeups if entry.state == "pending"]
        if require_dispatch_decision:
            pending = [
                d
                for d in pending
                if isinstance((d.get("metadata") or {}).get("dispatch_decision"), dict)
            ]
        return pending[:take]

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

            event = TransitionEvent(
                repo_id=repo_id,
                run_id=run_id,
                thread_id=thread_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                event_type=event_type_norm,
                transition_id=transition_id,
                extra_metadata=metadata_payload,
            )

            domain_subs = [
                self._lifecycle_sub_to_domain(entry) for entry in subscriptions
            ]
            existing_keys = frozenset(
                existing.idempotency_key
                for existing in wakeups
                if existing.idempotency_key
            )
            result = reduce_transition(
                domain_subs,
                existing_keys,
                event,
                event_timestamp=timestamp,
            )

            self._apply_reduce_result(
                subscriptions,
                wakeups,
                result,
                timestamp,
                compute_dispatch=True,
            )

            if result.created > 0 or result.subscriptions_changed > 0:
                self._save_structured_unlocked(state, subscriptions, timers, wakeups)
        return {
            "status": "ok",
            "matched": result.matched,
            "created": result.created,
            "repo_id": repo_id,
            "run_id": run_id,
            "thread_id": thread_id,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
            "timestamp": timestamp,
        }

    def notify_timer_fired(
        self, timer: dict[str, Any]
    ) -> tuple[Optional[PmaAutomationWakeup], bool]:
        timer_id = _normalize_text(timer.get("timer_id"))
        if timer_id is None:
            return None, True

        fired_at = _normalize_text(timer.get("fired_at")) or _iso_now()

        timer_event = TimerFiredEvent(
            timer_id=timer_id,
            timer_type=_normalize_timer_type(timer.get("timer_type")),
            fired_at=fired_at,
            repo_id=_normalize_text(timer.get("repo_id")),
            run_id=_normalize_text(timer.get("run_id")),
            thread_id=_normalize_text(timer.get("thread_id")),
            lane_id=_normalize_lane_id(timer.get("lane_id")),
            from_state=_normalize_text(timer.get("from_state")),
            to_state=_normalize_text(timer.get("to_state")),
            reason=_normalize_text(timer.get("reason")),
            subscription_id=_normalize_text(timer.get("subscription_id")),
            metadata=(
                dict(timer["metadata"])
                if isinstance(timer.get("metadata"), dict)
                else {}
            ),
        )

        result = reduce_timer_fired(timer_event)
        intent = result.wakeup_intent

        return self.enqueue_wakeup(
            source=intent.source,
            repo_id=intent.repo_id,
            run_id=intent.run_id,
            thread_id=intent.thread_id,
            lane_id=intent.lane_id,
            from_state=intent.from_state,
            to_state=intent.to_state,
            reason=intent.reason,
            timestamp=intent.timestamp or fired_at,
            idempotency_key=intent.idempotency_key,
            subscription_id=intent.subscription_id,
            timer_id=timer_id,
            event_type=intent.event_type,
            metadata=intent.metadata,
        )

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
                domain_wakeup = self._store_wakeup_to_domain(entry)
                result = reduce_wakeup_dispatch(domain_wakeup, stamp)
                if result.dispatched and result.updated_wakeup is not None:
                    self._apply_domain_wakeup_to_store(entry, result.updated_wakeup)
                    changed = True
                break
            if changed:
                self._save_structured_unlocked(state, subscriptions, timers, wakeups)
            return changed

    def purge_wakeup(self, wakeup_id: str, *, require_inactive: bool = True) -> bool:
        target_id = _normalize_text(wakeup_id)
        if target_id is None:
            return False
        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
            retained: list[PmaAutomationWakeup] = []
            removed = False
            for entry in wakeups:
                if entry.wakeup_id != target_id:
                    retained.append(entry)
                    continue
                if require_inactive and entry.state == "pending":
                    retained.append(entry)
                    continue
                removed = True
            if removed:
                self._save_structured_unlocked(state, subscriptions, timers, retained)
            return removed


__all__ = [
    "PMA_AUTOMATION_STORE_FILENAME",
    "PMA_AUTOMATION_VERSION",
    "PmaAutomationStore",
    "PmaAutomationTimer",
    "PmaAutomationThreadNotFoundError",
    "PmaAutomationWakeup",
    "PmaLifecycleSubscription",
    "default_pma_automation_state",
]
