from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from fastapi import HTTPException, Request

from .....agents.registry import validate_agent_id
from .....core.state import now_iso
from .....core.usage import persist_opencode_usage_snapshot
from .....core.utils import atomic_write
from .targets import (
    _Target,
    build_file_chat_prompt,
    build_patch,
    parse_target,
    read_file,
    _resolve_repo_root,
    _hash_content,
    _load_state,
    _save_state,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

FILE_CHAT_TIMEOUT_SECONDS = 180


class FileChatError(Exception):
    """Base error for file chat failures."""


async def execute_file_chat(
    request: Request,
    repo_root: Path,
    target: _Target,
    message: str,
    *,
    agent: str = "codex",
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    on_meta: Optional[Callable[[str, str, str], Any]] = None,
    on_usage: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    supervisor = getattr(request.app.state, "app_server_supervisor", None)
    threads = getattr(request.app.state, "app_server_threads", None)
    opencode = getattr(request.app.state, "opencode_supervisor", None)
    engine = getattr(request.app.state, "engine", None)
    events = getattr(request.app.state, "app_server_events", None)
    stall_timeout_seconds = None
    try:
        stall_timeout_seconds = (
            engine.config.opencode.session_stall_timeout_seconds
            if engine is not None
            else None
        )
    except Exception:
        stall_timeout_seconds = None
    if supervisor is None and opencode is None:
        raise FileChatError("No agent supervisor available for file chat")

    before = read_file(target.path)
    base_hash = _hash_content(before)

    prompt = build_file_chat_prompt(target=target, message=message, before=before)

    from .runtime import get_or_create_interrupt_event

    interrupt_event = await get_or_create_interrupt_event(request, target.state_key)
    if interrupt_event.is_set():
        return {"status": "interrupted", "detail": "File chat interrupted"}

    try:
        agent_id = validate_agent_id(agent or "")
    except ValueError:
        agent_id = "codex"

    thread_key = f"file_chat.{target.state_key}"

    from .runtime import update_turn_state

    await update_turn_state(request, target, status="running", agent=agent_id)

    if agent_id == "opencode":
        if opencode is None:
            return {"status": "error", "detail": "OpenCode supervisor unavailable"}
        result = await execute_opencode(
            opencode,
            repo_root,
            prompt,
            interrupt_event,
            model=model,
            reasoning=reasoning,
            thread_registry=threads,
            thread_key=thread_key,
            stall_timeout_seconds=stall_timeout_seconds,
            on_meta=on_meta,
            on_usage=on_usage,
        )
    else:
        if supervisor is None:
            return {
                "status": "error",
                "detail": "App-server supervisor unavailable",
            }
        result = await execute_app_server(
            supervisor,
            repo_root,
            prompt,
            interrupt_event,
            agent_id=agent_id,
            model=model,
            reasoning=reasoning,
            thread_registry=threads,
            thread_key=thread_key,
            on_meta=on_meta,
            events=events,
        )

    if result.get("status") != "ok":
        return result

    after = read_file(target.path)

    if after != before:
        atomic_write(target.path, before)

    agent_message = result.get("agent_message", "File updated")
    response_text = result.get("message", agent_message)

    if after != before:
        patch = build_patch(target.rel_path, before, after)
        state = _load_state(repo_root)
        drafts = (
            state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
        )
        drafts[target.state_key] = {
            "content": after,
            "patch": patch,
            "agent_message": agent_message,
            "created_at": now_iso(),
            "base_hash": base_hash,
            "target": target.target,
            "rel_path": target.rel_path,
        }
        state["drafts"] = drafts
        _save_state(repo_root, state)
        return {
            "status": "ok",
            "target": target.target,
            "agent": agent_id,
            "agent_message": agent_message,
            "message": response_text,
            "has_draft": True,
            "patch": patch,
            "content": after,
            "base_hash": base_hash,
            "created_at": drafts[target.state_key]["created_at"],
            "thread_id": result.get("thread_id"),
            "turn_id": result.get("turn_id"),
            **(
                {"raw_events": result.get("raw_events")}
                if result.get("raw_events")
                else {}
            ),
        }

    return {
        "status": "ok",
        "target": target.target,
        "agent": agent_id,
        "agent_message": agent_message,
        "message": response_text,
        "has_draft": False,
        "thread_id": result.get("thread_id"),
        "turn_id": result.get("turn_id"),
        **(
            {"raw_events": result.get("raw_events")} if result.get("raw_events") else {}
        ),
    }


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
    client = await supervisor.get_client(repo_root)

    thread_id = None
    if thread_registry is not None and thread_key:
        thread_id = thread_registry.get_thread_id(thread_key)
    if thread_id:
        try:
            await client.thread_resume(thread_id)
        except Exception:
            thread_id = None

    if not thread_id:
        thread = await client.thread_start(str(repo_root))
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise FileChatError("App-server did not return a thread id")
        if thread_registry is not None and thread_key:
            thread_registry.set_thread_id(thread_key, thread_id)

    turn_kwargs: Dict[str, Any] = {}
    if model:
        turn_kwargs["model"] = model
    if reasoning:
        turn_kwargs["effort"] = reasoning

    handle = await client.turn_start(
        thread_id,
        prompt,
        approval_policy="on-request",
        sandbox_policy="dangerFullAccess",
        **turn_kwargs,
    )
    if events is not None:
        try:
            await events.register_turn(thread_id, handle.turn_id)
        except Exception:
            logger.debug("file chat register_turn failed", exc_info=True)
    if on_meta is not None:
        try:
            maybe = on_meta(agent_id, thread_id, handle.turn_id)
            if asyncio.iscoroutine(maybe):
                await maybe
        except Exception:
            logger.debug("file chat meta callback failed", exc_info=True)

    turn_task = asyncio.create_task(handle.wait(timeout=None))
    timeout_task = asyncio.create_task(asyncio.sleep(FILE_CHAT_TIMEOUT_SECONDS))
    interrupt_task = asyncio.create_task(interrupt_event.wait())
    try:
        done, _ = await asyncio.wait(
            {turn_task, timeout_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_task in done:
            turn_task.cancel()
            return {"status": "error", "detail": "File chat timed out"}
        if interrupt_task in done:
            turn_task.cancel()
            return {"status": "interrupted", "detail": "File chat interrupted"}
        turn_result = await turn_task
    finally:
        timeout_task.cancel()
        interrupt_task.cancel()

    if getattr(turn_result, "errors", None):
        errors = turn_result.errors
        raise FileChatError(errors[-1] if errors else "App-server error")

    output = "\n".join(getattr(turn_result, "agent_messages", []) or []).strip()
    agent_message = parse_agent_message(output)
    raw_events = getattr(turn_result, "raw_events", []) or []
    return {
        "status": "ok",
        "agent_message": agent_message,
        "message": output,
        "raw_events": raw_events,
        "thread_id": thread_id,
        "turn_id": getattr(handle, "turn_id", None),
        "agent": agent_id,
    }


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
    from .....agents.opencode.runtime import (
        PERMISSION_ALLOW,
        build_turn_id,
        collect_opencode_output,
        extract_session_id,
        parse_message_response,
        split_model_id,
    )

    client = await supervisor.get_client(repo_root)
    session_id = None
    if thread_registry is not None and thread_key:
        session_id = thread_registry.get_thread_id(thread_key)
    if not session_id:
        session = await client.create_session(directory=str(repo_root))
        session_id = extract_session_id(session, allow_fallback_id=True)
        if not isinstance(session_id, str) or not session_id:
            raise FileChatError("OpenCode did not return a session id")
        if thread_registry is not None and thread_key:
            thread_registry.set_thread_id(thread_key, session_id)

    turn_id = build_turn_id(session_id)
    if on_meta is not None:
        try:
            maybe = on_meta("opencode", session_id, turn_id)
            if asyncio.iscoroutine(maybe):
                await maybe
        except Exception:
            logger.debug("file chat opencode meta failed", exc_info=True)

    model_payload = split_model_id(model)
    await supervisor.mark_turn_started(repo_root)

    usage_parts: list[Dict[str, Any]] = []

    async def _part_handler(
        part_type: str, part: Any, turn_id_arg: Optional[str] | None
    ) -> None:
        if part_type == "usage" and on_usage is not None:
            usage_parts.append(part)
            try:
                maybe = on_usage(part)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception:
                logger.debug("file chat usage handler failed", exc_info=True)

    ready_event = asyncio.Event()
    output_task = asyncio.create_task(
        collect_opencode_output(
            client,
            session_id=session_id,
            workspace_path=str(repo_root),
            model_payload=model_payload,
            permission_policy=PERMISSION_ALLOW,
            question_policy="auto_first_option",
            should_stop=interrupt_event.is_set,
            ready_event=ready_event,
            part_handler=_part_handler,
            stall_timeout_seconds=stall_timeout_seconds,
        )
    )
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(ready_event.wait(), timeout=2.0)

    prompt_task = asyncio.create_task(
        client.prompt_async(
            session_id,
            message=prompt,
            model=model_payload,
            variant=reasoning,
        )
    )
    timeout_task = asyncio.create_task(asyncio.sleep(FILE_CHAT_TIMEOUT_SECONDS))
    interrupt_task = asyncio.create_task(interrupt_event.wait())
    try:
        prompt_response = None
        try:
            prompt_response = await prompt_task
        except Exception as exc:
            interrupt_event.set()
            output_task.cancel()
            raise FileChatError(f"OpenCode prompt failed: {exc}") from exc

        done, _ = await asyncio.wait(
            {output_task, timeout_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_task in done:
            output_task.cancel()
            return {"status": "error", "detail": "File chat timed out"}
        if interrupt_task in done:
            output_task.cancel()
            return {"status": "interrupted", "detail": "File chat interrupted"}
        output_result = await output_task
        if (not output_result.text) and prompt_response is not None:
            fallback = parse_message_response(prompt_response)
            if fallback.text:
                output_result = type(output_result)(
                    text=fallback.text,
                    error=output_result.error or fallback.error,
                    usage=output_result.usage,
                )
    finally:
        timeout_task.cancel()
        interrupt_task.cancel()
        await supervisor.mark_turn_finished(repo_root)

    if output_result.usage:
        persist_opencode_usage_snapshot(
            repo_root,
            session_id=session_id,
            turn_id=turn_id,
            usage=output_result.usage,
            source="live_stream",
        )
    if output_result.error:
        raise FileChatError(output_result.error)
    agent_message = parse_agent_message(output_result.text)
    result = {
        "status": "ok",
        "agent_message": agent_message,
        "message": output_result.text,
        "thread_id": session_id,
        "turn_id": turn_id,
        "agent": "opencode",
    }
    if usage_parts:
        result["usage_parts"] = usage_parts
    return result


def parse_agent_message(output: str) -> str:
    text = (output or "").strip()
    if not text:
        return "File updated via chat."
    for line in text.splitlines():
        if line.lower().startswith("agent:"):
            return line[len("agent:") :].strip() or "File updated via chat."
    first_line = text.splitlines()[0].strip()
    return (first_line[:97] + "...") if len(first_line) > 100 else first_line
