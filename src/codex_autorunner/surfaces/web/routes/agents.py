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
from ....agents.registry import get_available_agents
from ....agents.types import ModelCatalog
from ....core.orchestration.catalog import map_agent_capabilities
from ..services.validation import normalize_agent_id
from .shared import SSE_HEADERS


def _normalize_path_agent_id(agent: str) -> str:
    # Path segments that decode to blank/whitespace are malformed and should
    # not silently fall back to the default agent.
    if not isinstance(agent, str) or not agent.strip():
        raise HTTPException(status_code=404, detail="Unknown agent")
    return normalize_agent_id(agent)


def _available_agents(request: Request) -> tuple[list[dict[str, Any]], str]:
    agents: list[dict[str, Any]] = []
    default_agent: Optional[str] = None

    available = get_available_agents(request.app.state)

    for agent_id, descriptor in available.items():
        agent_data: dict[str, Any] = {
            "id": agent_id,
            "name": descriptor.name,
            "capabilities": sorted(map_agent_capabilities(descriptor.capabilities)),
        }
        if agent_id == "codex":
            agent_data["protocol_version"] = "2.0"
        if agent_id == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor and hasattr(supervisor, "_handles"):
                handles = supervisor._handles
                if handles:
                    first_handle = next(iter(handles.values()), None)
                    if first_handle:
                        version = getattr(first_handle, "version", None)
                        if version:
                            agent_data["version"] = str(version)
        agents.append(agent_data)
        if default_agent is None:
            default_agent = agent_id

    if not agents:
        agents = [
            {
                "id": "codex",
                "name": "Codex",
                "protocol_version": "2.0",
                "capabilities": [],
            }
        ]
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
        agent_id = _normalize_path_agent_id(agent)
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
        agent: str,
        turn_id: str,
        request: Request,
        thread_id: Optional[str] = None,
        since_event_id: Optional[int] = None,
    ):
        agent_id = _normalize_path_agent_id(agent)
        resume_after = since_event_id
        if resume_after is None:
            last_event_id = request.headers.get("Last-Event-ID")
            if last_event_id:
                try:
                    resume_after = int(last_event_id)
                except ValueError:
                    resume_after = None
        if agent_id == "codex":
            events = getattr(request.app.state, "app_server_events", None)
            if events is None:
                raise HTTPException(status_code=404, detail="Codex events unavailable")
            if not thread_id:
                raise HTTPException(status_code=400, detail="thread_id is required")
            return StreamingResponse(
                events.stream(thread_id, turn_id, after_id=(resume_after or 0)),
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
