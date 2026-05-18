from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .chat_bindings import (
    active_chat_binding_metadata_by_thread,
    preferred_non_pma_chat_notification_source_for_workspace,
)
from .config import load_hub_config
from .config_contract import ConfigError
from .managed_thread_store import ManagedThreadStore
from .pma_automation_persistence import PmaAutomationPersistence
from .pma_automation_records import PmaAutomationWakeup, PmaLifecycleSubscription
from .pma_automation_types import (
    DEFAULT_PMA_LANE_ID,
    _normalize_lane_id,
    _normalize_text,
)
from .pma_dispatch_decision import (
    build_pma_dispatch_decision,
    pma_dispatch_decision_to_dict,
)
from .pma_origin import extract_pma_origin_metadata, merge_pma_origin_metadata
from .text_utils import _normalize_pma_delivery_target

MANAGED_THREAD_AUTO_SUBSCRIPTION_PREFIXES = (
    "managed-thread-notify:",
    "managed-thread-send-notify:",
)


class PmaAutomationThreadNotFoundError(ValueError):
    def __init__(self, thread_id: str) -> None:
        super().__init__(f"Unknown thread_id: {thread_id}")
        self.thread_id = thread_id


def normalize_delivery_target(value: Any) -> Optional[dict[str, str]]:
    normalized = _normalize_pma_delivery_target(value)
    if normalized is None:
        return None
    surface_kind, surface_key = normalized
    return {
        "surface_kind": surface_kind,
        "surface_key": surface_key,
    }


class PmaSubscriptionCommandService:
    def __init__(self, hub_root: Path, persistence: PmaAutomationPersistence) -> None:
        self._hub_root = hub_root
        self._persistence = persistence

    @staticmethod
    def normalize_event_types(value: Any, *, singular: Any = None) -> list[str]:
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

    def repo_scoped_warning(
        self, *, repo_id: Optional[str], thread_id: Optional[str]
    ) -> Optional[str]:
        normalized_repo_id = _normalize_text(repo_id)
        normalized_thread_id = _normalize_text(thread_id)
        if normalized_repo_id is None or normalized_thread_id is not None:
            return None
        thread_count = (
            ManagedThreadStore(self._hub_root)
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

    def resolve_thread_lane_id(self, *, thread_id: str) -> str:
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

    def resolve_thread_delivery_target(
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
        return normalize_delivery_target(
            {
                "surface_kind": binding_metadata.get("binding_kind"),
                "surface_key": binding_metadata.get("binding_id"),
            }
        )

    def resolve_lane_id(
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
                resolved_origin_lane_id = self.resolve_thread_lane_id(
                    thread_id=normalized_origin_thread_id
                )
            except PmaAutomationThreadNotFoundError:
                resolved_origin_lane_id = DEFAULT_PMA_LANE_ID
            if resolved_origin_lane_id != DEFAULT_PMA_LANE_ID:
                return resolved_origin_lane_id

        if normalized_thread_id is not None:
            resolved_lane_id = self.resolve_thread_lane_id(
                thread_id=normalized_thread_id
            )
            if resolved_lane_id != DEFAULT_PMA_LANE_ID:
                return resolved_lane_id
        return DEFAULT_PMA_LANE_ID

    @staticmethod
    def is_auto_subscription_key(idempotency_key: Optional[str]) -> bool:
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
    def _event_types_are_covered(*, requested: list[str], existing: list[str]) -> bool:
        if not existing:
            return True
        if not requested:
            return False
        existing_set = set(existing)
        return all(event_type in existing_set for event_type in requested)

    def find_covering_auto_subscription(
        self,
        *,
        event_types: list[str],
        repo_id: Optional[str],
        run_id: Optional[str],
        thread_id: Optional[str],
        from_state: Optional[str],
        to_state: Optional[str],
    ) -> Optional[PmaLifecycleSubscription]:
        state = self._persistence.load()
        subscriptions = self._persistence._normalize_subscriptions(
            state.get("subscriptions")
        )
        for entry in subscriptions:
            if entry.state != "active":
                continue
            if not self.is_auto_subscription_key(entry.idempotency_key):
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

    def resolve_metadata(
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
        delivery_target = normalize_delivery_target(
            resolved_metadata.get("delivery_target")
        )
        normalized_thread_id = _normalize_text(thread_id)
        origin_metadata = extract_pma_origin_metadata(resolved_metadata)
        normalized_origin_thread_id = _normalize_text(origin_thread_id) or (
            origin_metadata.thread_id if origin_metadata else None
        )
        if delivery_target is None and normalized_origin_thread_id is not None:
            try:
                delivery_target = self.resolve_thread_delivery_target(
                    thread_id=normalized_origin_thread_id
                )
            except PmaAutomationThreadNotFoundError:
                delivery_target = None
        if delivery_target is None and normalized_thread_id is not None:
            try:
                delivery_target = self.resolve_thread_delivery_target(
                    thread_id=normalized_thread_id
                )
            except PmaAutomationThreadNotFoundError:
                delivery_target = None
        if delivery_target is not None:
            resolved_metadata["delivery_target"] = delivery_target
        else:
            resolved_metadata.pop("delivery_target", None)
        return resolved_metadata


class PmaWakeupDispatchDecisionService:
    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root

    def enrich(self, wakeup: PmaAutomationWakeup) -> None:
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


__all__ = [
    "PmaAutomationThreadNotFoundError",
    "PmaSubscriptionCommandService",
    "PmaWakeupDispatchDecisionService",
    "normalize_delivery_target",
]
