from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...core.app_server_events import format_sse
from ..base import AgentHarness
from ..types import AgentId, ConversationRef, ModelCatalog, ModelSpec, TurnRef
from .supervisor import OpenCodeSupervisor

_logger = logging.getLogger(__name__)


def _split_model_id(model: Optional[str]) -> Optional[dict[str, str]]:
    if not model or "/" not in model:
        return None
    provider_id, model_id = model.split("/", 1)
    provider_id = provider_id.strip()
    model_id = model_id.strip()
    if not provider_id or not model_id:
        return None
    return {"providerID": provider_id, "modelID": model_id}


def _coerce_providers(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        providers = payload.get("providers")
        if isinstance(providers, list):
            return [entry for entry in providers if isinstance(entry, dict)]
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


def _extract_session_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("sessionID", "sessionId", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    properties = payload.get("properties")
    if isinstance(properties, dict):
        value = properties.get("sessionID")
        if isinstance(value, str) and value:
            return value
        part = properties.get("part")
        if isinstance(part, dict):
            value = part.get("sessionID")
            if isinstance(value, str) and value:
                return value
    session = payload.get("session")
    if isinstance(session, dict):
        return _extract_session_id(session)
    return None


class OpenCodeHarness(AgentHarness):
    agent_id: AgentId = "opencode"
    display_name = "OpenCode"

    def __init__(self, supervisor: OpenCodeSupervisor) -> None:
        self._supervisor = supervisor

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._supervisor.get_client(workspace_root)

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        client = await self._supervisor.get_client(workspace_root)
        payload = await client.providers(directory=str(workspace_root))
        providers = _coerce_providers(payload)
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
                name = model.get("name") or model.get("id") or model_id
                display_name = name if isinstance(name, str) and name else model_id
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
                models.append(
                    ModelSpec(
                        id=f"{provider_id}/{model_id}",
                        display_name=display_name,
                        supports_reasoning=supports_reasoning,
                        reasoning_options=reasoning_options,
                    )
                )
        if not default_model and models:
            default_model = models[0].id
        return ModelCatalog(default_model=default_model, models=models)

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        client = await self._supervisor.get_client(workspace_root)
        result = await client.create_session(
            title=title,
            directory=str(workspace_root),
        )
        session_id = _extract_session_id(result) or result.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("OpenCode did not return a session id")
        return ConversationRef(agent="opencode", id=session_id)

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        client = await self._supervisor.get_client(workspace_root)
        result = await client.list_sessions(directory=str(workspace_root))
        sessions: list[dict[str, Any]] = []
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                sessions = [entry for entry in data if isinstance(entry, dict)]
        elif isinstance(result, list):
            sessions = [entry for entry in result if isinstance(entry, dict)]
        conversations: list[ConversationRef] = []
        for entry in sessions:
            session_id = _extract_session_id(entry) or entry.get("id")
            if isinstance(session_id, str) and session_id:
                conversations.append(ConversationRef(agent="opencode", id=session_id))
        return conversations

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        client = await self._supervisor.get_client(workspace_root)
        try:
            result = await client.get_session(conversation_id)
        except Exception:
            result = {}
        session_id = _extract_session_id(result) or conversation_id
        return ConversationRef(agent="opencode", id=session_id)

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> TurnRef:
        client = await self._supervisor.get_client(workspace_root)
        model_payload = _split_model_id(model)
        result = await client.send_message(
            conversation_id,
            message=prompt,
            model=model_payload,
            variant=reasoning,
        )
        turn_id = _extract_session_id(result) or result.get("id")
        if not isinstance(turn_id, str) or not turn_id:
            turn_id = f"{conversation_id}:{int(time.time() * 1000)}"
        return TurnRef(conversation_id=conversation_id, turn_id=turn_id)

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> TurnRef:
        client = await self._supervisor.get_client(workspace_root)
        arguments = prompt if prompt else ""
        await client.send_command(
            conversation_id,
            command="review",
            arguments=arguments,
            model=model,
        )
        turn_id = f"{conversation_id}:{int(time.time() * 1000)}"
        return TurnRef(conversation_id=conversation_id, turn_id=turn_id)

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        client = await self._supervisor.get_client(workspace_root)
        try:
            await client.abort(conversation_id)
        except Exception as exc:
            _logger.debug(
                "Failed to abort OpenCode session %s: %s", conversation_id, exc
            )

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[str]:
        client = await self._supervisor.get_client(workspace_root)
        async for event in client.stream_events(directory=str(workspace_root)):
            payload = event.data
            try:
                parsed = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                parsed = {"raw": payload}
            session_id = _extract_session_id(parsed)
            if event.event == "session.idle" and session_id == conversation_id:
                break
            if session_id and session_id != conversation_id:
                continue
            if not session_id:
                continue
            wrapped = {"message": {"method": event.event, "params": parsed}}
            yield format_sse("app-server", wrapped)


__all__ = ["OpenCodeHarness"]
