from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import Request

from .....adapters.chat.agents import resolve_chat_agent_and_profile
from .....agents.registry import has_capability, validate_agent_id
from .....agents.runtime_options import resolve_agent_runtime_options
from .....core import drafts as draft_utils
from .....core.managed_thread_identity import file_chat_target_key
from .....core.orchestration import (
    DeliveryIntentRef,
    TurnExecutionOrigin,
    TurnExecutionRequest,
)
from .....core.state import load_state
from .....core.utils import atomic_write
from ..agent_profile_validation import resolve_requested_agent_profile
from .draft_state import load_draft_snapshot, persist_draft, relative_to_repo
from .execution_agents import execute_app_server as _execute_app_server_impl
from .execution_agents import execute_harness_turn as _execute_harness_turn_impl
from .execution_agents import execute_opencode as _execute_opencode_impl
from .targets import FileChatTarget, build_file_chat_prompt, read_file

FILE_CHAT_TIMEOUT_SECONDS = 180
_FILE_CHAT_REQUIRED_CAPABILITIES = ("durable_threads", "message_turns")


@dataclass(frozen=True)
class FileChatAgentSelection:
    agent_id: str
    profile: Optional[str]
    thread_key: str


@dataclass(frozen=True)
class FileChatCanonicalTurn:
    request: TurnExecutionRequest
    selection: FileChatAgentSelection


def _normalize_optional_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _load_runner_state(request: Request) -> Any:
    try:
        state_path = request.app.state.engine.state_path
    except (AttributeError, TypeError):
        return None
    try:
        return load_state(state_path)
    except (OSError, ValueError, TypeError):
        return None


def build_file_chat_turn_request(
    request: Request,
    repo_root: Path,
    target: FileChatTarget,
    message: str,
    *,
    agent: object = "codex",
    profile: object = None,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    client_turn_id: Optional[str] = None,
    prompt_text: Optional[str] = None,
) -> FileChatCanonicalTurn:
    selection = resolve_file_chat_agent_selection(
        request,
        target,
        agent=agent,
        profile=profile,
    )
    runtime_options = resolve_agent_runtime_options(
        selection.agent_id,
        profile=selection.profile,
        state=_load_runner_state(request),
        config=getattr(request.app.state, "config", None),
        workspace_root=repo_root,
        explicit_model=model,
        explicit_reasoning=reasoning,
        approval_policy="on-request",
        sandbox_policy="dangerFullAccess",
        include_builtin_model=False,
    )
    request_id = client_turn_id or f"file-chat-{uuid.uuid4().hex}"
    turn_request = TurnExecutionRequest(
        request_id=request_id,
        target_id=target.state_key,
        target_kind="thread",
        workspace_root=str(repo_root),
        request_kind="message",
        busy_policy="reject",
        prompt_text=prompt_text or message,
        agent=selection.agent_id,
        profile=selection.profile,
        model=runtime_options.model,
        model_payload=runtime_options.opencode_model_payload or {},
        reasoning=runtime_options.reasoning,
        approval_policy=runtime_options.effective_approval_policy,
        approval_mode=runtime_options.effective_approval_policy,
        sandbox_policy=runtime_options.sandbox.policy,
        client_request_id=client_turn_id,
        idempotency_key=client_turn_id or request_id,
        correlation_id=client_turn_id or request_id,
        origin=TurnExecutionOrigin(
            kind="surface",
            source_id=f"web:file-chat:{target.state_key}",
            surface_kind="web",
            surface_key=target.state_key,
            metadata={"route": "file-chat", "target": target.target},
        ),
        metadata={
            "surface": "file_chat",
            "target": target.target,
            "chat_scope": target.chat_scope,
            "thread_key": selection.thread_key,
            "user_message": message,
        },
        delivery_intents=(
            DeliveryIntentRef(
                kind="file_chat_state",
                intent_id=client_turn_id or request_id,
                metadata={"target": target.target},
            ),
        ),
    )
    return FileChatCanonicalTurn(request=turn_request, selection=selection)


def resolve_file_chat_agent_selection(
    request: Request,
    target: FileChatTarget,
    *,
    agent: object = "codex",
    profile: object = None,
) -> FileChatAgentSelection:
    normalized_agent, normalized_profile = resolve_chat_agent_and_profile(
        agent,
        profile,
        default="codex",
        context=request.app.state,
    )
    try:
        agent_id = validate_agent_id(normalized_agent or "", request.app.state)
    except ValueError:
        agent_id = "codex"

    explicit_profile = _normalize_optional_text(profile)
    requested_profile = (
        normalized_profile if normalized_profile is not None else explicit_profile
    )
    validated_profile = resolve_requested_agent_profile(
        request,
        agent_id,
        requested_profile,
    )
    return FileChatAgentSelection(
        agent_id=agent_id,
        profile=validated_profile,
        thread_key=file_chat_target_key(agent_id, target.state_key, validated_profile),
    )


def _build_execution_context(
    request: Request,
) -> tuple[Any, Any, Any, Any, Optional[float]]:
    supervisor = getattr(request.app.state, "app_server_supervisor", None)
    threads = getattr(request.app.state, "app_server_threads", None)
    opencode = getattr(request.app.state, "opencode_supervisor", None)
    events = getattr(request.app.state, "app_server_events", None)
    engine = getattr(request.app.state, "engine", None)
    stall_timeout_seconds = None
    try:
        stall_timeout_seconds = (
            engine.config.opencode.session_stall_timeout_seconds
            if engine is not None
            else None
        )
    except (AttributeError, TypeError):
        stall_timeout_seconds = None
    return supervisor, threads, opencode, events, stall_timeout_seconds


def _missing_file_chat_capability(
    agent_id: str,
    *,
    context: Any,
) -> Optional[str]:
    for capability in _FILE_CHAT_REQUIRED_CAPABILITIES:
        if not has_capability(agent_id, capability, context):
            return capability
    return None


async def _update_file_chat_turn_state(
    request: Request,
    target: FileChatTarget,
    selection: FileChatAgentSelection,
    *,
    turn_request: Optional[TurnExecutionRequest] = None,
) -> None:
    from .runtime import update_turn_state

    turn_state_updates: dict[str, Any] = {
        "status": "running",
        "agent": selection.agent_id,
    }
    if selection.profile is not None:
        turn_state_updates["profile"] = selection.profile
    if turn_request is not None:
        turn_state_updates["turn_request"] = turn_request.to_dict()
    await update_turn_state(request, target, **turn_state_updates)


async def _execute_selected_agent(
    request: Request,
    repo_root: Path,
    prompt: str,
    interrupt_event: asyncio.Event,
    selection: FileChatAgentSelection,
    *,
    supervisor: Any,
    threads: Any,
    opencode: Any,
    events: Any,
    stall_timeout_seconds: Optional[float],
    model: Optional[str],
    model_payload: Optional[dict[str, Any]],
    reasoning: Optional[str],
    on_meta: Optional[Callable[[str, str, str], Any]],
    on_usage: Optional[Callable[[Dict[str, Any]], Any]],
) -> Dict[str, Any]:
    if selection.agent_id == "opencode":
        if opencode is None:
            return {"status": "error", "detail": "OpenCode supervisor unavailable"}
        return await execute_opencode(
            opencode,
            repo_root,
            prompt,
            interrupt_event,
            model=model,
            model_payload=(
                {
                    "providerID": str(model_payload.get("providerID") or ""),
                    "modelID": str(model_payload.get("modelID") or ""),
                }
                if model_payload
                else None
            ),
            reasoning=reasoning,
            thread_registry=threads,
            thread_key=selection.thread_key,
            stall_timeout_seconds=stall_timeout_seconds,
            on_meta=on_meta,
            on_usage=on_usage,
        )
    if selection.agent_id == "codex":
        if supervisor is None:
            return {"status": "error", "detail": "App-server supervisor unavailable"}
        return await execute_app_server(
            supervisor,
            repo_root,
            prompt,
            interrupt_event,
            agent_id=selection.agent_id,
            model=model,
            reasoning=reasoning,
            thread_registry=threads,
            thread_key=selection.thread_key,
            on_meta=on_meta,
            events=events,
        )
    missing_capability = _missing_file_chat_capability(
        selection.agent_id,
        context=request.app.state,
    )
    if missing_capability is not None:
        return {
            "status": "error",
            "detail": (
                f"Agent '{selection.agent_id}' does not support file-chat execution "
                f"(missing capability: {missing_capability})"
            ),
        }
    return await execute_harness_turn(
        request,
        repo_root,
        prompt,
        interrupt_event,
        agent_id=selection.agent_id,
        profile=selection.profile,
        model=model,
        reasoning=reasoning,
        thread_registry=threads,
        thread_key=selection.thread_key,
        on_meta=on_meta,
    )


def _build_file_chat_success_result(
    repo_root: Path,
    target: FileChatTarget,
    *,
    state: dict[str, Any],
    drafts: dict[str, Any],
    live_before: str,
    base_content: str,
    base_hash: str,
    created_at: str,
    draft_path: Path,
    agent_id: str,
    profile: Optional[str],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    live_after = read_file(target.path)
    after = read_file(draft_path)
    agent_message = result.get("agent_message", "File updated")
    response_text = result.get("message", agent_message)

    if live_after != live_before:
        atomic_write(target.path, live_before)

    response: Dict[str, Any] = {
        "status": "ok",
        "target": target.target,
        "agent": agent_id,
        "agent_message": agent_message,
        "message": response_text,
        "thread_id": result.get("thread_id"),
        "turn_id": result.get("turn_id"),
    }
    if profile is not None:
        response["profile"] = profile
    if result.get("raw_events"):
        response["raw_events"] = result.get("raw_events")

    if after == base_content:
        draft_utils.remove_draft(repo_root, target.state_key)
        response["has_draft"] = False
        return response

    draft = persist_draft(
        repo_root,
        target,
        state=state,
        drafts=drafts,
        content=after,
        base_content=base_content,
        base_hash=base_hash,
        created_at=created_at,
        agent_message=agent_message,
    )
    response.update(
        {
            "has_draft": True,
            "patch": draft["patch"],
            "content": after,
            "base_hash": base_hash,
            "created_at": draft["created_at"],
        }
    )
    return response


async def execute_file_chat(
    request: Request,
    repo_root: Path,
    target: FileChatTarget,
    message: str,
    *,
    agent: str = "codex",
    profile: Optional[str] = None,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    turn_request: Optional[TurnExecutionRequest] = None,
    on_meta: Optional[Callable[[str, str, str], Any]] = None,
    on_usage: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    supervisor, threads, opencode, events, stall_timeout_seconds = (
        _build_execution_context(request)
    )

    (
        state,
        drafts,
        live_before,
        before,
        base_content,
        base_hash,
        created_at,
        draft_path,
    ) = load_draft_snapshot(repo_root, target)
    prompt = build_file_chat_prompt(
        target=target,
        message=message,
        before=before,
        editable_rel_path=relative_to_repo(repo_root, draft_path),
    )
    if turn_request is None:
        canonical = build_file_chat_turn_request(
            request,
            repo_root,
            target,
            message,
            agent=agent,
            profile=profile,
            model=model,
            reasoning=reasoning,
            prompt_text=prompt,
        )
        turn_request = canonical.request
        selection = canonical.selection
    else:
        turn_request = replace(
            turn_request,
            prompt_text=prompt,
            metadata={**turn_request.metadata, "user_message": message},
        )
        selection = resolve_file_chat_agent_selection(
            request,
            target,
            agent=turn_request.agent,
            profile=turn_request.profile,
        )

    from .runtime import get_or_create_interrupt_event

    interrupt_event = await get_or_create_interrupt_event(request, target.state_key)
    if interrupt_event.is_set():
        return {"status": "interrupted", "detail": "File chat interrupted"}

    await _update_file_chat_turn_state(
        request,
        target,
        selection,
        turn_request=turn_request,
    )
    result = await _execute_selected_agent(
        request,
        repo_root,
        prompt,
        interrupt_event,
        selection,
        supervisor=supervisor,
        threads=threads,
        opencode=opencode,
        events=events,
        stall_timeout_seconds=stall_timeout_seconds,
        model=turn_request.model,
        model_payload=turn_request.model_payload,
        reasoning=turn_request.reasoning,
        on_meta=on_meta,
        on_usage=on_usage,
    )

    if result.get("status") != "ok":
        result = dict(result)
        result["turn_request"] = turn_request.to_dict()
        return result

    response = _build_file_chat_success_result(
        repo_root,
        target,
        state=state,
        drafts=drafts,
        live_before=live_before,
        base_content=base_content,
        base_hash=base_hash,
        created_at=created_at,
        draft_path=draft_path,
        agent_id=selection.agent_id,
        profile=selection.profile,
        result=result,
    )
    response["turn_request"] = turn_request.to_dict()
    return response


async def execute_app_server(
    supervisor: Any,
    repo_root: Path,
    prompt: str,
    interrupt_event: asyncio.Event,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    agent_id: str = "codex",
    thread_registry: Optional[Any] = None,
    thread_key: Optional[str] = None,
    on_meta: Optional[Callable[[str, str, str], Any]] = None,
    events: Optional[Any] = None,
) -> Dict[str, Any]:
    return await _execute_app_server_impl(
        supervisor,
        repo_root,
        prompt,
        interrupt_event,
        model=model,
        reasoning=reasoning,
        agent_id=agent_id,
        thread_registry=thread_registry,
        thread_key=thread_key,
        on_meta=on_meta,
        events=events,
        timeout_seconds=FILE_CHAT_TIMEOUT_SECONDS,
    )


async def execute_opencode(
    supervisor: Any,
    repo_root: Path,
    prompt: str,
    interrupt_event: asyncio.Event,
    *,
    model: Optional[str] = None,
    model_payload: Optional[dict[str, str]] = None,
    reasoning: Optional[str] = None,
    thread_registry: Optional[Any] = None,
    thread_key: Optional[str] = None,
    stall_timeout_seconds: Optional[float] = None,
    on_meta: Optional[Callable[[str, str, str], Any]] = None,
    on_usage: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    return await _execute_opencode_impl(
        supervisor,
        repo_root,
        prompt,
        interrupt_event,
        model=model,
        model_payload=model_payload,
        reasoning=reasoning,
        thread_registry=thread_registry,
        thread_key=thread_key,
        stall_timeout_seconds=stall_timeout_seconds,
        on_meta=on_meta,
        on_usage=on_usage,
        timeout_seconds=FILE_CHAT_TIMEOUT_SECONDS,
    )


async def execute_harness_turn(
    request: Request,
    repo_root: Path,
    prompt: str,
    interrupt_event: asyncio.Event,
    *,
    agent_id: str,
    profile: Optional[str],
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    thread_registry: Optional[Any] = None,
    thread_key: Optional[str] = None,
    on_meta: Optional[Callable[[str, str, str], Any]] = None,
) -> Dict[str, Any]:
    return await _execute_harness_turn_impl(
        request,
        repo_root,
        prompt,
        interrupt_event,
        agent_id=agent_id,
        profile=profile,
        model=model,
        reasoning=reasoning,
        thread_registry=thread_registry,
        thread_key=thread_key,
        on_meta=on_meta,
        timeout_seconds=FILE_CHAT_TIMEOUT_SECONDS,
    )


_FILE_CHAT_EXECUTION_API = execute_file_chat
