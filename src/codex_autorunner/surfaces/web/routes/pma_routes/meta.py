from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .....agents.codex.harness import CodexHarness
from .....agents.opencode.harness import OpenCodeHarness
from .....agents.opencode.supervisor import OpenCodeSupervisorError
from ...services.pma.common import pma_config_from_raw
from ..agents import _available_agents, _serialize_model_catalog


def build_pma_meta_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build PMA metadata, audit, and model-catalog routes."""

    def _get_pma_config(request: Request) -> dict[str, Any]:
        raw = getattr(request.app.state.config, "raw", {})
        return pma_config_from_raw(raw)

    def _get_safety_checker(request: Request):
        runtime = get_runtime_state()
        hub_root = request.app.state.config.root
        return runtime.get_safety_checker(hub_root, request)

    @router.get("/agents")
    def list_pma_agents(request: Request) -> dict[str, Any]:
        if (
            getattr(request.app.state, "app_server_supervisor", None) is None
            and getattr(request.app.state, "opencode_supervisor", None) is None
        ):
            raise HTTPException(status_code=404, detail="PMA unavailable")
        agents, default_agent = _available_agents(request)
        defaults = _get_pma_config(request)
        payload: dict[str, Any] = {"agents": agents, "default": default_agent}
        if defaults.get("model") or defaults.get("reasoning"):
            payload["defaults"] = {
                key: value
                for key, value in {
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
        if agent_id == "codex":
            supervisor = request.app.state.app_server_supervisor
            events = request.app.state.app_server_events
            if supervisor is None:
                raise HTTPException(status_code=404, detail="Codex harness unavailable")
            return _serialize_model_catalog(
                await CodexHarness(supervisor, events).model_catalog(hub_root)
            )
        if agent_id == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                raise HTTPException(
                    status_code=404, detail="OpenCode harness unavailable"
                )
            try:
                return _serialize_model_catalog(
                    await OpenCodeHarness(supervisor).model_catalog(hub_root)
                )
            except OpenCodeSupervisorError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        raise HTTPException(status_code=404, detail="Unknown agent")
