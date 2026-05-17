from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
from .pma_automation_domain_translation import PmaAutomationDomainTranslator
from .pma_automation_mirror import PmaAutomationMirror, PmaUnifiedMirrorResult
from .pma_automation_persistence import PmaAutomationPersistence
from .pma_automation_records import (
    PmaAutomationTimer,
    PmaAutomationWakeup,
    PmaLifecycleSubscription,
    _resolve_subscription_max_matches,
)
from .pma_automation_services import (
    MANAGED_THREAD_AUTO_SUBSCRIPTION_PREFIXES,
    PmaAutomationThreadNotFoundError,
    PmaSubscriptionCommandService,
    PmaWakeupDispatchDecisionService,
    normalize_delivery_target,
)
from .pma_automation_types import (
    DEFAULT_PMA_LANE_ID,
    DEFAULT_WATCHDOG_IDLE_SECONDS,
    PMA_AUTOMATION_STORE_FILENAME,
    PMA_AUTOMATION_VERSION,
    TIMER_TYPE_WATCHDOG,
    _iso_after_seconds,
    _iso_now,
    _normalize_bool,
    _normalize_due_timestamp,
    _normalize_lane_id,
    _normalize_non_negative_int,
    _normalize_positive_int,
    _normalize_text,
    _normalize_timer_type,
    _parse_iso,
    default_pma_automation_state,
)
from .pma_domain.automation_reducer import (
    reduce_dequeue_due_timers,
    reduce_timer_touch,
    reduce_wakeup_dispatch,
    reduce_wakeup_queued,
)
from .pma_domain.subscription_reducer import (
    ReduceTransitionResult,
    TimerFiredEvent,
    TransitionEvent,
    reduce_timer_fired,
    reduce_transition,
)

logger = logging.getLogger(__name__)


def _normalize_delivery_target(value: Any) -> Optional[dict[str, str]]:
    return normalize_delivery_target(value)


class PmaAutomationStore:
    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = hub_root
        self._durable = durable
        self._persistence = PmaAutomationPersistence(hub_root, durable=durable)
        self._mirror = PmaAutomationMirror(hub_root, durable=durable)
        self._subscriptions = PmaSubscriptionCommandService(hub_root, self._persistence)
        self._dispatch_decisions = PmaWakeupDispatchDecisionService(hub_root)

    @property
    def path(self) -> Path:
        return self._persistence.path

    def _lock_path(self) -> Path:
        return self._persistence._lock_path()

    def load(self) -> dict[str, Any]:
        return self._persistence.load()

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        return self._persistence._load_unlocked()

    def _load_structured_unlocked(
        self,
    ) -> tuple[
        dict[str, Any],
        list[PmaLifecycleSubscription],
        list[PmaAutomationTimer],
        list[PmaAutomationWakeup],
    ]:
        return self._persistence._load_structured_unlocked()

    def _normalize_subscriptions(self, value: Any) -> list[PmaLifecycleSubscription]:
        return self._persistence._normalize_subscriptions(value)

    def _normalize_timers(self, value: Any) -> list[PmaAutomationTimer]:
        return self._persistence._normalize_timers(value)

    def _normalize_wakeups(self, value: Any) -> list[PmaAutomationWakeup]:
        return self._persistence._normalize_wakeups(value)

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
        self._dispatch_decisions.enrich(wakeup)

    _lifecycle_sub_to_domain = staticmethod(
        PmaAutomationDomainTranslator._lifecycle_sub_to_domain
    )
    _store_timer_to_domain = staticmethod(
        PmaAutomationDomainTranslator._store_timer_to_domain
    )
    _store_wakeup_to_domain = staticmethod(
        PmaAutomationDomainTranslator._store_wakeup_to_domain
    )
    _apply_domain_timer_to_store = staticmethod(
        PmaAutomationDomainTranslator._apply_domain_timer_to_store
    )
    _apply_domain_wakeup_to_store = staticmethod(
        PmaAutomationDomainTranslator._apply_domain_wakeup_to_store
    )

    def _apply_reduce_result(
        self,
        subscriptions: list[PmaLifecycleSubscription],
        wakeups: list[PmaAutomationWakeup],
        result: ReduceTransitionResult,
        timestamp: str,
        *,
        compute_dispatch: bool = True,
    ) -> list[PmaAutomationWakeup]:
        return PmaAutomationDomainTranslator._apply_reduce_result(
            subscriptions,
            wakeups,
            result,
            timestamp,
            compute_dispatch=(
                self._compute_dispatch_decision_for_wakeup if compute_dispatch else None
            ),
        )

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
        origin_surface_kind: Optional[str] = None,
        origin_surface_key: Optional[str] = None,
    ) -> tuple[PmaLifecycleSubscription, bool]:
        key = _normalize_text(idempotency_key)
        normalized_event_types = self._subscriptions.normalize_event_types(event_types)
        normalized_thread_id = _normalize_text(thread_id)
        resolved_lane_id = self._subscriptions.resolve_lane_id(
            thread_id=normalized_thread_id,
            lane_id=lane_id,
            metadata=metadata,
            origin_thread_id=origin_thread_id,
            origin_lane_id=origin_lane_id,
        )
        resolved_metadata = self._subscriptions.resolve_metadata(
            thread_id=normalized_thread_id,
            metadata=metadata,
            origin_thread_id=origin_thread_id,
            origin_lane_id=origin_lane_id,
            origin_surface_kind=origin_surface_kind,
            origin_surface_key=origin_surface_key,
        )
        if not normalized_event_types:
            logger.warning(
                "Creating PMA subscription with empty event_types; subscription will match all events"
            )
        created: PmaLifecycleSubscription
        deduped = False
        with file_lock(self._lock_path()):
            with self._persistence.with_write_connection() as conn:
                with conn:
                    if key is not None:
                        existing = self._persistence.find_active_subscription_by_key(
                            conn, key
                        )
                        if existing is not None:
                            created = existing
                            deduped = True
                    if not deduped:
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
                        self._persistence.insert_subscription(conn, created)
        self._mirror_subscription_rule(created)
        return created, deduped

    def create_subscription(
        self, payload: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        data = self._coerce_payload(payload, kwargs)
        normalized_event_types = self._subscriptions.normalize_event_types(
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
        normalized_origin_surface_kind = _normalize_text(
            data.get("origin_surface_kind")
        )
        normalized_origin_surface_key = _normalize_text(data.get("origin_surface_key"))
        confirm_duplicate = _normalize_bool(data.get("confirm"), fallback=False)
        is_auto_subscription = self._subscriptions.is_auto_subscription_key(
            normalized_idempotency_key
        )
        if not confirm_duplicate:
            existing_auto = self._subscriptions.find_covering_auto_subscription(
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
            origin_surface_kind=normalized_origin_surface_kind,
            origin_surface_key=normalized_origin_surface_key,
        )
        result = {"subscription": created.to_dict(), "deduped": deduped}
        scope_warning = self._subscriptions.repo_scoped_warning(
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
            with self._persistence.with_write_connection() as conn:
                with conn:
                    changed = self._persistence.cancel_subscription(
                        conn, target_id, stamp
                    )
            return changed

    def _mirror_subscription_rule(
        self, subscription: PmaLifecycleSubscription
    ) -> PmaUnifiedMirrorResult:
        return self._mirror.mirror_subscription_rule(subscription)

    def purge_subscription(
        self, subscription_id: str, *, require_inactive: bool = True
    ) -> bool:
        target_id = _normalize_text(subscription_id)
        if target_id is None:
            return False
        with file_lock(self._lock_path()):
            with self._persistence.with_write_connection() as conn:
                with conn:
                    return self._persistence.purge_subscription(
                        conn, target_id, require_inactive=require_inactive
                    )

    def purge_subscriptions(
        self,
        *,
        state_filter: Optional[str] = None,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        target_state = _normalize_text(state_filter)
        with file_lock(self._lock_path()):
            with self._persistence.with_write_connection() as conn:
                with conn:
                    removed = self._persistence.purge_subscriptions(
                        conn,
                        state_filter=target_state,
                        dry_run=dry_run,
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
        created: PmaAutomationTimer
        deduped = False
        with file_lock(self._lock_path()):
            with self._persistence.with_write_connection() as conn:
                with conn:
                    normalized_subscription_id = _normalize_text(subscription_id)
                    if normalized_subscription_id is not None:
                        if not self._persistence.subscription_id_exists(
                            conn, normalized_subscription_id
                        ):
                            raise ValueError(
                                f"Unknown subscription_id: {normalized_subscription_id}"
                            )
                    if key is not None:
                        existing = self._persistence.find_pending_timer_by_key(
                            conn, key
                        )
                        if existing is not None:
                            created = existing
                            deduped = True
                    if not deduped:
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
                        self._persistence.insert_timer(conn, created)
        self._mirror_timer_schedule(created)
        return created, deduped

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
            with self._persistence.with_write_connection() as conn:
                with conn:
                    changed = self._persistence.cancel_timer(
                        conn,
                        target_id,
                        cancelled_at=cancelled_at,
                        reason=reason,
                    )
            return changed

    def purge_timer(self, timer_id: str, *, require_inactive: bool = True) -> bool:
        target_id = _normalize_text(timer_id)
        if target_id is None:
            return False
        with file_lock(self._lock_path()):
            with self._persistence.with_write_connection() as conn:
                with conn:
                    return self._persistence.purge_timer(
                        conn, target_id, require_inactive=require_inactive
                    )

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
            _, _, timers, _ = self._load_structured_unlocked()
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
                    with self._persistence.with_write_connection() as conn:
                        with conn:
                            self._persistence.update_timer(conn, entry)
                    self._mirror_timer_schedule(entry)
                    return {"status": "ok", "timer": entry.to_dict(), "touched": True}
        return {"status": "ok", "timer_id": target_id, "touched": False}

    def _mirror_timer_schedule(
        self, timer: PmaAutomationTimer
    ) -> PmaUnifiedMirrorResult:
        return self._mirror.mirror_timer_schedule(timer)

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
            _, _, timers, _ = self._load_structured_unlocked()
            domain_timers = [self._store_timer_to_domain(t) for t in timers]
            result = reduce_dequeue_due_timers(domain_timers, now_dt, limit=due_limit)
            for i, domain_timer in enumerate(result.updated_timers):
                if i < len(timers):
                    self._apply_domain_timer_to_store(timers[i], domain_timer)
            if result.fired_count > 0:
                with self._persistence.with_write_connection() as conn:
                    with conn:
                        for entry in timers:
                            self._persistence.update_timer(conn, entry)
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
            with self._persistence.with_write_connection() as conn:
                with conn:
                    if key is not None:
                        existing = self._persistence.find_wakeup_by_idempotency_key(
                            conn, key
                        )
                        if existing is not None:
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
                    self._persistence.insert_wakeup(conn, created)
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
        pending = [
            entry.to_dict() for entry in wakeups if entry.state in {"pending", "queued"}
        ]
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
            _, subscriptions, _, wakeups = self._load_structured_unlocked()

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
            existing_wakeup_ids = {existing.wakeup_id for existing in wakeups}
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
                with self._persistence.with_write_connection() as conn:
                    with conn:
                        for subscription in subscriptions:
                            self._persistence.update_subscription(conn, subscription)
                        for wakeup in wakeups:
                            if wakeup.wakeup_id not in existing_wakeup_ids:
                                self._persistence.insert_wakeup(conn, wakeup)
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

    def mark_wakeup_queued(
        self, wakeup_id: str, *, queued_at: Optional[str] = None
    ) -> bool:
        target_id = _normalize_text(wakeup_id)
        if target_id is None:
            return False
        stamp = _normalize_text(queued_at) or _iso_now()
        with file_lock(self._lock_path()):
            _, _, _, wakeups = self._load_structured_unlocked()
            changed = False
            for entry in wakeups:
                if entry.wakeup_id != target_id:
                    continue
                domain_wakeup = self._store_wakeup_to_domain(entry)
                result = reduce_wakeup_queued(domain_wakeup, stamp)
                if result.queued and result.updated_wakeup is not None:
                    self._apply_domain_wakeup_to_store(entry, result.updated_wakeup)
                    changed = True
                break
            if changed:
                with self._persistence.with_write_connection() as conn:
                    with conn:
                        self._persistence.update_wakeup(conn, entry)
            return changed

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
            _, _, _, wakeups = self._load_structured_unlocked()
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
                with self._persistence.with_write_connection() as conn:
                    with conn:
                        self._persistence.update_wakeup(conn, entry)
            return changed

    def purge_wakeup(self, wakeup_id: str, *, require_inactive: bool = True) -> bool:
        target_id = _normalize_text(wakeup_id)
        if target_id is None:
            return False
        with file_lock(self._lock_path()):
            with self._persistence.with_write_connection() as conn:
                with conn:
                    return self._persistence.purge_wakeup(
                        conn, target_id, require_inactive=require_inactive
                    )


__all__ = [
    "DEFAULT_PMA_LANE_ID",
    "MANAGED_THREAD_AUTO_SUBSCRIPTION_PREFIXES",
    "PMA_AUTOMATION_STORE_FILENAME",
    "PMA_AUTOMATION_VERSION",
    "PmaAutomationStore",
    "PmaAutomationTimer",
    "PmaAutomationThreadNotFoundError",
    "PmaAutomationWakeup",
    "PmaLifecycleSubscription",
    "default_pma_automation_state",
]
