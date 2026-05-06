from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .....agents.registry import get_available_agents
from .....core.orchestration.catalog import map_agent_capabilities
from .....integrations.chat.surface_action_manifest import (
    SurfaceActionManifestContext,
    build_surface_action_manifest,
)
from ...services.pma import get_pma_request_context


def _thread_capabilities(request: Request, thread: dict[str, Any]) -> frozenset[str]:
    agent = str(thread.get("agent") or thread.get("agent_id") or "").strip().lower()
    if not agent:
        return frozenset()
    descriptor = get_available_agents(request.app.state).get(agent)
    if descriptor is None:
        return frozenset()
    return frozenset(map_agent_capabilities(descriptor.capabilities))


def build_action_manifest_routes(router: APIRouter) -> None:
    @router.get("/threads/{managed_thread_id}/action-manifest")
    async def get_managed_thread_action_manifest(
        request: Request, managed_thread_id: str, ui_kind: str = "pma_web"
    ) -> dict[str, Any]:
        store = get_pma_request_context(request).thread_store()
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        running_turn = store.get_running_turn(managed_thread_id)
        lifecycle_state = "running" if running_turn is not None else "idle"
        manifest = build_surface_action_manifest(
            SurfaceActionManifestContext(
                surface_kind="web",
                ui_kind="pma_web" if ui_kind == "pma_web" else "generic",
                target_kind="managed_thread",
                workspace_id=str(thread.get("resource_id") or "") or None,
                thread_id=managed_thread_id,
                resource_kind=str(thread.get("resource_kind") or "") or None,
                resource_id=str(thread.get("resource_id") or "") or None,
                lifecycle_state=lifecycle_state,
                capabilities=_thread_capabilities(request, thread),
            )
        )
        return manifest.to_dict()


__all__ = ["build_action_manifest_routes"]
