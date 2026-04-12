from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import Request

from .....agents.registry import validate_agent_id
from .....core import drafts as draft_utils
from .....core.utils import atomic_write
from .....integrations.app_server.threads import file_chat_target_key
from .....integrations.chat.agents import resolve_chat_agent_and_profile
from ..agent_profile_validation import resolve_requested_agent_profile
from .draft_state import load_draft_snapshot, persist_draft, relative_to_repo
from .execution_agents import FileChatError
from .execution_agents import execute_app_server as _execute_app_server_impl
from .execution_agents import execute_harness_turn as _execute_harness_turn_impl
from .execution_agents import execute_opencode as _execute_opencode_impl
from .targets import _Target, build_file_chat_prompt, read_file

FILE_CHAT_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class FileChatAgentSelection:
    agent_id: str
    profile: Optional[str]
    thread_key: str


def _normalize_optional_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def resolve_file_chat_agent_selection(
    request: Request,
    target: _Target,
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
    if supervisor is None and opencode is None:
        raise FileChatError("No agent supervisor available for file chat")
    return supervisor, threads, opencode, events, stall_timeout_seconds


async def _update_file_chat_turn_state(
    request: Request, target: _Target, selection: FileChatAgentSelection
) -> None:
    from .runtime import update_turn_state

    turn_state_updates: dict[str, Any] = {
        "status": "running",
        "agent": selection.agent_id,
    }
    if selection.profile is not None:
        turn_state_updates["profile"] = selection.profile
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
    if selection.agent_id == "hermes":
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
    return {
        "status": "error",
        "detail": (
            f"Agent '{selection.agent_id}' is not supported on file-chat surface yet"
        ),
    }


def _build_file_chat_success_result(
    repo_root: Path,
    target: _Target,
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
    target: _Target,
    message: str,
    *,
    agent: str = "codex",
    profile: Optional[str] = None,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
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

    from .runtime import get_or_create_interrupt_event

    interrupt_event = await get_or_create_interrupt_event(request, target.state_key)
    if interrupt_event.is_set():
        return {"status": "interrupted", "detail": "File chat interrupted"}

    selection = resolve_file_chat_agent_selection(
        request,
        target,
        agent=agent,
        profile=profile,
    )
    await _update_file_chat_turn_state(request, target, selection)
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
        model=model,
        reasoning=reasoning,
        on_meta=on_meta,
        on_usage=on_usage,
    )

    if result.get("status") != "ok":
        return result

    return _build_file_chat_success_result(
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
