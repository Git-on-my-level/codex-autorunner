from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .....agents.registry import get_agent_descriptor, get_available_agents
from .....core.orchestration.catalog import map_agent_capabilities
from ...services.pma.common import pma_config_from_raw
from ..agents import (
    _available_agents,
    _serialize_agent_profiles,
    _serialize_model_catalog,
)
from .hermes_supervisors import resolve_cached_hermes_supervisor


def build_pma_meta_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build PMA metadata, audit, and model-catalog routes."""

    def _hermes_is_configured(request: Request) -> bool:
        config = getattr(request.app.state, "config", None)
        backend_getter = getattr(config, "agent_backend", None)
        if callable(backend_getter):
            try:
                return str(backend_getter("hermes")).strip().lower() == "hermes"
            except (TypeError, ValueError, RuntimeError):
                return False
        return get_agent_descriptor("hermes", request.app.state) is not None

    def _build_registered_hermes_payload(request: Request) -> dict[str, Any] | None:
        if not _hermes_is_configured(request):
            return None
        descriptor = get_agent_descriptor("hermes", request.app.state)
        if descriptor is None:
            return None
        agent_payload: dict[str, Any] = {
            "id": descriptor.id,
            "name": descriptor.name,
            "capabilities": sorted(map_agent_capabilities(descriptor.capabilities)),
        }
        agent_profiles = _serialize_agent_profiles(request, "hermes")
        if agent_profiles.get("profiles") or agent_profiles.get("default_profile"):
            agent_payload.update(agent_profiles)
        return agent_payload

    async def _enrich_hermes_payload(
        request: Request,
        agent_payload: dict[str, Any],
        *,
        default_agent: str,
        default_profile: str | None,
        include_supervisor_metadata: bool,
    ) -> dict[str, Any]:
        metadata_profile = (
            str(agent_payload.get("default_profile") or "").strip().lower() or None
        )
        if metadata_profile is None and default_agent == "hermes":
            metadata_profile = default_profile
        if include_supervisor_metadata:
            supervisor = _resolve_hermes_supervisor(
                request,
                profile=metadata_profile,
            )
            if supervisor is not None:
                try:
                    session_caps = await supervisor.session_capabilities(
                        request.app.state.config.root
                    )
                    commands = await supervisor.advertised_commands(
                        request.app.state.config.root
                    )
                except Exception:  # intentional: optional runtime metadata
                    session_caps = None
                    commands = []
                if session_caps is not None:
                    agent_payload["session_controls"] = {
                        "fork": bool(session_caps.fork),
                        "set_model": bool(session_caps.set_model),
                        "set_mode": bool(session_caps.set_mode),
                        "list_sessions": bool(session_caps.list_sessions),
                    }
                if commands:
                    agent_payload["advertised_commands"] = [
                        {
                            "name": command.name,
                            "description": command.description,
                        }
                        for command in commands
                    ]
        if metadata_profile:
            agent_payload["metadata_profile"] = metadata_profile
        return agent_payload

    def _get_pma_config(request: Request) -> dict[str, Any]:
        raw = getattr(request.app.state.config, "raw", {})
        return pma_config_from_raw(raw)

    def _get_safety_checker(request: Request):
        runtime = get_runtime_state()
        hub_root = request.app.state.config.root
        return runtime.get_safety_checker(hub_root, request)

    def _resolve_hermes_supervisor(
        request: Request,
        *,
        profile: str | None,
    ):
        return resolve_cached_hermes_supervisor(request, profile=profile)

    @router.get("/agents")
    async def list_pma_agents(request: Request) -> dict[str, Any]:
        if not get_available_agents(request.app.state):
            raise HTTPException(status_code=404, detail="PMA unavailable")
        agents, default_agent = _available_agents(request)
        defaults = _get_pma_config(request)
        default_profile = str(defaults.get("profile") or "").strip().lower() or None
        available_agent_ids = {
            str(agent.get("id") or "").strip().lower()
            for agent in agents
            if isinstance(agent, dict)
        }
        enriched_agents: list[dict[str, Any]] = []
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            agent_payload = dict(agent)
            if str(agent_payload.get("id") or "").strip().lower() == "hermes":
                agent_payload = await _enrich_hermes_payload(
                    request,
                    agent_payload,
                    default_agent=default_agent,
                    default_profile=default_profile,
                    include_supervisor_metadata=True,
                )
            enriched_agents.append(agent_payload)
        if "hermes" not in available_agent_ids:
            hermes_payload = _build_registered_hermes_payload(request)
            if hermes_payload is not None:
                enriched_agents.append(
                    await _enrich_hermes_payload(
                        request,
                        hermes_payload,
                        default_agent=default_agent,
                        default_profile=default_profile,
                        include_supervisor_metadata=False,
                    )
                )
        payload: dict[str, Any] = {"agents": agents, "default": default_agent}
        payload["agents"] = enriched_agents
        if (
            defaults.get("profile")
            or defaults.get("model")
            or defaults.get("reasoning")
        ):
            payload["defaults"] = {
                key: value
                for key, value in {
                    "profile": defaults.get("profile"),
                    "model": defaults.get("model"),
                    "reasoning": defaults.get("reasoning"),
                }.items()
                if value
            }
        return payload

    @router.get("/audit/recent")
    def get_pma_audit_log(request: Request, limit: int = 100) -> dict[str, Any]:
        entries = _get_safety_checker(request)._audit_log.list_recent(limit=limit)
        return {
            "entries": [
                {
                    "entry_id": entry.entry_id,
                    "action_type": entry.action_type.value,
                    "timestamp": entry.timestamp,
                    "agent": entry.agent,
                    "thread_id": entry.thread_id,
                    "turn_id": entry.turn_id,
                    "client_turn_id": entry.client_turn_id,
                    "details": entry.details,
                    "status": entry.status,
                    "error": entry.error,
                    "fingerprint": entry.fingerprint,
                }
                for entry in entries
            ]
        }

    @router.get("/safety/stats")
    def get_pma_safety_stats(request: Request):
        return _get_safety_checker(request).get_stats()

    @router.get("/agents/{agent}/models")
    async def list_pma_agent_models(agent: str, request: Request):
        agent_id = (agent or "").strip().lower()
        hub_root = request.app.state.config.root
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
            return _serialize_model_catalog(await harness.model_catalog(hub_root))
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            Exception
        ) as exc:  # intentional: agent harness/model_catalog errors are unpredictable
            raise HTTPException(status_code=502, detail=str(exc)) from exc
