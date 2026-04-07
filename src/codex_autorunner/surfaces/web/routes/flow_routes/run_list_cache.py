from __future__ import annotations

import copy
import inspect
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

from fastapi import Request
from fastapi.encoders import jsonable_encoder

if TYPE_CHECKING:
    from . import FlowRouteDependencies

_FLOW_RUN_LIST_CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class _FlowRunListCacheEntry:
    expires_at: float
    payload: list[dict[str, Any]]


def _supports_lite_response(builder: Any) -> bool:
    params = inspect.signature(builder).parameters.values()
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD or param.name == "lite"
        for param in params
    )


def _prune_flow_run_list_payload(payload: list[dict[str, Any]]) -> None:
    for row in payload:
        state = row.get("state")
        if not isinstance(state, dict):
            continue
        ticket_engine = state.get("ticket_engine")
        current_ticket = (
            ticket_engine.get("current_ticket")
            if isinstance(ticket_engine, dict)
            else state.get("current_ticket")
        )
        row["state"] = {"current_ticket": current_ticket, "status": row.get("status")}


def _list_runs_cache_handles(
    request: Request,
) -> tuple[dict[tuple[str, str], _FlowRunListCacheEntry], threading.Lock]:
    app_state = request.app.state
    if not hasattr(app_state, "flow_run_list_cache"):
        app_state.flow_run_list_cache = {}
    if not hasattr(app_state, "flow_run_list_cache_lock"):
        app_state.flow_run_list_cache_lock = threading.Lock()
    return (
        cast(
            dict[tuple[str, str], _FlowRunListCacheEntry], app_state.flow_run_list_cache
        ),
        cast(threading.Lock, app_state.flow_run_list_cache_lock),
    )


def _build_flow_run_list_row(
    deps: "FlowRouteDependencies", record: Any, repo_root: Path, *, store: Any
) -> Any:
    if _supports_lite_response(deps.build_flow_status_response):
        return deps.build_flow_status_response(
            record, repo_root, store=store, lite=True
        )
    return deps.build_flow_status_response(record, repo_root, store=store)


def _build_flow_run_list_payload(
    deps: "FlowRouteDependencies",
    records: list[Any],
    repo_root: Path,
    *,
    store: Any,
) -> list[dict[str, Any]]:
    payload = jsonable_encoder(
        [
            _build_flow_run_list_row(deps, record, repo_root, store=store)
            for record in records
        ]
    )
    if isinstance(payload, list):
        _prune_flow_run_list_payload(payload)
        return cast(list[dict[str, Any]], payload)
    return []


def _load_cached_flow_run_list(
    request: Request,
    cache_key: tuple[str, str],
) -> Optional[list[dict[str, Any]]]:
    cache, cache_lock = _list_runs_cache_handles(request)
    now = time.monotonic()
    with cache_lock:
        cached = cache.get(cache_key)
        if cached is None or cached.expires_at <= now:
            return None
        return copy.deepcopy(cached.payload)


def _store_cached_flow_run_list(
    request: Request,
    cache_key: tuple[str, str],
    payload: list[dict[str, Any]],
) -> None:
    cache, cache_lock = _list_runs_cache_handles(request)
    with cache_lock:
        cache[cache_key] = _FlowRunListCacheEntry(
            expires_at=time.monotonic() + _FLOW_RUN_LIST_CACHE_TTL_SECONDS,
            payload=copy.deepcopy(payload),
        )


def list_flow_runs_payload(
    request: Request,
    deps: "FlowRouteDependencies",
    load_flow_run_records: Any,
    *,
    flow_type: Optional[str],
    flow_target_id: Optional[str],
    reconcile: bool,
) -> list[dict[str, Any]]:
    repo_root = deps.find_repo_root()
    if repo_root is None:
        return []
    cache_key = (flow_type or "", flow_target_id or "")
    if not reconcile:
        cached = _load_cached_flow_run_list(request, cache_key)
        if cached is not None:
            return cached
    store = deps.require_flow_store(repo_root)
    try:
        records = load_flow_run_records(
            repo_root,
            flow_type=flow_type,
            flow_target_id=flow_target_id,
            reconcile=reconcile,
            store=store,
            safe_list_flow_runs=deps.safe_list_flow_runs,
            build_flow_orchestration_service_fn=deps.build_flow_orchestration_service,
        )
        payload = _build_flow_run_list_payload(deps, records, repo_root, store=store)
    finally:
        if store:
            store.close()
    if not reconcile:
        _store_cached_flow_run_list(request, cache_key, payload)
    return payload
