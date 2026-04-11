"""
Hub-level PMA routes (chat + models + events).
"""

from __future__ import annotations

import asyncio  # noqa: F401

from fastapi import APIRouter, Depends, HTTPException, Request

from ....bootstrap import ensure_pma_docs
from ....core.pma_context import build_hub_snapshot
from ....core.utils import atomic_write
from ....integrations.github.context_injection import maybe_inject_github_context
from ..services.pma.common import pma_config_from_raw
from .pma_routes import (
    PmaRuntimeState,
    build_automation_routes,
    build_chat_runtime_router,
    build_history_files_docs_router,
    build_managed_thread_crud_routes,
    build_managed_thread_runtime_routes,
    build_managed_thread_tail_routes,
    build_pma_meta_routes,
)
from .pma_routes import chat_runtime as chat_runtime_routes
from .pma_routes import history_files_docs as history_files_docs_routes
from .pma_routes.chat_runtime import (
    _ensure_lane_worker_for_app,
    _stop_all_lane_workers_for_app,
    _stop_lane_worker_for_app,
)
from .trace_inspection import build_trace_inspection_routes


def _require_pma_enabled(request: Request) -> None:
    raw = getattr(request.app.state.config, "raw", {})
    if not pma_config_from_raw(raw).get("enabled", True):
        raise HTTPException(status_code=404, detail="PMA is disabled")


def build_pma_routes() -> APIRouter:
    runtime_state = PmaRuntimeState()
    router = APIRouter(prefix="/hub/pma", dependencies=[Depends(_require_pma_enabled)])

    def _get_runtime_state() -> PmaRuntimeState:
        return runtime_state

    # Preserve the legacy module-level monkeypatch seams used by PMA route tests.
    chat_runtime_routes.build_hub_snapshot = build_hub_snapshot
    chat_runtime_routes.maybe_inject_github_context = maybe_inject_github_context
    history_files_docs_routes.ensure_pma_docs = ensure_pma_docs
    history_files_docs_routes.atomic_write = atomic_write

    build_automation_routes(router, _get_runtime_state)
    build_managed_thread_crud_routes(router, _get_runtime_state)
    build_managed_thread_tail_routes(router, _get_runtime_state)
    build_managed_thread_runtime_routes(router, _get_runtime_state)
    build_history_files_docs_router(router, _get_runtime_state)
    build_chat_runtime_router(router, _get_runtime_state)
    build_pma_meta_routes(router, _get_runtime_state)
    build_trace_inspection_routes(router)

    async def _start_lane_worker(app, lane_id: str) -> None:
        await _ensure_lane_worker_for_app(runtime_state, app, lane_id)

    async def _stop_lane_worker(app, lane_id: str) -> None:
        await _stop_lane_worker_for_app(runtime_state, app, lane_id)

    async def _stop_all_lane_workers(app) -> None:
        await _stop_all_lane_workers_for_app(runtime_state, app)

    router._pma_start_lane_worker = _start_lane_worker  # type: ignore[attr-defined]
    router._pma_stop_lane_worker = _stop_lane_worker  # type: ignore[attr-defined]
    router._pma_stop_all_lane_workers = _stop_all_lane_workers  # type: ignore[attr-defined]
    return router


__all__ = ["build_pma_routes"]
