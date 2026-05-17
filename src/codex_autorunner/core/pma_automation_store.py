from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .chat_bindings import (
    active_chat_binding_metadata_by_thread,
    preferred_non_pma_chat_notification_source_for_workspace,
)
from .config import load_hub_config
from .config_contract import ConfigError
from .locks import file_lock
from .managed_thread_store import ManagedThreadStore
from .orchestration.sqlite import open_orchestration_sqlite
from .pma_automation_domain_translation import PmaAutomationDomainTranslator
from .pma_automation_mirror import PmaAutomationMirror
from .pma_automation_persistence import PmaAutomationPersistence
from .pma_automation_records import (
    PmaAutomationTimer,
    PmaAutomationWakeup,
    PmaLifecycleSubscription,
    _resolve_subscription_max_matches,
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
from .pma_dispatch_decision import (
    build_pma_dispatch_decision,
    pma_dispatch_decision_to_dict,
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
from .pma_origin import extract_pma_origin_metadata, merge_pma_origin_metadata
from .text_utils import _normalize_pma_delivery_target

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
        ManagedThreadStore(hub_root)
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


class PmaAutomationStore:
    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = hub_root
        self._durable = durable
        self._persistence = PmaAutomationPersistence(hub_root, durable=durable)
        self._mirror = PmaAutomationMirror(hub_root, durable=durable)

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

    def _save_structured_unlocked(
        self,
        state: dict[str, Any],
        subscriptions: list[PmaLifecycleSubscription],
        timers: list[PmaAutomationTimer],
        wakeups: list[PmaAutomationWakeup],
    ) -> None:
        self._persistence._save_structured_unlocked(
            state, subscriptions, timers, wakeups
        )

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
                thread_store = ManagedThreadStore(self._hub_root)
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
        thread_store = ManagedThreadStore(self._hub_root)
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
        thread_store = ManagedThreadStore(self._hub_root)
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
        origin_surface_kind: Optional[str] = None,
        origin_surface_key: Optional[str] = None,
    ) -> dict[str, Any]:
        resolved_metadata = merge_pma_origin_metadata(
            metadata,
            origin_thread_id=origin_thread_id,
            origin_lane_id=origin_lane_id,
            origin_surface_kind=origin_surface_kind,
            origin_surface_key=origin_surface_key,
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
        origin_surface_kind: Optional[str] = None,
        origin_surface_key: Optional[str] = None,
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
            with open_orchestration_sqlite(
                self._hub_root, durable=self._durable
            ) as conn:
                with conn:
                    if key is not None:
                        existing = self._persistence._find_active_subscription_by_key(
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
                        self._persistence._insert_subscription_row(conn, created)
        self._mirror_subscription_rule(created)
        return created, deduped

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
        normalized_origin_surface_kind = _normalize_text(
            data.get("origin_surface_kind")
        )
        normalized_origin_surface_key = _normalize_text(data.get("origin_surface_key"))
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
            origin_surface_kind=normalized_origin_surface_kind,
            origin_surface_key=normalized_origin_surface_key,
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
                    if changed:
                        conn.execute(
                            """
                            UPDATE orch_automation_rules
                               SET enabled = 0,
                                   updated_at = ?
                             WHERE rule_id = ?
                            """,
                            (stamp, f"builtin:pma:subscription:{target_id}"),
                        )
            return changed

    def _mirror_subscription_rule(self, subscription: PmaLifecycleSubscription) -> None:
        self._mirror.mirror_subscription_rule(subscription)

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
                removed = [self._persistence._row_to_subscription(row) for row in rows]
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
        created: PmaAutomationTimer
        deduped = False
        with file_lock(self._lock_path()):
            with open_orchestration_sqlite(
                self._hub_root, durable=self._durable
            ) as conn:
                with conn:
                    normalized_subscription_id = _normalize_text(subscription_id)
                    if normalized_subscription_id is not None:
                        if not self._persistence._subscription_id_exists(
                            conn, normalized_subscription_id
                        ):
                            raise ValueError(
                                f"Unknown subscription_id: {normalized_subscription_id}"
                            )
                    if key is not None:
                        existing = self._persistence._find_pending_timer_by_key(
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
                        self._persistence._insert_timer_row(conn, created)
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
                    if changed:
                        conn.execute(
                            """
                            UPDATE orch_automation_schedules
                               SET state = 'cancelled',
                                   next_fire_at = NULL,
                                   updated_at = ?
                             WHERE schedule_id = ?
                            """,
                            (cancelled_at, f"pma-timer:{target_id}"),
                        )
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
                    self._mirror_timer_schedule(entry)
                    return {"status": "ok", "timer": entry.to_dict(), "touched": True}
        return {"status": "ok", "timer_id": target_id, "touched": False}

    def _mirror_timer_schedule(self, timer: PmaAutomationTimer) -> None:
        self._mirror.mirror_timer_schedule(timer)

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

    def mark_wakeup_queued(
        self, wakeup_id: str, *, queued_at: Optional[str] = None
    ) -> bool:
        target_id = _normalize_text(wakeup_id)
        if target_id is None:
            return False
        stamp = _normalize_text(queued_at) or _iso_now()
        with file_lock(self._lock_path()):
            state, subscriptions, timers, wakeups = self._load_structured_unlocked()
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
                self._save_structured_unlocked(state, subscriptions, timers, wakeups)
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
                if require_inactive and entry.state in {"pending", "queued"}:
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
