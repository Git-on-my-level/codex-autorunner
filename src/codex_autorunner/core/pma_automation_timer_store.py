from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from .locks import file_lock
from .pma_automation_records import PmaAutomationTimer
from .pma_automation_store_context import PmaAutomationStoreContextMixin
from .pma_automation_types import (
    DEFAULT_WATCHDOG_IDLE_SECONDS,
    TIMER_TYPE_WATCHDOG,
    _iso_after_seconds,
    _iso_now,
    _normalize_due_timestamp,
    _normalize_non_negative_int,
    _normalize_text,
    _normalize_timer_type,
    _parse_iso,
)
from .pma_domain.automation_reducer import (
    reduce_dequeue_due_timers,
    reduce_timer_touch,
)


class PmaAutomationTimerStoreMixin(PmaAutomationStoreContextMixin):
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


__all__ = ["PmaAutomationTimerStoreMixin"]
