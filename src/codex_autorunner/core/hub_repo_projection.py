from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional, cast

from .hub_projection_store import (
    REPO_RUNTIME_PROJECTION_NAMESPACE,
    path_stat_fingerprint,
)

_REPO_ENRICH_CACHE_TTL_SECONDS = 45.0
_REPO_RUNTIME_PROJECTION_MAX_AGE_SECONDS = 300.0


@dataclass(frozen=True)
class _RepoEnrichmentCacheEntry:
    fingerprint: tuple[Any, ...]
    expires_at: float
    payload: dict[str, Any]


class HubRepoProjectionService:
    def __init__(
        self,
        context: Any,
        *,
        repo_payload_decorator: Optional[
            Callable[[dict[str, Any]], dict[str, Any]]
        ] = None,
    ) -> None:
        self._context = context
        self._repo_payload_decorator = repo_payload_decorator
        self._unbound_thread_counts_cache: Optional[dict[str, int]] = None
        self._unbound_thread_counts_cached_at = 0.0
        self._unbound_thread_counts_lock = threading.Lock()
        self._repo_state_cache: dict[str, _RepoEnrichmentCacheEntry] = {}
        self._repo_state_cache_lock = threading.Lock()

    def repo_state_fingerprint(
        self,
        snapshot: Any,
        *,
        stale_threshold_seconds: Optional[int],
    ) -> tuple[Any, ...]:
        return self._repo_state_fingerprint(
            snapshot,
            stale_threshold_seconds=stale_threshold_seconds,
        )

    def invalidate_runtime_caches(self) -> None:
        with self._unbound_thread_counts_lock:
            self._unbound_thread_counts_cache = None
            self._unbound_thread_counts_cached_at = 0.0
        with self._repo_state_cache_lock:
            self._repo_state_cache.clear()
        projection_store = getattr(self._context, "projection_store", None)
        if projection_store is not None:
            try:
                projection_store.delete(namespace=REPO_RUNTIME_PROJECTION_NAMESPACE)
            except Exception:
                pass

    def _unbound_repo_thread_counts(self) -> dict[str, int]:
        with self._unbound_thread_counts_lock:
            now = time.monotonic()
            if (
                self._unbound_thread_counts_cache is not None
                and now - self._unbound_thread_counts_cached_at < 1.0
            ):
                return dict(self._unbound_thread_counts_cache)
            try:
                counts = self._context.supervisor.unbound_repo_thread_counts()
            except Exception:
                counts = {}
            self._unbound_thread_counts_cache = dict(counts)
            self._unbound_thread_counts_cached_at = now
            return dict(counts)

    def unbound_repo_thread_counts_snapshot(self) -> dict[str, int]:
        return self._unbound_repo_thread_counts()

    def _repo_state_projection_key(self, snapshot: Any) -> str:
        return f"{snapshot.id}:{snapshot.path}"

    def _repo_state_fingerprint(
        self,
        snapshot: Any,
        *,
        stale_threshold_seconds: Optional[int],
    ) -> tuple[Any, ...]:
        repo_root = snapshot.path
        car_root = repo_root / ".codex-autorunner"
        return (
            str(snapshot.id),
            str(repo_root),
            bool(snapshot.exists_on_disk),
            bool(snapshot.initialized),
            snapshot.last_run_id,
            snapshot.last_run_started_at,
            snapshot.last_run_finished_at,
            int(stale_threshold_seconds or 0),
            path_stat_fingerprint(car_root),
            path_stat_fingerprint(car_root / "tickets"),
            path_stat_fingerprint(car_root / "runs"),
        )

    def _compute_repo_state_payload(
        self,
        snapshot: Any,
        *,
        stale_threshold_seconds: Optional[int],
    ) -> dict[str, Any]:
        from .archive import has_car_state
        from .flows.models import flow_run_duration_seconds
        from .flows.store import FlowStore
        from .pma_context import get_latest_ticket_flow_run_state_with_record
        from .ticket_flow_projection import build_canonical_state_v1
        from .ticket_flow_summary import build_ticket_flow_summary

        payload: dict[str, Any] = {
            "has_car_state": (
                has_car_state(self._context.config.root / snapshot.path)
                if snapshot.exists_on_disk
                else False
            ),
            "ticket_flow": None,
            "ticket_flow_display": None,
            "run_state": None,
            "canonical_state_v1": None,
        }
        if not (snapshot.initialized and snapshot.exists_on_disk):
            return payload

        db_path = snapshot.path / ".codex-autorunner" / "flows.db"
        store: Optional[FlowStore] = None
        if db_path.exists():
            try:
                store = FlowStore.connect_readonly(db_path)
                store.initialize()
            except Exception:
                store = None

        try:
            ticket_flow = build_ticket_flow_summary(
                snapshot.path,
                include_failure=True,
                store=store,
            )
            payload["ticket_flow"] = ticket_flow
            if isinstance(ticket_flow, dict):
                payload["ticket_flow_display"] = {
                    "status": ticket_flow.get("status"),
                    "status_label": ticket_flow.get("status_label"),
                    "status_icon": ticket_flow.get("status_icon"),
                    "is_active": ticket_flow.get("is_active"),
                    "done_count": ticket_flow.get("done_count"),
                    "total_count": ticket_flow.get("total_count"),
                    "run_id": ticket_flow.get("run_id"),
                }
            else:
                payload["ticket_flow_display"] = {
                    "status": None,
                    "status_label": "Idle",
                    "status_icon": "⚪",
                    "is_active": False,
                    "done_count": 0,
                    "total_count": 0,
                    "run_id": None,
                }
            run_state, run_record = get_latest_ticket_flow_run_state_with_record(
                snapshot.path,
                snapshot.id,
                store=store,
            )
            payload["run_state"] = run_state
            if run_record is not None:
                if str(snapshot.last_run_id) != str(run_record.id):
                    payload["last_exit_code"] = None
                payload["last_run_id"] = run_record.id
                payload["last_run_started_at"] = run_record.started_at
                payload["last_run_finished_at"] = run_record.finished_at
                payload["last_run_duration_seconds"] = flow_run_duration_seconds(
                    run_record
                )
            payload["canonical_state_v1"] = build_canonical_state_v1(
                repo_root=snapshot.path,
                repo_id=snapshot.id,
                run_state=payload["run_state"],
                record=run_record,
                store=store,
                preferred_run_id=(
                    str(snapshot.last_run_id)
                    if snapshot.last_run_id is not None
                    else None
                ),
                stale_threshold_seconds=stale_threshold_seconds,
            )
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
        return payload

    def _repo_state_payload(
        self,
        snapshot: Any,
        *,
        stale_threshold_seconds: Optional[int],
    ) -> dict[str, Any]:
        now = time.monotonic()
        fingerprint = self._repo_state_fingerprint(
            snapshot,
            stale_threshold_seconds=stale_threshold_seconds,
        )
        cache_key = self._repo_state_projection_key(snapshot)
        projection_store = getattr(self._context, "projection_store", None)
        with self._repo_state_cache_lock:
            cached = self._repo_state_cache.get(cache_key)
            if (
                cached is not None
                and cached.expires_at > now
                and cached.fingerprint == fingerprint
            ):
                return copy.deepcopy(cached.payload)
        if projection_store is not None:
            try:
                cached_payload = projection_store.get_cache(
                    cache_key,
                    fingerprint,
                    max_age_seconds=_REPO_RUNTIME_PROJECTION_MAX_AGE_SECONDS,
                    namespace=REPO_RUNTIME_PROJECTION_NAMESPACE,
                )
            except Exception:
                cached_payload = None
            if cached_payload is not None:
                with self._repo_state_cache_lock:
                    self._repo_state_cache[cache_key] = _RepoEnrichmentCacheEntry(
                        fingerprint=fingerprint,
                        expires_at=now + _REPO_ENRICH_CACHE_TTL_SECONDS,
                        payload=copy.deepcopy(cached_payload),
                    )
                return copy.deepcopy(cached_payload)
        payload = self._compute_repo_state_payload(
            snapshot,
            stale_threshold_seconds=stale_threshold_seconds,
        )
        with self._repo_state_cache_lock:
            self._repo_state_cache[cache_key] = _RepoEnrichmentCacheEntry(
                fingerprint=fingerprint,
                expires_at=now + _REPO_ENRICH_CACHE_TTL_SECONDS,
                payload=copy.deepcopy(payload),
            )
        if projection_store is not None:
            try:
                projection_store.set_cache(
                    cache_key,
                    fingerprint,
                    payload,
                    namespace=REPO_RUNTIME_PROJECTION_NAMESPACE,
                )
            except Exception:
                pass
        return payload

    def enrich_repo(
        self,
        snapshot: Any,
        chat_binding_counts: Optional[dict[str, int]] = None,
        chat_binding_counts_by_source: Optional[dict[str, dict[str, int]]] = None,
        unbound_thread_counts: Optional[dict[str, int]] = None,
    ) -> dict[str, Any]:
        from .freshness import resolve_stale_threshold_seconds

        repo_dict = cast(dict[str, Any], snapshot.to_dict(self._context.config.root))
        if self._repo_payload_decorator is not None:
            repo_dict = self._repo_payload_decorator(repo_dict)
        binding_count = int((chat_binding_counts or {}).get(snapshot.id, 0))
        stale_threshold_seconds = resolve_stale_threshold_seconds(
            getattr(
                self._context.config.pma,
                "freshness_stale_threshold_seconds",
                None,
            )
        )
        source_counts = dict((chat_binding_counts_by_source or {}).get(snapshot.id, {}))
        pma_binding_count = int(source_counts.get("pma", 0))
        discord_binding_count = int(source_counts.get("discord", 0))
        telegram_binding_count = int(source_counts.get("telegram", 0))
        non_pma_binding_count = max(0, binding_count - pma_binding_count)
        repo_dict["chat_bound"] = binding_count > 0
        repo_dict["chat_bound_thread_count"] = binding_count
        repo_dict["pma_chat_bound_thread_count"] = pma_binding_count
        repo_dict["discord_chat_bound_thread_count"] = discord_binding_count
        repo_dict["telegram_chat_bound_thread_count"] = telegram_binding_count
        repo_dict["non_pma_chat_bound_thread_count"] = non_pma_binding_count
        repo_dict["cleanup_blocked_by_chat_binding"] = non_pma_binding_count > 0
        unbound_thread_count = 0
        if snapshot.kind == "base":
            counts = (
                dict(unbound_thread_counts)
                if unbound_thread_counts is not None
                else self._unbound_repo_thread_counts()
            )
            unbound_thread_count = int(counts.get(snapshot.id, 0))
        repo_dict["unbound_managed_thread_count"] = max(0, unbound_thread_count)
        repo_dict.update(
            self._repo_state_payload(
                snapshot,
                stale_threshold_seconds=stale_threshold_seconds,
            )
        )
        repo_dict.setdefault("last_run_duration_seconds", None)
        return repo_dict


__all__ = ["HubRepoProjectionService"]
