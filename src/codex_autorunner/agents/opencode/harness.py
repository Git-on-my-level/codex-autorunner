from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...core.sse import format_sse, parse_sse_lines
from ..base import AgentHarness
from ..types import (
    AgentId,
    ConversationRef,
    ModelCatalog,
    ModelSpec,
    RuntimeCapability,
    TerminalTurnResult,
    TurnRef,
)
from .constants import DEFAULT_TICKET_MODEL
from .runtime import (
    build_turn_id,
    extract_session_id,
    extract_turn_id,
    split_model_id,
)
from .supervisor import OpenCodeSupervisor

_logger = logging.getLogger(__name__)


def _normalize_message_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, dict):
                part_text = part.get("text")
                if isinstance(part_text, str):
                    parts.append(part_text)
        joined = "".join(parts).strip()
        return joined or None
    if isinstance(value, dict):
        for key in ("text", "message", "content"):
            nested_text = _normalize_message_text(value.get(key))
            if nested_text:
                return nested_text
        return None
    return None


def _extract_delta_text(params: dict[str, Any]) -> Optional[str]:
    for key in ("content", "delta", "text"):
        text = _normalize_message_text(params.get(key))
        if text:
            return text
    message = params.get("message")
    if isinstance(message, dict):
        return _extract_delta_text(message)
    item = params.get("item")
    if isinstance(item, dict):
        return _extract_delta_text(item)
    return None


def _extract_completed_text(params: dict[str, Any]) -> Optional[str]:
    item = params.get("item")
    if isinstance(item, dict):
        return _normalize_message_text(item.get("content")) or _normalize_message_text(
            item
        )
    result = params.get("result")
    if isinstance(result, dict):
        return _normalize_message_text(result)
    return _normalize_message_text(params)


def _extract_error_text(params: dict[str, Any]) -> Optional[str]:
    error = params.get("error")
    if isinstance(error, dict):
        return _normalize_message_text(error)
    return _normalize_message_text(params.get("message")) or _normalize_message_text(
        params.get("detail")
    )


def _unwrap_harness_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if isinstance(payload.get("message"), dict):
        message = payload["message"]
        method = message.get("method")
        params = message.get("params")
        if isinstance(method, str) and isinstance(params, dict):
            return method, params
    method = payload.get("method")
    params = payload.get("params")
    if isinstance(method, str) and isinstance(params, dict):
        return method, params
    return "", {}


def _collect_terminal_text(payloads: list[dict[str, Any]]) -> tuple[str, list[str]]:
    output_chunks: list[str] = []
    completed_message: Optional[str] = None
    errors: list[str] = []
    for payload in payloads:
        method, params = _unwrap_harness_payload(payload)
        method_lower = method.lower()

        if method in {"message.delta", "message.updated", "message.completed"}:
            text = _extract_delta_text(params) or _extract_completed_text(params)
            if text:
                if method == "message.delta":
                    output_chunks.append(text)
                else:
                    completed_message = text
            continue

        if method == "item/agentMessage/delta" or method == "turn/streamDelta":
            text = _extract_delta_text(params)
            if text:
                output_chunks.append(text)
            continue

        if "outputdelta" in method_lower:
            text = _extract_delta_text(params)
            if text:
                output_chunks.append(text)
            continue

        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = _extract_completed_text(params)
                if text:
                    completed_message = text
            continue

        if method in {"turn/error", "error"}:
            error = _extract_error_text(params)
            if error:
                errors.append(error)

    assistant_text = (completed_message or "".join(output_chunks)).strip()
    return assistant_text, errors


def _coerce_providers(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        providers = payload.get("providers")
        if isinstance(providers, list):
            return [entry for entry in providers if isinstance(entry, dict)]
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


def _iter_provider_models(models_raw: Any) -> list[tuple[str, dict[str, Any]]]:
    models: list[tuple[str, dict[str, Any]]] = []
    if isinstance(models_raw, dict):
        for model_id, model in models_raw.items():
            if isinstance(model_id, str) and model_id:
                if isinstance(model, dict):
                    models.append((model_id, model))
                else:
                    models.append((model_id, {"id": model_id}))
        return models
    if isinstance(models_raw, list):
        for entry in models_raw:
            if isinstance(entry, dict):
                model_id = entry.get("id") or entry.get("modelID")
                if isinstance(model_id, str) and model_id:
                    models.append((model_id, entry))
            elif isinstance(entry, str) and entry:
                models.append((entry, {"id": entry}))
    return models


class OpenCodeHarness(AgentHarness):
    agent_id: AgentId = AgentId("opencode")
    display_name = "OpenCode"
    capabilities = frozenset(
        [
            RuntimeCapability("durable_threads"),
            RuntimeCapability("message_turns"),
            RuntimeCapability("interrupt"),
            RuntimeCapability("active_thread_discovery"),
            RuntimeCapability("review"),
            RuntimeCapability("model_listing"),
            RuntimeCapability("event_streaming"),
        ]
    )

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
            models_raw = provider.get("models")
            for model_id, model in _iter_provider_models(models_raw):
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
        session_id = extract_session_id(result) or result.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("OpenCode did not return a session id")
        return ConversationRef(agent=AgentId("opencode"), id=session_id)

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
            session_id = extract_session_id(entry) or entry.get("id")
            if isinstance(session_id, str) and session_id:
                conversations.append(
                    ConversationRef(agent=AgentId("opencode"), id=session_id)
                )
        return conversations

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        client = await self._supervisor.get_client(workspace_root)
        try:
            result = await client.get_session(conversation_id)
        except Exception:
            result = {}
        session_id = extract_session_id(result) or conversation_id
        return ConversationRef(agent=AgentId("opencode"), id=session_id)

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
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
        _ = input_items
        client = await self._supervisor.get_client(workspace_root)
        if model is None:
            model = DEFAULT_TICKET_MODEL
        model_payload = split_model_id(model)
        await client.prompt_async(
            conversation_id,
            message=prompt,
            model=model_payload,
            variant=reasoning,
        )
        return TurnRef(
            conversation_id=conversation_id,
            turn_id=build_turn_id(conversation_id),
        )

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
        if model is None:
            model = DEFAULT_TICKET_MODEL
        arguments = prompt if prompt else ""

        async def _send_review() -> None:
            try:
                result = await client.send_command(
                    conversation_id,
                    command="review",
                    arguments=arguments,
                    model=model,
                )
                turn_id = extract_turn_id(conversation_id, result)
                if turn_id:
                    _logger.debug("OpenCode review started: %s", turn_id)
            except Exception as exc:
                _logger.warning("OpenCode review command failed: %s", exc)

        asyncio.create_task(_send_review())
        turn_id = build_turn_id(conversation_id)
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
            session_id = extract_session_id(parsed)
            status_type = None
            if event.event == "session.status" and isinstance(parsed, dict):
                properties = parsed.get("properties")
                if isinstance(properties, dict):
                    status = properties.get("status") or {}
                else:
                    status = parsed.get("status") or {}
                if isinstance(status, dict):
                    status_type = status.get("type") or status.get("status")
            if (
                event.event == "session.idle"
                or (
                    event.event == "session.status"
                    and isinstance(status_type, str)
                    and status_type.lower() == "idle"
                )
            ) and session_id == conversation_id:
                break
            if session_id and session_id != conversation_id:
                continue
            if not session_id:
                continue
            wrapped = {"message": {"method": event.event, "params": parsed}}
            yield format_sse("app-server", wrapped)

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = turn_id

        async def _collect() -> TerminalTurnResult:
            payloads: list[dict[str, Any]] = []

            async def _iter_lines(raw_event_text: str) -> AsyncIterator[str]:
                for line in raw_event_text.splitlines():
                    yield line
                yield ""

            async for raw_event in self.stream_events(
                workspace_root,
                conversation_id,
                turn_id or "",
            ):
                async for sse_event in parse_sse_lines(_iter_lines(str(raw_event))):
                    try:
                        payload = json.loads(sse_event.data) if sse_event.data else {}
                    except json.JSONDecodeError:
                        payload = {}
                    if isinstance(payload, dict):
                        payloads.append(payload)

            assistant_text, errors = _collect_terminal_text(payloads)
            return TerminalTurnResult(
                status="error" if errors else "ok",
                assistant_text=assistant_text,
                errors=errors,
                raw_events=payloads,
            )

        if timeout is None:
            return await _collect()
        return await asyncio.wait_for(_collect(), timeout=timeout)


__all__ = ["OpenCodeHarness"]
