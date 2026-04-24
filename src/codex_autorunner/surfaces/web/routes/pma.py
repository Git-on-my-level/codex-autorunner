"""
Hub-level PMA routes (chat + models + events).
"""

from __future__ import annotations

import asyncio  # noqa: F401
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request

from ..services.pma import (
    PmaApplicationContainer,
    create_pma_application_container,
    get_pma_request_context,
)
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
from .pma_routes.chat_runtime import (
    _ensure_lane_worker_for_app,
    _stop_all_lane_workers_for_app,
    _stop_lane_worker_for_app,
)
from .trace_inspection import build_trace_inspection_routes


def _require_pma_enabled(request: Request) -> None:
    context = get_pma_request_context(request)
    if not context.pma_config.get("enabled", True):
        raise HTTPException(status_code=404, detail="PMA is disabled")


def build_pma_routes(*, container: PmaApplicationContainer | None = None) -> APIRouter:
    pma_container = container or create_pma_application_container(
        runtime_state=PmaRuntimeState()
    )
    runtime_state = cast(PmaRuntimeState, pma_container.runtime_state)
    router = APIRouter(prefix="/hub/pma", dependencies=[Depends(_require_pma_enabled)])

    def _get_runtime_state() -> PmaRuntimeState:
        return runtime_state

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

    router._pma_container = pma_container  # type: ignore[attr-defined]
    router._pma_start_lane_worker = _start_lane_worker  # type: ignore[attr-defined]
    router._pma_stop_lane_worker = _stop_lane_worker  # type: ignore[attr-defined]
    router._pma_stop_all_lane_workers = _stop_all_lane_workers  # type: ignore[attr-defined]
    return router


__all__ = ["build_pma_routes"]
