"""
Agent harness support routes (models + event streaming).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..agents.codex.harness import CodexHarness
from ..agents.opencode.harness import OpenCodeHarness
from ..agents.opencode.supervisor import OpenCodeSupervisorError
from ..agents.types import ModelCatalog
from .shared import SSE_HEADERS


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


def _coerce_opencode_providers(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        providers = payload.get("providers")
        if isinstance(providers, list):
            return [entry for entry in providers if isinstance(entry, dict)]
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


def _build_opencode_model_catalog(payload: Any) -> ModelCatalog:
    from ..agents.types import ModelSpec

    providers = _coerce_opencode_providers(payload)
    models: list[ModelSpec] = []
    default_model = ""
    if isinstance(payload, dict):
        raw_default = payload.get("default")
        if isinstance(raw_default, dict):
            for provider in providers:
                provider_id = provider.get("id") or provider.get("providerID")
                if (
                    isinstance(provider_id, str)
                    and provider_id
                    and provider_id in raw_default
                ):
                    default_model_id = raw_default[provider_id]
                    if isinstance(default_model_id, str) and default_model_id:
                        default_model = f"{provider_id}/{default_model_id}"
                        break
    for provider in providers:
        provider_id = provider.get("id") or provider.get("providerID")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        models_map = provider.get("models")
        if not isinstance(models_map, dict):
            continue
        for model_id, model in models_map.items():
            if not isinstance(model_id, str) or not isinstance(model, dict):
                continue
            model_name = model.get("name") or model.get("id") or model_id
            display_name = (
                model_name if isinstance(model_name, str) and model_name else model_id
            )
            capabilities = model.get("capabilities")
            supports_reasoning = False
            if isinstance(capabilities, dict):
                supports_reasoning = bool(capabilities.get("reasoning"))
            variants = model.get("variants")
            reasoning_options: list[str] = []
            if isinstance(variants, dict):
                reasoning_options = [
                    key for key in variants.keys() if isinstance(key, str)
                ]
                if reasoning_options:
                    supports_reasoning = True
            full_id = f"{provider_id}/{model_id}"
            models.append(
                ModelSpec(
                    id=full_id,
                    display_name=display_name,
                    supports_reasoning=supports_reasoning,
                    reasoning_options=reasoning_options,
                )
            )
    if not default_model and models:
        default_model = models[0].id
    return ModelCatalog(default_model=default_model, models=models)


def build_agents_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/agents")
    def list_agents() -> dict[str, Any]:
        return {
            "agents": [
                {"id": "codex", "name": "Codex"},
                {"id": "opencode", "name": "OpenCode"},
            ],
            "default": "codex",
        }

    @router.get("/api/agents/{agent}/models")
    async def list_agent_models(agent: str, request: Request):
        agent_id = (agent or "").strip().lower()
        engine = request.app.state.engine
        if agent_id == "codex":
            supervisor = request.app.state.app_server_supervisor
            events = request.app.state.app_server_events
            if supervisor is None:
                raise HTTPException(status_code=404, detail="Codex harness unavailable")
            harness = CodexHarness(supervisor, events)
            catalog = await harness.model_catalog(engine.repo_root)
            return _serialize_model_catalog(catalog)
        if agent_id == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                raise HTTPException(
                    status_code=404, detail="OpenCode harness unavailable"
                )
            try:
                client = await supervisor.get_client(engine.repo_root)
                payload = await client.providers(directory=str(engine.repo_root))
                catalog = _build_opencode_model_catalog(payload)
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
