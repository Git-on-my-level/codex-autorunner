"""
Agent harness support routes (models + event streaming).
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Iterable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ....agents.registry import get_agent_descriptor, get_available_agents
from ....core.orchestration.catalog import map_agent_capabilities
from ....core.sse import format_sse
from .agents_helpers import (
    normalize_path_agent_id,
    parse_resume_after,
    serialize_agent_profiles,
    serialize_model_catalog,
)
from .shared import SSE_HEADERS

_logger = logging.getLogger(__name__)


_serialize_model_catalog = serialize_model_catalog


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
        agent_profiles = _serialize_agent_profiles(request, agent_id)
        if agent_profiles["profiles"]:
            agent_data.update(agent_profiles)
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
        fallback_descriptor = get_agent_descriptor("codex", request.app.state)
        if fallback_descriptor is not None:
            agents = [
                {
                    "id": fallback_descriptor.id,
                    "name": fallback_descriptor.name,
                    "protocol_version": "2.0",
                    "capabilities": [],
                }
            ]
            default_agent = fallback_descriptor.id
        else:
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


def _serialize_agent_profiles(request: Request, agent_id: str) -> dict[str, Any]:
    config = getattr(request.app.state, "config", None)
    profile_getter = getattr(config, "agent_profiles", None)
    default_getter = getattr(config, "agent_default_profile", None)
    configured_profiles: object = {}
    if callable(profile_getter):
        try:
            configured_profiles = profile_getter(agent_id)
        except (ValueError, TypeError):
            configured_profiles = {}
    hermes_profile_options: Iterable[Any] = ()
    if agent_id == "hermes":
        try:
            from ....integrations.chat.agents import chat_hermes_profile_options

            hermes_profile_options = chat_hermes_profile_options(request.app.state)
        except Exception:  # intentional: optional hermes integration
            _logger.debug("Failed to resolve hermes profile options", exc_info=True)
    default_profile = None
    if callable(default_getter):
        try:
            default_profile = default_getter(agent_id)
        except (ValueError, TypeError):
            default_profile = None
    return serialize_agent_profiles(
        configured_profiles,
        default_profile,
        hermes_profile_options=hermes_profile_options,
    )


def build_agents_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/agents")
    def list_agents(request: Request) -> dict[str, Any]:
        agents, default_agent = _available_agents(request)
        return {"agents": agents, "default": default_agent}

    @router.get("/api/agents/{agent}/models")
    async def list_agent_models(agent: str, request: Request):
        try:
            agent_id = normalize_path_agent_id(agent)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        engine = request.app.state.engine
        descriptor = get_agent_descriptor(agent_id, request.app.state)
        if descriptor is None:
            raise HTTPException(status_code=404, detail="Unknown agent")
        if "model_listing" not in descriptor.capabilities:
            raise HTTPException(
                status_code=400,
                detail=f"Agent '{agent_id}' does not support capability 'model_listing'",
            )
        try:
            harness = descriptor.make_harness(request.app.state)
            catalog = await harness.model_catalog(engine.repo_root)
            return serialize_model_catalog(catalog)
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # intentional: harness error → HTTP 502
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/api/agents/{agent}/turns/{turn_id}/events")
    async def stream_agent_turn_events(
        agent: str,
        turn_id: str,
        request: Request,
        thread_id: Optional[str] = None,
        since_event_id: Optional[int] = None,
    ):
        try:
            agent_id = normalize_path_agent_id(agent)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        resume_after = parse_resume_after(
            since_event_id,
            request.headers.get("Last-Event-ID"),
        )
        events = getattr(request.app.state, "app_server_events", None)
        if agent_id == "codex":
            if events is None:
                raise HTTPException(status_code=404, detail="Codex events unavailable")
            if not thread_id:
                raise HTTPException(status_code=400, detail="thread_id is required")
            return StreamingResponse(
                events.stream(thread_id, turn_id, after_id=(resume_after or 0)),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        descriptor = get_agent_descriptor(agent_id, request.app.state)
        if descriptor is None:
            raise HTTPException(status_code=404, detail="Unknown agent")
        if "event_streaming" not in descriptor.capabilities:
            raise HTTPException(
                status_code=400,
                detail=f"Agent '{agent_id}' does not support capability 'event_streaming'",
            )
        if not thread_id:
            raise HTTPException(status_code=400, detail="thread_id is required")
        try:
            harness = descriptor.make_harness(request.app.state)

            async def _stream_harness_events() -> AsyncIterator[str]:
                async for raw_event in harness.stream_events(
                    request.app.state.engine.repo_root, thread_id, turn_id
                ):
                    payload = (
                        raw_event
                        if isinstance(raw_event, dict)
                        else {"value": raw_event}
                    )
                    yield format_sse("app-server", payload)

            return StreamingResponse(
                _stream_harness_events(),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router


__all__ = ["build_agents_routes"]
