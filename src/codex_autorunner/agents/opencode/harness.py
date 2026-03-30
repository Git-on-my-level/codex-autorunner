from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx

from ...core.logging_utils import log_event
from ...core.orchestration.interfaces import FreshConversationRequiredError
from ...core.sse import format_sse, parse_sse_lines
from ...integrations.chat.agents import DEFAULT_CHAT_AGENT_MODELS
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
from .runtime import (
    build_turn_id,
    collect_opencode_output_from_events,
    extract_session_id,
    extract_turn_id,
    map_approval_policy_to_permission,
    split_model_id,
)
from .supervisor_protocol import OpenCodeHarnessSupervisorProtocol

_logger = logging.getLogger(__name__)
_GLOB_META_RE = re.compile(r"[*?\[\]{]")


@dataclass
class _PendingTurnConfig:
    model_payload: Optional[dict[str, str]]
    approval_mode: Optional[str]
    sandbox_policy: Optional[Any]
    question_policy: str
    reserved_workspace_root: Optional[Path] = None
    command_task: Optional[asyncio.Task[Any]] = None
    progress_event_subscribers: list[asyncio.Queue[Optional[dict[str, Any]]]] = field(
        default_factory=list
    )
    progress_event_history: list[dict[str, Any]] = field(default_factory=list)


def _path_is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _permission_candidate_path(value: Any, workspace_root: Path) -> Optional[Path]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = _GLOB_META_RE.search(text)
    if match is not None:
        text = text[: match.start()]
    text = text.rstrip("/")
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = workspace_root / path
    return path.resolve(strict=False)


def _collect_permission_paths(
    props: dict[str, Any], workspace_root: Path
) -> list[Path]:
    candidates: list[Path] = []

    def _append(value: Any) -> None:
        path = _permission_candidate_path(value, workspace_root)
        if path is not None:
            candidates.append(path)

    for key in ("path", "filepath", "directory"):
        _append(props.get(key))

    patterns = props.get("patterns")
    if isinstance(patterns, list):
        for item in patterns:
            _append(item)

    metadata = props.get("metadata")
    if isinstance(metadata, dict):
        for key in ("path", "filepath", "directory"):
            _append(metadata.get(key))
        nested_patterns = metadata.get("patterns")
        if isinstance(nested_patterns, list):
            for item in nested_patterns:
                _append(item)

    return candidates


def _progress_event_shape_hint(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    hints: list[str] = []
    keys = sorted(str(key) for key in payload.keys())
    if keys:
        hints.append(f"keys={','.join(keys[:6])}")
    properties = payload.get("properties")
    if isinstance(properties, dict):
        property_keys = sorted(str(key) for key in properties.keys())
        if property_keys:
            hints.append(f"properties={','.join(property_keys[:6])}")
    item = payload.get("item")
    if not isinstance(item, dict) and isinstance(properties, dict):
        nested_item = properties.get("item")
        if isinstance(nested_item, dict):
            item = nested_item
    if isinstance(item, dict):
        item_keys = sorted(str(key) for key in item.keys())
        if item_keys:
            hints.append(f"item={','.join(item_keys[:6])}")
    return " ".join(hints) or None


def _fresh_conversation_error(
    exc: Exception,
    *,
    conversation_id: str,
    operation: str,
) -> Optional[FreshConversationRequiredError]:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    response = exc.response
    request = exc.request
    status_code = response.status_code if response is not None else None
    if status_code not in {400, 404}:
        return None
    path = request.url.path if request is not None else None
    if not isinstance(path, str) or "/session/" not in path:
        return None
    return FreshConversationRequiredError(
        (
            f"OpenCode session '{conversation_id}' rejected {operation}; "
            "refresh the conversation binding"
        ),
        conversation_id=conversation_id,
        operation=operation,
        status_code=status_code,
    )


def _raise_fresh_conversation_error(
    exc: Exception,
    *,
    conversation_id: str,
    operation: str,
) -> None:
    refresh_error = _fresh_conversation_error(
        exc,
        conversation_id=conversation_id,
        operation=operation,
    )
    if refresh_error is None:
        raise exc
    log_event(
        _logger,
        logging.INFO,
        "opencode.conversation.invalidated",
        conversation_id=conversation_id,
        operation=refresh_error.operation,
        status_code=refresh_error.status_code,
    )
    raise refresh_error from exc


def _workspace_permission_decision(
    props: dict[str, Any],
    *,
    workspace_root: Path,
) -> str:
    permission_kind = (
        str(props.get("permission") or props.get("type") or props.get("tool") or "")
        .strip()
        .lower()
    )
    paths = _collect_permission_paths(props, workspace_root)
    if paths:
        if all(_path_is_within(workspace_root, path) for path in paths):
            return "allow"
        return "reject"
    if permission_kind in {"external_directory", "external_file", "external_path"}:
        return "reject"
    return "allow"


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
    properties = params.get("properties")
    if isinstance(properties, dict):
        delta = properties.get("delta")
        if isinstance(delta, dict):
            text = _normalize_message_text(delta)
            if text:
                return text
        part = properties.get("part")
        if isinstance(part, dict) and part.get("type") == "text":
            text = _normalize_message_text(part.get("text"))
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


def _extract_message_info(params: dict[str, Any]) -> dict[str, Any]:
    info = params.get("info")
    if isinstance(info, dict):
        return info
    properties = params.get("properties")
    if isinstance(properties, dict):
        nested = properties.get("info")
        if isinstance(nested, dict):
            return nested
    return {}


def _extract_message_id(params: dict[str, Any]) -> Optional[str]:
    info = _extract_message_info(params)
    for key in ("id", "messageID", "messageId", "message_id"):
        value = info.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("messageID", "messageId", "message_id"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_message_role(params: dict[str, Any]) -> Optional[str]:
    info = _extract_message_info(params)
    role = info.get("role")
    if isinstance(role, str) and role:
        return role
    role = params.get("role")
    if isinstance(role, str) and role:
        return role
    return None


def _normalize_message_phase(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"commentary", "final_answer"}:
        return normalized
    return None


def _extract_message_phase(params: dict[str, Any]) -> Optional[str]:
    phase = _normalize_message_phase(params.get("phase"))
    if phase:
        return phase
    info = _extract_message_info(params)
    phase = _normalize_message_phase(info.get("phase"))
    if phase:
        return phase
    properties = params.get("properties")
    if isinstance(properties, dict):
        phase = _normalize_message_phase(properties.get("phase"))
        if phase:
            return phase
    message = params.get("message")
    if isinstance(message, dict):
        phase = _normalize_message_phase(message.get("phase"))
        if phase:
            return phase
    item = params.get("item")
    if isinstance(item, dict):
        phase = _normalize_message_phase(item.get("phase"))
        if phase:
            return phase
    return None


def _extract_part_message_id(params: dict[str, Any]) -> Optional[str]:
    properties = params.get("properties")
    part = properties.get("part") if isinstance(properties, dict) else None
    if not isinstance(part, dict):
        return None
    for key in ("messageID", "messageId", "message_id"):
        value = part.get(key)
        if isinstance(value, str) and value:
            return value
    return None


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
    output_text = ""
    completed_message: Optional[str] = None
    completed_final_message: Optional[str] = None
    commentary_message: Optional[str] = None
    errors: list[str] = []
    message_roles: dict[str, str] = {}
    pending_by_message: dict[str, str] = {}
    pending_no_id = ""
    message_roles_seen = False

    def _merge_stream(current: str, incoming: str) -> str:
        if not incoming:
            return current
        if not current:
            return incoming
        if incoming == current:
            return current
        if len(incoming) > len(current) and incoming.startswith(current):
            return incoming
        max_overlap = min(len(current), max(len(incoming) - 1, 0))
        for overlap in range(max_overlap, 0, -1):
            if current[-overlap:] == incoming[:overlap]:
                return f"{current}{incoming[overlap:]}"
        return f"{current}{incoming}"

    def _append_part_text(message_id: Optional[str], text: str) -> None:
        nonlocal output_text
        nonlocal pending_no_id
        if not text:
            return
        if message_id is None:
            if not message_roles_seen:
                output_text = _merge_stream(output_text, text)
            else:
                pending_no_id = _merge_stream(pending_no_id, text)
            return
        role = message_roles.get(message_id)
        if role == "user":
            return
        if role == "assistant":
            output_text = _merge_stream(output_text, text)
            return
        pending_by_message[message_id] = _merge_stream(
            pending_by_message.get(message_id, ""),
            text,
        )

    def _register_role(message_id: Optional[str], role: Optional[str]) -> None:
        nonlocal output_text
        nonlocal pending_no_id
        nonlocal message_roles_seen
        if not message_id or not role:
            return
        message_roles[message_id] = role
        message_roles_seen = True
        pending = pending_by_message.pop(message_id, "")
        if role == "assistant":
            if pending:
                output_text = _merge_stream(output_text, pending)
            if pending_no_id:
                output_text = _merge_stream(output_text, pending_no_id)
                pending_no_id = ""
            return
        if role == "user":
            pending_no_id = ""

    def _flush_fallback_pending() -> None:
        nonlocal output_text
        if pending_by_message:
            for message_id, pending in list(pending_by_message.items()):
                if message_roles.get(message_id) == "assistant":
                    output_text = _merge_stream(output_text, pending)
            pending_by_message.clear()
        if pending_no_id and not message_roles_seen:
            output_text = _merge_stream(output_text, pending_no_id)

    def _record_completed_message(text: str, phase: Optional[str]) -> None:
        nonlocal commentary_message
        nonlocal completed_final_message
        nonlocal completed_message
        if not text:
            return
        if phase == "final_answer":
            completed_final_message = text
            return
        if phase == "commentary":
            commentary_message = text
            return
        completed_message = text

    for payload in payloads:
        method, params = _unwrap_harness_payload(payload)
        method_lower = method.lower()

        if method in {
            "message.delta",
            "message.updated",
            "message.completed",
            "message.part.updated",
        }:
            if method in {"message.updated", "message.completed"}:
                _register_role(
                    _extract_message_id(params),
                    _extract_message_role(params),
                )
            text = _extract_delta_text(params) or _extract_completed_text(params)
            if text:
                if method == "message.delta":
                    output_text = _merge_stream(output_text, text)
                elif method == "message.part.updated":
                    _append_part_text(_extract_part_message_id(params), text)
                else:
                    role = _extract_message_role(params)
                    if role != "user":
                        _record_completed_message(text, _extract_message_phase(params))
            continue

        if method == "item/agentMessage/delta" or method == "turn/streamDelta":
            text = _extract_delta_text(params)
            if text:
                output_text = _merge_stream(output_text, text)
            continue

        if "outputdelta" in method_lower:
            text = _extract_delta_text(params)
            if text:
                output_text = _merge_stream(output_text, text)
            continue

        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = _extract_completed_text(params)
                if text:
                    _record_completed_message(text, _extract_message_phase(params))
            continue

        if method in {"turn/error", "error"}:
            error = _extract_error_text(params)
            if error:
                errors.append(error)

    if not completed_message:
        _flush_fallback_pending()
    assistant_text = (
        completed_final_message
        or completed_message
        or output_text
        or commentary_message
        or ""
    ).strip()
    return assistant_text, errors


def _saw_terminal_completion(payloads: list[dict[str, Any]]) -> bool:
    for payload in payloads:
        method, params = _unwrap_harness_payload(payload)
        if method == "turn/completed":
            return True
        if method == "message.completed":
            return _extract_message_phase(params) != "commentary"
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                return _extract_message_phase(params) != "commentary"
    return False


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

    def allows_parallel_event_stream(self) -> bool:
        # wait_for_turn() consumes the OpenCode event stream to collect output.
        return False

    def progress_event_stream(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[Any]:
        pending = self._pending_turns.get((conversation_id, turn_id or ""))
        if pending is None:
            _ = workspace_root, conversation_id, turn_id

            async def _empty() -> AsyncIterator[Any]:
                if False:
                    yield None

            return _empty()

        queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue()
        for item in pending.progress_event_history:
            queue.put_nowait(item)
        pending.progress_event_subscribers.append(queue)

        async def _stream() -> AsyncIterator[Any]:
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item
            finally:
                try:
                    pending.progress_event_subscribers.remove(queue)
                except ValueError:
                    pass

        return _stream()

    def __init__(self, supervisor: OpenCodeHarnessSupervisorProtocol) -> None:
        self._supervisor = supervisor
        self._pending_turns: dict[tuple[str, str], _PendingTurnConfig] = {}
        self._reserved_conversations: dict[str, Path] = {}

    async def _acquire_turn_client(
        self, workspace_root: Path
    ) -> tuple[Any, Optional[Path]]:
        canonical_workspace = workspace_root.resolve()
        guarded_getter = getattr(self._supervisor, "get_client_for_turn", None)
        if callable(guarded_getter):
            client = await guarded_getter(canonical_workspace)
            return client, canonical_workspace
        client = await self._supervisor.get_client(canonical_workspace)
        marker = getattr(self._supervisor, "mark_turn_started", None)
        if not callable(marker):
            return client, None
        await marker(canonical_workspace)
        return client, canonical_workspace

    async def _release_turn_client(self, workspace_root: Optional[Path]) -> None:
        if workspace_root is None:
            return
        marker = getattr(self._supervisor, "mark_turn_finished", None)
        if not callable(marker):
            return
        try:
            await marker(workspace_root)
        except Exception:
            pass

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._supervisor.get_client(workspace_root)

    async def backend_runtime_instance_id(self, workspace_root: Path) -> Optional[str]:
        resolver = getattr(
            self._supervisor,
            "backend_runtime_instance_id_for_workspace",
            None,
        )
        if not callable(resolver):
            return None
        runtime_instance_id = await resolver(workspace_root)
        if not isinstance(runtime_instance_id, str):
            return None
        normalized = runtime_instance_id.strip()
        return normalized or None

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
        canonical_workspace = workspace_root.resolve()
        client, reserved_workspace = await self._acquire_turn_client(
            canonical_workspace
        )
        try:
            result = await client.create_session(
                title=title,
                directory=str(canonical_workspace),
            )
            session_id = extract_session_id(result) or result.get("id")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("OpenCode did not return a session id")
            if reserved_workspace is not None:
                self._reserved_conversations[session_id] = reserved_workspace
            return ConversationRef(agent=AgentId("opencode"), id=session_id)
        except Exception:
            await self._release_turn_client(reserved_workspace)
            raise

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
        canonical_workspace = workspace_root.resolve()
        client, reserved_workspace = await self._acquire_turn_client(
            canonical_workspace
        )
        try:
            result = await client.get_session(conversation_id)
        except Exception as exc:
            await self._release_turn_client(reserved_workspace)
            _raise_fresh_conversation_error(
                exc,
                conversation_id=conversation_id,
                operation="resume_conversation",
            )
        session_id = extract_session_id(result) or conversation_id
        if reserved_workspace is not None:
            self._reserved_conversations[session_id] = reserved_workspace
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
        canonical_workspace = workspace_root.resolve()
        reserved_workspace = self._reserved_conversations.pop(conversation_id, None)
        if reserved_workspace is None:
            client, reserved_workspace = await self._acquire_turn_client(
                canonical_workspace
            )
        else:
            client = await self._supervisor.get_client(canonical_workspace)
        if model is None:
            model = DEFAULT_CHAT_AGENT_MODELS.get("opencode")
        model_payload = split_model_id(model)
        turn_id = build_turn_id(conversation_id)
        try:
            await client.prompt_async(
                conversation_id,
                message=prompt,
                model=model_payload,
                variant=reasoning,
            )
        except Exception as exc:
            await self._release_turn_client(reserved_workspace)
            _raise_fresh_conversation_error(
                exc,
                conversation_id=conversation_id,
                operation="start_turn",
            )
        self._pending_turns[(conversation_id, turn_id)] = _PendingTurnConfig(
            model_payload=model_payload,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
            reserved_workspace_root=reserved_workspace,
            question_policy=(
                "reject"
                if approval_mode is not None or sandbox_policy is not None
                else "ignore"
            ),
        )
        return TurnRef(
            conversation_id=conversation_id,
            turn_id=turn_id,
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
        canonical_workspace = workspace_root.resolve()
        reserved_workspace = self._reserved_conversations.pop(conversation_id, None)
        if reserved_workspace is None:
            client, reserved_workspace = await self._acquire_turn_client(
                canonical_workspace
            )
        else:
            client = await self._supervisor.get_client(canonical_workspace)
        if model is None:
            model = DEFAULT_CHAT_AGENT_MODELS.get("opencode")
        arguments = prompt if prompt else ""
        turn_id = build_turn_id(conversation_id)
        try:
            result = await client.send_command(
                conversation_id,
                command="review",
                arguments=arguments,
                model=model,
            )
        except Exception as exc:
            await self._release_turn_client(reserved_workspace)
            _raise_fresh_conversation_error(
                exc,
                conversation_id=conversation_id,
                operation="start_review",
            )
        started_turn_id = extract_turn_id(conversation_id, result)
        if started_turn_id:
            _logger.debug("OpenCode review started: %s", started_turn_id)

        self._pending_turns[(conversation_id, turn_id)] = _PendingTurnConfig(
            model_payload=split_model_id(model),
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
            reserved_workspace_root=reserved_workspace,
            question_policy=(
                "reject"
                if approval_mode is not None or sandbox_policy is not None
                else "ignore"
            ),
        )
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
        pending = self._pending_turns.get((conversation_id, turn_id or ""))
        client = await self._supervisor.get_client(workspace_root)
        workspace_root = workspace_root.resolve()

        async def _collect() -> TerminalTurnResult:
            if pending is None:
                payloads: list[dict[str, Any]] = []
                stream_error: Optional[Exception] = None

                async def _iter_lines(raw_event_text: str) -> AsyncIterator[str]:
                    for line in raw_event_text.splitlines():
                        yield line
                    yield ""

                try:
                    async for raw_event in self.stream_events(
                        workspace_root,
                        conversation_id,
                        turn_id or "",
                    ):
                        async for sse_event in parse_sse_lines(
                            _iter_lines(str(raw_event))
                        ):
                            try:
                                payload = (
                                    json.loads(sse_event.data) if sse_event.data else {}
                                )
                            except json.JSONDecodeError:
                                payload = {}
                            if isinstance(payload, dict):
                                payloads.append(payload)
                except Exception as exc:
                    stream_error = exc

                assistant_text, errors = _collect_terminal_text(payloads)
                if stream_error is not None and not (
                    assistant_text and _saw_terminal_completion(payloads)
                ):
                    raise stream_error
                return TerminalTurnResult(
                    status="error" if errors else "ok",
                    assistant_text=assistant_text,
                    errors=errors,
                    raw_events=payloads,
                )

            raw_events: list[dict[str, Any]] = []

            def _publish_progress_event(raw_event: dict[str, Any]) -> None:
                if not raw_event:
                    return
                pending.progress_event_history.append(raw_event)
                if len(pending.progress_event_history) > 64:
                    del pending.progress_event_history[:-64]
                for queue in list(pending.progress_event_subscribers):
                    queue.put_nowait(raw_event)

            def _close_progress_streams() -> None:
                for queue in list(pending.progress_event_subscribers):
                    queue.put_nowait(None)

            async def _event_stream() -> AsyncIterator[Any]:
                try:
                    async for event in client.stream_events(
                        directory=str(workspace_root)
                    ):
                        payload = event.data
                        try:
                            parsed = json.loads(payload) if payload else {}
                        except json.JSONDecodeError:
                            parsed = {"raw": payload}
                        if isinstance(parsed, dict):
                            wrapped = {
                                "message": {"method": event.event, "params": parsed}
                            }
                            raw_events.append(wrapped)
                            session_id = extract_session_id(parsed)
                            status_type = None
                            if event.event == "session.status":
                                properties = parsed.get("properties")
                                if isinstance(properties, dict):
                                    status = properties.get("status") or {}
                                else:
                                    status = parsed.get("status") or {}
                                if isinstance(status, dict):
                                    status_type = status.get("type") or status.get(
                                        "status"
                                    )
                            is_idle = event.event == "session.idle" or (
                                event.event == "session.status"
                                and isinstance(status_type, str)
                                and status_type.lower() == "idle"
                            )
                            if (
                                session_id == conversation_id
                                and not is_idle
                                and session_id
                            ):
                                _publish_progress_event(wrapped)
                            elif not is_idle:
                                log_event(
                                    _logger,
                                    logging.DEBUG,
                                    "opencode.progress_event.skipped",
                                    method=event.event,
                                    conversation_id=conversation_id,
                                    event_session_id=session_id,
                                    reason=(
                                        "missing_session_id"
                                        if not session_id
                                        else "session_mismatch"
                                    ),
                                    shape_hint=_progress_event_shape_hint(parsed),
                                )
                        yield event
                finally:
                    _close_progress_streams()

            async def _fetch_session() -> Any:
                statuses = await client.session_status(directory=str(workspace_root))
                if isinstance(statuses, dict):
                    session_status = statuses.get(conversation_id)
                    if session_status is None:
                        return {"status": {"type": "idle"}}
                    if isinstance(session_status, dict):
                        return {"status": session_status}
                    if isinstance(session_status, str):
                        return {"status": session_status}
                return {"status": {}}

            async def _fetch_providers() -> Any:
                return await client.providers(directory=str(workspace_root))

            async def _respond_permission(request_id: str, reply: str) -> None:
                await client.respond_permission(request_id=request_id, reply=reply)

            async def _reply_question(
                request_id: str, answers: list[list[str]]
            ) -> None:
                await client.reply_question(request_id, answers=answers)

            async def _reject_question(request_id: str) -> None:
                await client.reject_question(request_id)

            permission_policy = map_approval_policy_to_permission(
                pending.approval_mode if pending is not None else None,
                default="allow",
            )
            permission_handler = None
            if permission_policy == "ask":

                async def _permission_handler(
                    _request_id: str, props: dict[str, Any]
                ) -> str:
                    return _workspace_permission_decision(
                        props,
                        workspace_root=workspace_root,
                    )

                permission_handler = _permission_handler

            collect_task = asyncio.create_task(
                collect_opencode_output_from_events(
                    None,
                    session_id=conversation_id,
                    model_payload=(
                        pending.model_payload if pending is not None else None
                    ),
                    permission_policy=permission_policy,
                    permission_handler=permission_handler,
                    question_policy=(
                        pending.question_policy if pending is not None else "ignore"
                    ),
                    respond_permission=_respond_permission,
                    reply_question=_reply_question,
                    reject_question=_reject_question,
                    event_stream_factory=_event_stream,
                    session_fetcher=_fetch_session,
                    provider_fetcher=_fetch_providers,
                    stall_timeout_seconds=await self._supervisor.session_stall_timeout_seconds_for_workspace(
                        workspace_root
                    ),
                )
            )
            command_task = pending.command_task if pending is not None else None
            if command_task is None:
                output = await collect_task
            else:
                try:
                    done, _pending = await asyncio.wait(
                        {collect_task, command_task},
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                    if command_task in done:
                        command_exc = command_task.exception()
                        if command_exc is not None:
                            collect_task.cancel()
                            try:
                                await collect_task
                            except asyncio.CancelledError:
                                pass
                            raise command_exc
                    output = await collect_task
                    await command_task
                except Exception as exc:
                    return TerminalTurnResult(
                        status="error",
                        assistant_text="",
                        errors=[str(exc)],
                        raw_events=raw_events,
                    )

            errors = [output.error] if output.error else []
            return TerminalTurnResult(
                status="error" if errors else "ok",
                assistant_text=output.text,
                errors=errors,
                raw_events=raw_events,
            )

        try:
            if timeout is None:
                return await _collect()
            return await asyncio.wait_for(_collect(), timeout=timeout)
        finally:
            reserved_workspace = (
                pending.reserved_workspace_root if pending is not None else None
            )
            if turn_id is not None:
                self._pending_turns.pop((conversation_id, turn_id), None)
            await self._release_turn_client(reserved_workspace)


__all__ = ["OpenCodeHarness"]
