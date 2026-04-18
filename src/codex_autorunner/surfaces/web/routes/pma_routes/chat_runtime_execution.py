from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request

from .....agents.base import (
    harness_allows_parallel_event_stream,
    harness_progress_event_stream,
)
from .....agents.codex.harness import CodexHarness
from .....agents.opencode.harness import OpenCodeHarness
from .....agents.registry import (
    get_registered_agents,
    resolve_agent_runtime,
    wrap_requested_agent_context,
)
from .....core.orchestration import FreshConversationRequiredError
from .....core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    merge_runtime_thread_raw_events,
    normalize_runtime_thread_message,
    normalize_runtime_thread_raw_event,
)
from .....core.orchestration.turn_timeline import iso_from_epoch_millis
from .....core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    Completed,
    OutputDelta,
    RunEvent,
)
from .....core.time_utils import now_iso
from .....integrations.app_server import is_missing_thread_error

logger = logging.getLogger(__name__)

DEFAULT_PMA_TIMEOUT_SECONDS = 7200
SUCCESSFUL_COMPLETION_STATUSES = frozenset(
    {"ok", "completed", "complete", "done", "success"}
)


def requires_fresh_pma_conversation(exc: Exception) -> bool:
    if isinstance(exc, FreshConversationRequiredError):
        return True
    return is_missing_thread_error(exc)


def cancel_background_task(task: asyncio.Task[Any], *, name: str) -> None:
    def _on_done(done_task: asyncio.Future[Any]) -> None:
        if isinstance(done_task, asyncio.Task):
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("PMA task failed: %s", name)

    if task.done():
        _on_done(task)
        return

    task.add_done_callback(_on_done)
    task.cancel()


def timeline_has_assistant_output(events: list[RunEvent]) -> bool:
    return any(
        isinstance(event, OutputDelta)
        and event.delta_type
        in {
            RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
            RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
        }
        for event in events
    )


def raw_events_show_completion(raw_events: tuple[Any, ...]) -> bool:
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        method = str(raw_event.get("method") or "").strip().lower()
        if not method:
            message = raw_event.get("message")
            if isinstance(message, dict):
                method = str(message.get("method") or "").strip().lower()
        if method in {
            "turn/completed",
            "prompt/completed",
            "session.idle",
        }:
            return True
    return False


def build_runtime_harness(
    request: Request, agent_id: str, profile: Optional[str] = None
) -> Any:
    try:
        descriptors = get_registered_agents(request.app.state)
    except TypeError as exc:
        if "positional argument" not in str(exc):
            raise
        descriptors = get_registered_agents()
    resolution = resolve_agent_runtime(agent_id, profile, context=request.app.state)
    effective_agent_id = resolution.runtime_agent_id
    effective_profile = resolution.runtime_profile
    descriptor = descriptors.get(effective_agent_id)
    if descriptor is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown agent: {effective_agent_id}"
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
    ) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def timeline_from_raw_events(
    raw_events: tuple[Any, ...],
) -> tuple[list[RunEvent], RuntimeThreadRunEventState]:
    state = RuntimeThreadRunEventState()
    timeline_events: list[RunEvent] = []
    for raw_event in raw_events:
        timeline_events.extend(
            await normalize_runtime_thread_raw_event(
                raw_event, state, timestamp=now_iso()
            )
        )
    return timeline_events, state


async def execute_harness_turn(
    harness: Any,
    hub_root: Path,
    prompt: str,
    interrupt_event: asyncio.Event,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    backend_thread_id: Optional[str] = None,
    thread_registry: Optional[Any] = None,
    thread_key: Optional[str] = None,
    on_meta: Optional[Any] = None,
    timeout_seconds: Optional[float] = None,
) -> dict[str, Any]:
    await harness.ensure_ready(hub_root)
    resolved_timeout_seconds = float(
        timeout_seconds if timeout_seconds is not None else DEFAULT_PMA_TIMEOUT_SECONDS
    )

    conversation_id = backend_thread_id
    if conversation_id is None and thread_registry is not None and thread_key:
        conversation_id = thread_registry.get_thread_id(thread_key)
    had_existing_conversation = bool(conversation_id)

    if conversation_id:
        try:
            resumed = await harness.resume_conversation(hub_root, conversation_id)
        except Exception as exc:
            if requires_fresh_pma_conversation(exc) or isinstance(
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
            resolved_conversation_id = getattr(resumed, "id", None)
            if isinstance(resolved_conversation_id, str) and resolved_conversation_id:
                conversation_id = resolved_conversation_id
                had_existing_conversation = True

    async def _create_fresh_conversation() -> str:
        conversation = await harness.new_conversation(hub_root, title="PMA")
        fresh_conversation_id = str(getattr(conversation, "id", "") or "").strip()
        if not fresh_conversation_id:
            raise HTTPException(
                status_code=502,
                detail="Runtime did not return a conversation id",
            )
        return fresh_conversation_id

    if not conversation_id:
        conversation_id = await _create_fresh_conversation()

    if thread_registry is not None and thread_key and conversation_id:
        thread_registry.set_thread_id(thread_key, conversation_id)

    try:
        turn = await harness.start_turn(
            hub_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode="on-request",
            sandbox_policy="dangerFullAccess",
        )
    except Exception as exc:
        if not had_existing_conversation or not requires_fresh_pma_conversation(exc):
            raise
        if thread_registry is not None and thread_key:
            try:
                thread_registry.reset_thread(thread_key)
            except OSError:
                logger.debug(
                    "Failed to clear stale PMA conversation binding for %s",
                    thread_key,
                    exc_info=True,
                )
        conversation_id = await _create_fresh_conversation()
        if thread_registry is not None and thread_key:
            thread_registry.set_thread_id(thread_key, conversation_id)
        turn = await harness.start_turn(
            hub_root,
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
        raise HTTPException(
            status_code=502,
            detail="Runtime did not return a conversation id",
        )
    turn_id = str(getattr(turn, "turn_id", "") or "").strip()
    if not turn_id:
        raise HTTPException(status_code=502, detail="Runtime did not return a turn id")

    if thread_registry is not None and thread_key:
        thread_registry.set_thread_id(thread_key, resolved_conversation_id)

    if on_meta is not None:
        try:
            maybe = on_meta(resolved_conversation_id, turn_id)
            if asyncio.iscoroutine(maybe):
                await maybe
        except (
            RuntimeError,
            ValueError,
            TypeError,
            ConnectionError,
            OSError,
        ):
            logger.exception("pma meta callback failed")

    if interrupt_event.is_set():
        try:
            await harness.interrupt(hub_root, resolved_conversation_id, turn_id)
        except (
            RuntimeError,
            OSError,
            BrokenPipeError,
            ProcessLookupError,
            ConnectionResetError,
        ):
            logger.exception("Failed to interrupt PMA runtime turn")
        return {"status": "interrupted", "detail": "PMA chat interrupted"}

    streamed_raw_events: list[Any] = []
    stream_task: Optional[asyncio.Task[None]] = None
    if harness_allows_parallel_event_stream(harness):

        async def _collect_events() -> None:
            async for raw_event in harness_progress_event_stream(
                harness,
                hub_root,
                resolved_conversation_id,
                turn_id,
            ):
                streamed_raw_events.append(raw_event)

        stream_task = asyncio.create_task(_collect_events())

    turn_task = asyncio.create_task(
        harness.wait_for_turn(
            hub_root,
            resolved_conversation_id,
            turn_id,
            timeout=None,
        )
    )
    timeout_task = asyncio.create_task(asyncio.sleep(resolved_timeout_seconds))
    interrupt_task = asyncio.create_task(interrupt_event.wait())
    try:
        done, _ = await asyncio.wait(
            {turn_task, timeout_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_task in done:
            try:
                await harness.interrupt(hub_root, resolved_conversation_id, turn_id)
            except (
                RuntimeError,
                OSError,
                BrokenPipeError,
                ProcessLookupError,
                ConnectionResetError,
            ):
                logger.exception("Failed to interrupt PMA runtime turn")
            cancel_background_task(turn_task, name="pma.runtime.turn.wait")
            return {"status": "error", "detail": "PMA chat timed out"}
        if interrupt_task in done:
            try:
                await harness.interrupt(hub_root, resolved_conversation_id, turn_id)
            except (
                RuntimeError,
                OSError,
                BrokenPipeError,
                ProcessLookupError,
                ConnectionResetError,
            ):
                logger.exception("Failed to interrupt PMA runtime turn")
            cancel_background_task(turn_task, name="pma.runtime.turn.wait")
            return {"status": "interrupted", "detail": "PMA chat interrupted"}
        turn_result = await turn_task
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        cancel_background_task(timeout_task, name="pma.runtime.timeout.wait")
        cancel_background_task(interrupt_task, name="pma.runtime.interrupt.wait")
        if stream_task is not None:
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass

    raw_events = tuple(getattr(turn_result, "raw_events", ()) or ())
    merged_raw_events = tuple(
        merge_runtime_thread_raw_events(streamed_raw_events, raw_events)
    )
    timeline_events, timeline_state = await timeline_from_raw_events(merged_raw_events)

    assistant_text = str(getattr(turn_result, "assistant_text", "") or "").strip()
    if not assistant_text:
        assistant_text = timeline_state.best_assistant_text().strip()

    status = str(getattr(turn_result, "status", "") or "").strip().lower()
    errors = tuple(getattr(turn_result, "errors", ()) or ())
    successful_completion = status in SUCCESSFUL_COMPLETION_STATUSES
    if errors:
        detail = next(
            (str(error or "").strip() for error in errors if str(error or "").strip()),
            "",
        )
        if (
            assistant_text
            and (successful_completion or timeline_state.completed_seen)
            and (
                timeline_state.completed_seen
                or raw_events_show_completion(merged_raw_events)
            )
        ):
            timeline_events.append(
                Completed(timestamp=now_iso(), final_message=assistant_text)
            )
            return {
                "status": "ok",
                "message": assistant_text,
                "thread_id": resolved_conversation_id,
                "backend_thread_id": resolved_conversation_id,
                "turn_id": turn_id,
                "raw_events": list(merged_raw_events),
                "timeline_events": timeline_events,
            }
        return {"status": "error", "detail": detail or "PMA chat failed"}

    if status in {"interrupted", "cancelled", "canceled", "aborted"}:
        return {"status": "interrupted", "detail": "PMA chat interrupted"}
    if status and not successful_completion:
        return {"status": "error", "detail": "PMA chat failed"}

    if assistant_text:
        timeline_events.append(
            Completed(timestamp=now_iso(), final_message=assistant_text)
        )
    return {
        "status": "ok",
        "message": assistant_text,
        "thread_id": resolved_conversation_id,
        "backend_thread_id": resolved_conversation_id,
        "turn_id": turn_id,
        "raw_events": list(merged_raw_events),
        "timeline_events": timeline_events,
    }


async def execute_app_server(
    supervisor: Any,
    events: Any,
    hub_root: Path,
    prompt: str,
    interrupt_event: asyncio.Event,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    backend_thread_id: Optional[str] = None,
    thread_registry: Optional[Any] = None,
    thread_key: Optional[str] = None,
    on_meta: Optional[Any] = None,
    timeout_seconds: Optional[float] = None,
) -> dict[str, Any]:
    resolved_timeout_seconds = float(
        timeout_seconds if timeout_seconds is not None else DEFAULT_PMA_TIMEOUT_SECONDS
    )
    client = await supervisor.get_client(hub_root)

    if backend_thread_id:
        thread_id = backend_thread_id
    elif thread_registry is not None and thread_key:
        thread_id = thread_registry.get_thread_id(thread_key)
    else:
        thread_id = None
    if thread_id:
        try:
            await client.thread_resume(thread_id)
        except (
            OSError,
            RuntimeError,
            ValueError,
        ):
            thread_id = None

    if not thread_id:
        thread = await client.thread_start(str(hub_root))
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise HTTPException(
                status_code=502, detail="App-server did not return a thread id"
            )
        if thread_registry is not None and thread_key:
            thread_registry.set_thread_id(thread_key, thread_id)

    turn_kwargs: dict[str, Any] = {}
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
    register_turn = getattr(events, "register_turn", None)
    if callable(register_turn):
        try:
            await register_turn(thread_id, handle.turn_id)
        except (
            OSError,
            RuntimeError,
            ValueError,
        ):
            logger.debug("pma chat register_turn failed", exc_info=True)
    codex_harness = CodexHarness(supervisor, events)
    if on_meta is not None:
        try:
            maybe = on_meta(thread_id, handle.turn_id)
            if asyncio.iscoroutine(maybe):
                await maybe
        except (
            RuntimeError,
            ValueError,
            TypeError,
            ConnectionError,
            OSError,
        ):
            logger.exception("pma meta callback failed")

    if interrupt_event.is_set():
        try:
            await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
        except (
            RuntimeError,
            OSError,
            BrokenPipeError,
            ProcessLookupError,
            ConnectionResetError,
        ):
            logger.exception("Failed to interrupt Codex turn")
        return {"status": "interrupted", "detail": "PMA chat interrupted"}

    turn_task = asyncio.create_task(handle.wait(timeout=None))
    timeout_task = asyncio.create_task(asyncio.sleep(resolved_timeout_seconds))
    interrupt_task = asyncio.create_task(interrupt_event.wait())
    try:
        done, _ = await asyncio.wait(
            {turn_task, timeout_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_task in done:
            try:
                await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
            except (
                RuntimeError,
                OSError,
                BrokenPipeError,
                ProcessLookupError,
                ConnectionResetError,
            ):
                logger.exception("Failed to interrupt Codex turn")
            cancel_background_task(turn_task, name="pma.app_server.turn.wait")
            return {"status": "error", "detail": "PMA chat timed out"}
        if interrupt_task in done:
            try:
                await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
            except (
                RuntimeError,
                OSError,
                BrokenPipeError,
                ProcessLookupError,
                ConnectionResetError,
            ):
                logger.exception("Failed to interrupt Codex turn")
            cancel_background_task(turn_task, name="pma.app_server.turn.wait")
            return {"status": "interrupted", "detail": "PMA chat interrupted"}
        turn_result = await turn_task
    finally:
        cancel_background_task(timeout_task, name="pma.app_server.timeout.wait")
        cancel_background_task(interrupt_task, name="pma.app_server.interrupt.wait")

    if getattr(turn_result, "errors", None):
        errors = turn_result.errors
        raise HTTPException(status_code=502, detail=errors[-1] if errors else "")

    output = str(getattr(turn_result, "final_message", "") or "").strip()
    if not output:
        output = "\n".join(getattr(turn_result, "agent_messages", []) or []).strip()
    raw_events = getattr(turn_result, "raw_events", []) or []
    timeline_events: list[RunEvent] = []
    list_events = getattr(events, "list_events", None)
    if callable(list_events):
        try:
            buffered = await list_events(thread_id, handle.turn_id)
        except (
            OSError,
            RuntimeError,
            ValueError,
        ):
            buffered = []
        state = RuntimeThreadRunEventState()
        for entry in buffered:
            if not isinstance(entry, dict):
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            params = message.get("params")
            timeline_events.extend(
                normalize_runtime_thread_message(
                    str(message.get("method") or ""),
                    params if isinstance(params, dict) else {},
                    state,
                    timestamp=iso_from_epoch_millis(entry.get("received_at")),
                )
            )
    if not timeline_events and raw_events:
        state = RuntimeThreadRunEventState()
        for raw_event in raw_events:
            timeline_events.extend(
                await normalize_runtime_thread_raw_event(raw_event, state)
            )
    timeline_events.append(Completed(timestamp=now_iso(), final_message=output))
    return {
        "status": "ok",
        "message": output,
        "thread_id": thread_id,
        "backend_thread_id": thread_id,
        "turn_id": handle.turn_id,
        "raw_events": raw_events,
        "timeline_events": timeline_events,
    }


async def execute_opencode(
    supervisor: Any,
    hub_root: Path,
    prompt: str,
    interrupt_event: asyncio.Event,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    backend_session_id: Optional[str] = None,
    thread_registry: Optional[Any] = None,
    thread_key: Optional[str] = None,
    stall_timeout_seconds: Optional[float] = None,
    on_meta: Optional[Any] = None,
    part_handler: Optional[Any] = None,
    timeout_seconds: Optional[float] = None,
) -> dict[str, Any]:
    resolved_timeout_seconds = float(
        timeout_seconds if timeout_seconds is not None else DEFAULT_PMA_TIMEOUT_SECONDS
    )
    from .....agents.opencode.runtime import (
        PERMISSION_ALLOW,
        build_turn_id,
        collect_opencode_output,
        extract_session_id,
        opencode_stream_timeouts,
        parse_message_response,
        split_model_id,
    )

    client = await supervisor.get_client(hub_root)
    session_id = backend_session_id
    if session_id is None and thread_registry is not None and thread_key:
        session_id = thread_registry.get_thread_id(thread_key)
    if not session_id:
        session = await client.create_session(directory=str(hub_root))
        session_id = extract_session_id(session, allow_fallback_id=True)
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(
                status_code=502, detail="OpenCode did not return a session id"
            )
        if thread_registry is not None and thread_key:
            thread_registry.set_thread_id(thread_key, session_id)
    if on_meta is not None:
        try:
            maybe = on_meta(session_id, build_turn_id(session_id))
            if asyncio.iscoroutine(maybe):
                await maybe
        except (
            RuntimeError,
            ValueError,
            TypeError,
            ConnectionError,
            OSError,
        ):
            logger.exception("pma meta callback failed")

    opencode_harness = OpenCodeHarness(supervisor)
    if interrupt_event.is_set():
        await opencode_harness.interrupt(hub_root, session_id, None)
        return {"status": "interrupted", "detail": "PMA chat interrupted"}

    model_payload = split_model_id(model)
    await supervisor.mark_turn_started(hub_root)

    ready_event = asyncio.Event()
    timeline_state = RuntimeThreadRunEventState()
    timeline_events: list[RunEvent] = []

    async def _timeline_part_handler(
        part_type: str,
        part: dict[str, Any],
        delta_text: Optional[str],
    ) -> None:
        params: dict[str, Any] = {"part": dict(part or {})}
        if delta_text:
            params["delta"] = {"text": delta_text}
        timeline_events.extend(
            normalize_runtime_thread_message(
                "message.part.updated",
                params,
                timeline_state,
                timestamp=now_iso(),
            )
        )
        if part_handler is not None:
            maybe = part_handler(part_type, part, delta_text)
            if asyncio.iscoroutine(maybe):
                await maybe

    stall_timeout, first_event_timeout = opencode_stream_timeouts(
        stall_timeout_seconds,
    )
    output_task = asyncio.create_task(
        collect_opencode_output(
            client,
            session_id=session_id,
            workspace_path=str(hub_root),
            model_payload=model_payload,
            permission_policy=PERMISSION_ALLOW,
            question_policy="auto_first_option",
            should_stop=interrupt_event.is_set,
            ready_event=ready_event,
            part_handler=_timeline_part_handler,
            stall_timeout_seconds=stall_timeout,
            first_event_timeout_seconds=first_event_timeout,
            logger=logger,
        )
    )
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pass

    prompt_task = asyncio.create_task(
        client.prompt_async(
            session_id,
            message=prompt,
            model=model_payload,
            variant=reasoning,
        )
    )
    timeout_task = asyncio.create_task(asyncio.sleep(resolved_timeout_seconds))
    interrupt_task = asyncio.create_task(interrupt_event.wait())
    try:
        prompt_response = None
        try:
            prompt_response = await prompt_task
        except (RuntimeError, OSError, ConnectionError) as exc:
            interrupt_event.set()
            cancel_background_task(output_task, name="pma.opencode.output.collect")
            await opencode_harness.interrupt(hub_root, session_id, None)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        done, _ = await asyncio.wait(
            {output_task, timeout_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_task in done:
            cancel_background_task(output_task, name="pma.opencode.output.collect")
            await opencode_harness.interrupt(hub_root, session_id, None)
            return {"status": "error", "detail": "PMA chat timed out"}
        if interrupt_task in done:
            cancel_background_task(output_task, name="pma.opencode.output.collect")
            await opencode_harness.interrupt(hub_root, session_id, None)
            return {"status": "interrupted", "detail": "PMA chat interrupted"}
        output_result = await output_task
        if (not output_result.text) and prompt_response is not None:
            fallback = parse_message_response(prompt_response)
            if fallback.text:
                output_result = type(output_result)(
                    text=fallback.text, error=fallback.error
                )
    finally:
        cancel_background_task(timeout_task, name="pma.opencode.timeout.wait")
        cancel_background_task(interrupt_task, name="pma.opencode.interrupt.wait")
        await supervisor.mark_turn_finished(hub_root)

    if output_result.error:
        raise HTTPException(status_code=502, detail=output_result.error)
    if (
        output_result.text
        and prompt_response is not None
        and not timeline_has_assistant_output(timeline_events)
    ):
        completion_payload = (
            dict(prompt_response) if isinstance(prompt_response, dict) else {}
        )
        info = completion_payload.get("info")
        if not isinstance(info, dict):
            info = {}
        info = dict(info)
        if not isinstance(info.get("role"), str) or not str(info.get("role")).strip():
            info["role"] = "assistant"
        completion_payload["info"] = info
        if not isinstance(completion_payload.get("parts"), list):
            completion_payload["parts"] = [{"type": "text", "text": output_result.text}]
        timeline_events.extend(
            normalize_runtime_thread_message(
                "message.completed",
                completion_payload,
                timeline_state,
                timestamp=now_iso(),
            )
        )
    timeline_events.append(
        Completed(timestamp=now_iso(), final_message=output_result.text)
    )
    return {
        "status": "ok",
        "message": output_result.text,
        "thread_id": session_id,
        "backend_thread_id": session_id,
        "turn_id": build_turn_id(session_id),
        "timeline_events": timeline_events,
    }
