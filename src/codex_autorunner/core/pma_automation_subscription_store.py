from __future__ import annotations

import logging
from typing import Any, Optional

from .locks import file_lock
from .pma_automation_records import (
    PmaLifecycleSubscription,
    _resolve_subscription_max_matches,
)
from .pma_automation_store_context import PmaAutomationStoreContextMixin
from .pma_automation_types import (
    _iso_now,
    _normalize_bool,
    _normalize_positive_int,
    _normalize_text,
)

logger = logging.getLogger(__name__)


class PmaAutomationSubscriptionStoreMixin(PmaAutomationStoreContextMixin):
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


__all__ = ["PmaAutomationSubscriptionStoreMixin"]
