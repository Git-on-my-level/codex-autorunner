"""
Agent harness support routes (models + event streaming).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..agents.registry import (
    get_agent_descriptor,
    get_available_agents,
    has_capability,
    validate_agent_id,
)
from ..agents.types import ModelCatalog
from .shared import SSE_HEADERS

_logger = logging.getLogger(__name__)


async def _get_codex_version(app_server_supervisor: Any) -> Optional[str]:
    """Get Codex version by running binary."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        version = stdout.decode().strip() or stderr.decode().strip()
        if version and not version.lower().startswith(("error", "unknown", "usage")):
            return version.splitlines()[0] if version else None
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
        _logger.debug("Failed to get Codex version: %s", exc)
    return None


async def _get_opencode_version(opencode_supervisor: Any) -> Optional[str]:
    """Get OpenCode version from supervisor health info."""
    if opencode_supervisor is None:
        return None
    try:
        handles = opencode_supervisor._handles
        if handles:
            for handle in handles.values():
                if handle.version:
                    return handle.version
    except Exception as exc:
        _logger.debug("Failed to get OpenCode version: %s", exc)
    return None


async def _get_agent_version_info(agent_id: str, app_state: Any) -> dict[str, Any]:
    """Get version and protocol version info for an agent."""
    info: dict[str, Any] = {"version": None, "protocol_version": None}

    try:
        if agent_id == "codex":
            app_server_supervisor = getattr(app_state, "app_server_supervisor", None)
            if app_server_supervisor:
                info["version"] = await _get_codex_version(app_server_supervisor)
        elif agent_id == "opencode":
            opencode_supervisor = getattr(app_state, "opencode_supervisor", None)
            if opencode_supervisor:
                info["version"] = await _get_opencode_version(opencode_supervisor)
    except Exception as exc:
        _logger.warning("Failed to get version info for %s: %s", agent_id, exc)

    return info


def _available_agents(request: Request) -> tuple[list[dict[str, Any]], str]:
    available_agents = get_available_agents(request.app.state)
    agents_list: list[dict[str, Any]] = []
    default_agent: Optional[str] = None

    for agent_id, descriptor in available_agents.items():
        agents_list.append(
            {
                "id": agent_id,
                "name": descriptor.name,
                "available": True,
                "capabilities": sorted(descriptor.capabilities),
            }
        )
        if default_agent is None:
            default_agent = agent_id

    if not agents_list:
        default_agent = "codex"

    return agents_list, default_agent or "codex"


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
    async def list_agents(request: Request) -> dict[str, Any]:
        agents, default_agent = _available_agents(request)

        for agent in agents:
            agent_id = agent["id"]
            version_info = await _get_agent_version_info(agent_id, request.app.state)
            agent["version"] = version_info["version"]
            agent["protocol_version"] = version_info["protocol_version"]

        return {"agents": agents, "default": default_agent}

    @router.get("/api/agents/{agent}/models")
    async def list_agent_models(agent: str, request: Request):
        engine = request.app.state.engine

        try:
            agent_id = validate_agent_id(agent)
        except ValueError:
            raise HTTPException(
                status_code=404, detail=f"Unknown agent: {agent}"
            ) from None

        descriptor = get_agent_descriptor(agent_id)
        if descriptor is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown agent: {agent}"
            ) from None

        if not has_capability(agent_id, "model_listing"):
            raise HTTPException(
                status_code=501,
                detail=f"Agent {agent_id} does not support model listing",
            )

        try:
            harness = descriptor.make_harness(request.app.state)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        catalog = await harness.model_catalog(engine.repo_root)
        return _serialize_model_catalog(catalog)

    @router.get("/api/agents/{agent}/threads")
    async def list_agent_threads(agent: str, request: Request):
        engine = request.app.state.engine

        try:
            agent_id = validate_agent_id(agent)
        except ValueError:
            raise HTTPException(
                status_code=404, detail=f"Unknown agent: {agent}"
            ) from None

        descriptor = get_agent_descriptor(agent_id)
        if descriptor is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown agent: {agent}"
            ) from None

        if not has_capability(agent_id, "threads"):
            raise HTTPException(
                status_code=501,
                detail=f"Agent {agent_id} does not support thread listing",
            )

        try:
            harness = descriptor.make_harness(request.app.state)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        conversations = await harness.list_conversations(engine.repo_root)
        return {
            "threads": [{"id": conv.id, "agent": conv.agent} for conv in conversations]
        }

    @router.get("/api/agents/{agent}/turns/{turn_id}/events")
    async def stream_agent_turn_events(
        agent: str, turn_id: str, request: Request, thread_id: Optional[str] = None
    ):
        engine = request.app.state.engine

        try:
            agent_id = validate_agent_id(agent)
        except ValueError:
            raise HTTPException(
                status_code=404, detail=f"Unknown agent: {agent}"
            ) from None

        descriptor = get_agent_descriptor(agent_id)
        if descriptor is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown agent: {agent}"
            ) from None

        if not has_capability(agent_id, "event_streaming"):
            raise HTTPException(
                status_code=501,
                detail=f"Agent {agent_id} does not support event streaming",
            )

        if not thread_id:
            raise HTTPException(status_code=400, detail="thread_id is required")

        try:
            harness = descriptor.make_harness(request.app.state)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        return StreamingResponse(
            harness.stream_events(engine.repo_root, thread_id, turn_id),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return router


__all__ = ["build_agents_routes"]
