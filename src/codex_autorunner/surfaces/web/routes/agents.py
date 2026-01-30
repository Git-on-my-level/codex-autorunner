"""
Agent harness support routes (models + event streaming).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ....agents.codex.harness import CodexHarness
from ....agents.opencode.harness import OpenCodeHarness
from ....agents.opencode.supervisor import OpenCodeSupervisorError
from ....agents.types import ModelCatalog
from .shared import SSE_HEADERS


def _available_agents(request: Request) -> tuple[list[dict[str, str]], str]:
    agents: list[dict[str, str]] = []
    default_agent: Optional[str] = None

    if getattr(request.app.state, "app_server_supervisor", None) is not None:
        agents.append({"id": "codex", "name": "Codex", "protocol_version": "2.0"})
        default_agent = "codex"

    if getattr(request.app.state, "opencode_supervisor", None) is not None:
        supervisor = getattr(request.app.state, "opencode_supervisor", None)
        version = None
        if supervisor and hasattr(supervisor, "_handles"):
            handles = supervisor._handles
            if handles:
                first_handle = next(iter(handles.values()), None)
                if first_handle:
                    version = getattr(first_handle, "version", None)
        agent_data = {"id": "opencode", "name": "OpenCode"}
        if version:
            agent_data["version"] = str(version)
        agents.append(agent_data)
        if default_agent is None:
            default_agent = "opencode"

    if not agents:
        agents = [{"id": "codex", "name": "Codex", "protocol_version": "2.0"}]
        default_agent = "codex"

    return agents, default_agent or "codex"


def _serialize_model_catalog(catalog: ModelCatalog) -> dict[str, Any]:
    return {
        "default_model": catalog.default_model,
        "models": [
            {
                "id": model.id,
                "display_name": model.display_name,
                "supports_reasoning": model.supports_reasoning,
                "reasoning_options": list(model.reasoning_options),
            }
            for model in catalog.models
        ],
    }


def build_agents_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/agents")
    def list_agents(request: Request) -> dict[str, Any]:
        agents, default_agent = _available_agents(request)
        return {"agents": agents, "default": default_agent}

    @router.get("/api/agents/{agent}/models")
    async def list_agent_models(agent: str, request: Request):
        agent_id = (agent or "").strip().lower()
        engine = request.app.state.engine
        if agent_id == "codex":
            supervisor = request.app.state.app_server_supervisor
            events = request.app.state.app_server_events
            if supervisor is None:
                raise HTTPException(status_code=404, detail="Codex harness unavailable")
            codex_harness = CodexHarness(supervisor, events)
            catalog = await codex_harness.model_catalog(engine.repo_root)
            return _serialize_model_catalog(catalog)
        if agent_id == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                raise HTTPException(
                    status_code=404, detail="OpenCode harness unavailable"
                )
            try:
                opencode_harness = OpenCodeHarness(supervisor)
                catalog = await opencode_harness.model_catalog(engine.repo_root)
                return _serialize_model_catalog(catalog)
            except OpenCodeSupervisorError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        raise HTTPException(status_code=404, detail="Unknown agent")

    @router.get("/api/agents/{agent}/turns/{turn_id}/events")
    async def stream_agent_turn_events(
        agent: str, turn_id: str, request: Request, thread_id: Optional[str] = None
    ):
        agent_id = (agent or "").strip().lower()
        if agent_id == "codex":
            events = getattr(request.app.state, "app_server_events", None)
            if events is None:
                raise HTTPException(status_code=404, detail="Codex events unavailable")
            if not thread_id:
                raise HTTPException(status_code=400, detail="thread_id is required")
            return StreamingResponse(
                events.stream(thread_id, turn_id),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        if agent_id == "opencode":
            if not thread_id:
                raise HTTPException(status_code=400, detail="thread_id is required")
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                raise HTTPException(
                    status_code=404, detail="OpenCode events unavailable"
                )
            harness = OpenCodeHarness(supervisor)
            return StreamingResponse(
                harness.stream_events(
                    request.app.state.engine.repo_root, thread_id, turn_id
                ),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        raise HTTPException(status_code=404, detail="Unknown agent")

    return router


__all__ = ["build_agents_routes"]
