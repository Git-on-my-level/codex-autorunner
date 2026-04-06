"""
Agent harness support routes (models + event streaming).
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ....agents.registry import get_agent_descriptor, get_available_agents
from ....agents.types import ModelCatalog
from ....core.orchestration.catalog import map_agent_capabilities
from ....core.sse import format_sse
from ..services.validation import normalize_agent_id
from .shared import SSE_HEADERS

_logger = logging.getLogger(__name__)


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


def _serialize_agent_profiles(request: Request, agent_id: str) -> dict[str, Any]:
    config = getattr(request.app.state, "config", None)
    profile_getter = getattr(config, "agent_profiles", None)
    default_getter = getattr(config, "agent_default_profile", None)
    profiles: list[dict[str, Any]] = []
    if callable(profile_getter):
        try:
            configured_profiles = profile_getter(agent_id)
        except (ValueError, TypeError):
            configured_profiles = {}
        if isinstance(configured_profiles, dict):
            for profile_id in sorted(configured_profiles):
                profile_cfg = configured_profiles.get(profile_id)
                display_name = None
                if profile_cfg is not None:
                    display_name = getattr(profile_cfg, "display_name", None)
                profiles.append(
                    {
                        "id": profile_id,
                        "display_name": (
                            str(display_name).strip()
                            if isinstance(display_name, str) and display_name.strip()
                            else profile_id
                        ),
                    }
                )
    if agent_id == "hermes":
        try:
            from ....integrations.chat.agents import chat_hermes_profile_options

            existing_ids = {p["id"] for p in profiles}
            for option in chat_hermes_profile_options(request.app.state):
                if option.profile in existing_ids:
                    continue
                desc = option.description
                profiles.append(
                    {
                        "id": option.profile,
                        "display_name": (
                            desc.strip()
                            if isinstance(desc, str) and desc.strip()
                            else option.profile
                        ),
                    }
                )
                existing_ids.add(option.profile)
            profiles.sort(key=lambda p: p["id"])
        except Exception:  # intentional: optional hermes integration
            _logger.debug("Failed to resolve hermes profile options", exc_info=True)
    default_profile = None
    if callable(default_getter):
        try:
            default_profile = default_getter(agent_id)
        except (ValueError, TypeError):
            default_profile = None
    return {
        "default_profile": default_profile,
        "profiles": profiles,
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
            return _serialize_model_catalog(catalog)
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
        agent_id = _normalize_path_agent_id(agent)
        resume_after = since_event_id
        if resume_after is None:
            last_event_id = request.headers.get("Last-Event-ID")
            if last_event_id:
                try:
                    resume_after = int(last_event_id)
                except ValueError:
                    resume_after = None
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
