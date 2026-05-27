from __future__ import annotations

from typing import Any, Optional

from .locks import file_lock
from .pma_automation_records import PmaAutomationWakeup
from .pma_automation_store_context import PmaAutomationStoreContextMixin
from .pma_automation_types import (
    _iso_now,
    _normalize_lane_id,
    _normalize_text,
    _normalize_timer_type,
)
from .pma_domain.automation_reducer import (
    reduce_wakeup_dispatch,
    reduce_wakeup_queued,
)
from .pma_domain.subscription_reducer import (
    TimerFiredEvent,
    TransitionEvent,
    reduce_timer_fired,
    reduce_transition,
)


class PmaAutomationWakeupStoreMixin(PmaAutomationStoreContextMixin):
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


__all__ = ["PmaAutomationWakeupStoreMixin"]
