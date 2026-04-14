from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request

from .....agents.registry import (
    get_registered_agents,
    resolve_agent_runtime,
    wrap_requested_agent_context,
)
from .....core.orchestration import FreshConversationRequiredError
from .....core.usage import persist_opencode_usage_snapshot
from .....integrations.app_server import is_missing_thread_error

logger = logging.getLogger(__name__)


class FileChatError(Exception):
    """Base error for file chat failures."""


def parse_agent_message(output: str) -> str:
    text = (output or "").strip()
    if not text:
        return "File updated via chat."
    for line in text.splitlines():
        if line.lower().startswith("agent:"):
            return line[len("agent:") :].strip() or "File updated via chat."
    first_line = text.splitlines()[0].strip()
    return (first_line[:97] + "...") if len(first_line) > 100 else first_line


def _requires_fresh_file_chat_conversation(exc: Exception) -> bool:
    if isinstance(exc, FreshConversationRequiredError):
        return True
    return is_missing_thread_error(exc)


def _build_runtime_harness(
    request: Request, agent_id: str, profile: Optional[str] = None
) -> Any:
    descriptors = get_registered_agents(request.app.state)
    resolution = resolve_agent_runtime(agent_id, profile, context=request.app.state)
    effective_agent_id = resolution.runtime_agent_id
    effective_profile = resolution.runtime_profile
    descriptor = descriptors.get(effective_agent_id)
    if descriptor is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown agent: {effective_agent_id}",
        )
    try:
        return descriptor.make_harness(
            wrap_requested_agent_context(
                request.app.state,
                agent_id=effective_agent_id,
                profile=effective_profile,
            )
        )
    except (
        RuntimeError,
        TypeError,
        ValueError,
        AttributeError,
        OSError,
        ConnectionError,
    ) as exc:  # intentional: harness construction may fail arbitrarily
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
    timeout_seconds: float = 180,
) -> Dict[str, Any]:
    harness = _build_runtime_harness(request, agent_id, profile)
    if not callable(getattr(harness, "supports", None)):
        raise FileChatError("Runtime harness unavailable")
    if not harness.supports("durable_threads") or not harness.supports("message_turns"):
        raise FileChatError(
            f"Agent '{agent_id}' does not support file-chat message turns"
        )

    await harness.ensure_ready(repo_root)

    conversation_id = None
    if thread_registry is not None and thread_key:
        conversation_id = thread_registry.get_thread_id(thread_key)
    had_existing_conversation = bool(conversation_id)

    if conversation_id:
        try:
            resumed = await harness.resume_conversation(repo_root, conversation_id)
        except (
            Exception
        ) as exc:  # intentional: resume may fail; fallback to new conversation
            if _requires_fresh_file_chat_conversation(exc) or isinstance(
                exc,
                (
                    OSError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    AttributeError,
                    ConnectionError,
                ),
            ):
                conversation_id = None
            else:
                raise
        else:
            resumed_id = str(getattr(resumed, "id", "") or "").strip()
            if resumed_id:
                conversation_id = resumed_id
                had_existing_conversation = True

    async def _create_fresh_conversation() -> str:
        conversation = await harness.new_conversation(repo_root, title="File chat")
        fresh_conversation_id = str(getattr(conversation, "id", "") or "").strip()
        if not fresh_conversation_id:
            raise FileChatError("Runtime did not return a conversation id")
        return fresh_conversation_id

    if not conversation_id:
        conversation_id = await _create_fresh_conversation()

    if thread_registry is not None and thread_key and conversation_id:
        thread_registry.set_thread_id(thread_key, conversation_id)

    try:
        turn = await harness.start_turn(
            repo_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode="on-request",
            sandbox_policy="dangerFullAccess",
        )
    except Exception as exc:  # intentional: retry stale durable-thread failures once
        if not had_existing_conversation or not _requires_fresh_file_chat_conversation(
            exc
        ):
            raise
        if thread_registry is not None and thread_key:
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                thread_registry.reset_thread(thread_key)
        conversation_id = await _create_fresh_conversation()
        if thread_registry is not None and thread_key:
            thread_registry.set_thread_id(thread_key, conversation_id)
        turn = await harness.start_turn(
            repo_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode="on-request",
            sandbox_policy="dangerFullAccess",
        )

    resolved_conversation_id = str(
        getattr(turn, "conversation_id", None) or conversation_id or ""
    ).strip()
    if not resolved_conversation_id:
        raise FileChatError("Runtime did not return a conversation id")
    turn_id = str(getattr(turn, "turn_id", "") or "").strip()
    if not turn_id:
        raise FileChatError("Runtime did not return a turn id")

    if thread_registry is not None and thread_key:
        thread_registry.set_thread_id(thread_key, resolved_conversation_id)

    if on_meta is not None:
        try:
            maybe = on_meta(agent_id, resolved_conversation_id, turn_id)
            if asyncio.iscoroutine(maybe):
                await maybe
        except (
            RuntimeError,
            ValueError,
            TypeError,
            ConnectionError,
            OSError,
        ):  # intentional: non-critical callback
            logger.debug("file chat meta callback failed", exc_info=True)

    if interrupt_event.is_set():
        with contextlib.suppress(
            RuntimeError,
            OSError,
            BrokenPipeError,
            ProcessLookupError,
            ConnectionResetError,
        ):
            await harness.interrupt(repo_root, resolved_conversation_id, turn_id)
        return {"status": "interrupted", "detail": "File chat interrupted"}

    turn_task = asyncio.create_task(
        harness.wait_for_turn(
            repo_root,
            resolved_conversation_id,
            turn_id,
            timeout=None,
        )
    )
    timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
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
            with contextlib.suppress(
                RuntimeError,
                OSError,
                BrokenPipeError,
                ProcessLookupError,
                ConnectionResetError,
            ):
                await harness.interrupt(repo_root, resolved_conversation_id, turn_id)
            return {"status": "interrupted", "detail": "File chat interrupted"}
        turn_result = await turn_task
    finally:
        timeout_task.cancel()
        interrupt_task.cancel()

    if getattr(turn_result, "errors", None):
        errors = turn_result.errors
        raise FileChatError(errors[-1] if errors else "Runtime error")

    output = str(getattr(turn_result, "assistant_text", "") or "").strip()
    return {
        "status": "ok",
        "agent_message": parse_agent_message(output),
        "message": output,
        "raw_events": list(getattr(turn_result, "raw_events", []) or []),
        "thread_id": resolved_conversation_id,
        "turn_id": turn_id,
        "agent": agent_id,
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
    timeout_seconds: float = 180,
) -> Dict[str, Any]:
    client = await supervisor.get_client(repo_root)

    thread_id = None
    if thread_registry is not None and thread_key:
        thread_id = thread_registry.get_thread_id(thread_key)
    if thread_id:
        try:
            await client.thread_resume(thread_id)
        except (
            Exception
        ):  # intentional: thread_resume may fail with various client/network errors; fallback to new thread
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
        except (
            Exception
        ):  # intentional: register_turn is best-effort non-critical observability
            logger.debug("file chat register_turn failed", exc_info=True)
    if on_meta is not None:
        try:
            maybe = on_meta(agent_id, thread_id, handle.turn_id)
            if asyncio.iscoroutine(maybe):
                await maybe
        except (
            Exception
        ):  # intentional: user-provided callback must not crash execution
            logger.debug("file chat meta callback failed", exc_info=True)

    turn_task = asyncio.create_task(handle.wait(timeout=None))
    timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
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

    output = str(getattr(turn_result, "final_message", "") or "").strip()
    if not output:
        output = "\n".join(getattr(turn_result, "agent_messages", []) or []).strip()
    return {
        "status": "ok",
        "agent_message": parse_agent_message(output),
        "message": output,
        "raw_events": getattr(turn_result, "raw_events", []) or [],
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
    timeout_seconds: float = 180,
) -> Dict[str, Any]:
    from .....agents.opencode.runtime import (
        PERMISSION_ALLOW,
        build_turn_id,
        collect_opencode_output,
        extract_session_id,
        opencode_stream_timeouts,
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
        except (
            Exception
        ):  # intentional: user-provided callback must not crash execution
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
            except (
                Exception
            ):  # intentional: user-provided callback must not crash execution
                logger.debug("file chat usage handler failed", exc_info=True)

    ready_event = asyncio.Event()
    stall_timeout, first_event_timeout = opencode_stream_timeouts(
        stall_timeout_seconds,
    )
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
            stall_timeout_seconds=stall_timeout,
            first_event_timeout_seconds=first_event_timeout,
            logger=logger,
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
    timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
    interrupt_task = asyncio.create_task(interrupt_event.wait())
    try:
        prompt_response = None
        try:
            prompt_response = await prompt_task
        except (
            Exception
        ) as exc:  # intentional: rewrap any client failure as FileChatError
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
    return {
        "status": "ok",
        "agent_message": parse_agent_message(output_result.text),
        "message": output_result.text,
        "thread_id": session_id,
        "turn_id": turn_id,
        "agent": "opencode",
        **({"usage_parts": usage_parts} if usage_parts else {}),
    }


__all__ = [
    "FileChatError",
    "execute_app_server",
    "execute_harness_turn",
    "execute_opencode",
]
