"""
Hub-level PMA routes (chat + models + events).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ....agents.codex.harness import CodexHarness
from ....agents.opencode.harness import OpenCodeHarness
from ....agents.opencode.supervisor import OpenCodeSupervisorError
from ....agents.registry import validate_agent_id
from ....core.app_server_threads import PMA_KEY, PMA_OPENCODE_KEY
from ....core.pma_context import build_hub_snapshot, format_pma_prompt, load_pma_prompt
from ....integrations.app_server.event_buffer import format_sse
from .agents import _available_agents, _serialize_model_catalog
from .shared import SSE_HEADERS

logger = logging.getLogger(__name__)

PMA_TIMEOUT_SECONDS = 240


def build_pma_routes() -> APIRouter:
    router = APIRouter(prefix="/hub/pma")
    pma_lock = asyncio.Lock()
    pma_event: Optional[asyncio.Event] = None
    pma_active = False

    async def _get_interrupt_event() -> asyncio.Event:
        nonlocal pma_event
        async with pma_lock:
            if pma_event is None or pma_event.is_set():
                pma_event = asyncio.Event()
            return pma_event

    async def _set_active(active: bool) -> None:
        nonlocal pma_active
        async with pma_lock:
            pma_active = active

    async def _begin_turn() -> bool:
        nonlocal pma_active
        async with pma_lock:
            if pma_active:
                return False
            pma_active = True
            return True

    async def _clear_interrupt_event() -> None:
        nonlocal pma_event
        async with pma_lock:
            pma_event = None

    @router.get("/agents")
    def list_pma_agents(request: Request) -> dict[str, Any]:
        agents, default_agent = _available_agents(request)
        return {"agents": agents, "default": default_agent}

    @router.get("/agents/{agent}/models")
    async def list_pma_agent_models(agent: str, request: Request):
        agent_id = (agent or "").strip().lower()
        hub_root = request.app.state.config.root
        if agent_id == "codex":
            supervisor = request.app.state.app_server_supervisor
            events = request.app.state.app_server_events
            if supervisor is None:
                raise HTTPException(status_code=404, detail="Codex harness unavailable")
            codex_harness = CodexHarness(supervisor, events)
            catalog = await codex_harness.model_catalog(hub_root)
            return _serialize_model_catalog(catalog)
        if agent_id == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                raise HTTPException(
                    status_code=404, detail="OpenCode harness unavailable"
                )
            try:
                opencode_harness = OpenCodeHarness(supervisor)
                catalog = await opencode_harness.model_catalog(hub_root)
                return _serialize_model_catalog(catalog)
            except OpenCodeSupervisorError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        raise HTTPException(status_code=404, detail="Unknown agent")

    async def _execute_app_server(
        supervisor: Any,
        hub_root: Path,
        prompt: str,
        interrupt_event: asyncio.Event,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        thread_registry: Optional[Any] = None,
        thread_key: Optional[str] = None,
    ) -> dict[str, Any]:
        client = await supervisor.get_client(hub_root)

        thread_id = None
        if thread_registry is not None and thread_key:
            thread_id = thread_registry.get_thread_id(thread_key)
        if thread_id:
            try:
                await client.thread_resume(thread_id)
            except Exception:
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

        turn_task = asyncio.create_task(handle.wait(timeout=None))
        timeout_task = asyncio.create_task(asyncio.sleep(PMA_TIMEOUT_SECONDS))
        interrupt_task = asyncio.create_task(interrupt_event.wait())
        try:
            done, _ = await asyncio.wait(
                {turn_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                turn_task.cancel()
                return {"status": "error", "detail": "PMA chat timed out"}
            if interrupt_task in done:
                turn_task.cancel()
                return {"status": "interrupted", "detail": "PMA chat interrupted"}
            turn_result = await turn_task
        finally:
            timeout_task.cancel()
            interrupt_task.cancel()

        if getattr(turn_result, "errors", None):
            errors = turn_result.errors
            raise HTTPException(status_code=502, detail=errors[-1] if errors else "")

        output = "\n".join(getattr(turn_result, "agent_messages", []) or []).strip()
        raw_events = getattr(turn_result, "raw_events", []) or []
        return {
            "status": "ok",
            "message": output,
            "thread_id": thread_id,
            "turn_id": handle.turn_id,
            "raw_events": raw_events,
        }

    async def _execute_opencode(
        supervisor: Any,
        hub_root: Path,
        prompt: str,
        interrupt_event: asyncio.Event,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        thread_registry: Optional[Any] = None,
        thread_key: Optional[str] = None,
        stall_timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        from ....agents.opencode.runtime import (
            PERMISSION_ALLOW,
            build_turn_id,
            collect_opencode_output,
            extract_session_id,
            parse_message_response,
            split_model_id,
        )

        client = await supervisor.get_client(hub_root)
        session_id = None
        if thread_registry is not None and thread_key:
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

        model_payload = split_model_id(model)
        await supervisor.mark_turn_started(hub_root)

        ready_event = asyncio.Event()
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
                stall_timeout_seconds=stall_timeout_seconds,
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
        timeout_task = asyncio.create_task(asyncio.sleep(PMA_TIMEOUT_SECONDS))
        interrupt_task = asyncio.create_task(interrupt_event.wait())
        try:
            prompt_response = None
            try:
                prompt_response = await prompt_task
            except Exception as exc:
                interrupt_event.set()
                output_task.cancel()
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            done, _ = await asyncio.wait(
                {output_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                output_task.cancel()
                return {"status": "error", "detail": "PMA chat timed out"}
            if interrupt_task in done:
                output_task.cancel()
                return {"status": "interrupted", "detail": "PMA chat interrupted"}
            output_result = await output_task
            if (not output_result.text) and prompt_response is not None:
                fallback = parse_message_response(prompt_response)
                if fallback.text:
                    output_result = type(output_result)(
                        text=fallback.text, error=fallback.error
                    )
        finally:
            timeout_task.cancel()
            interrupt_task.cancel()
            await supervisor.mark_turn_finished(hub_root)

        if output_result.error:
            raise HTTPException(status_code=502, detail=output_result.error)
        return {
            "status": "ok",
            "message": output_result.text,
            "thread_id": session_id,
            "turn_id": build_turn_id(session_id),
        }

    @router.post("/chat")
    async def pma_chat(request: Request):
        body = await request.json()
        message = (body.get("message") or "").strip()
        stream = bool(body.get("stream", False))
        agent = body.get("agent")
        model = body.get("model")
        reasoning = body.get("reasoning")

        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        if not await _begin_turn():
            raise HTTPException(status_code=409, detail="PMA chat already running")

        agents, available_default = _available_agents(request)
        available_ids = {entry.get("id") for entry in agents if isinstance(entry, dict)}

        def _resolve_default_agent() -> str:
            raw = getattr(request.app.state.config, "raw", {})
            configured_default = None
            if isinstance(raw, dict):
                pma_config = raw.get("pma", {})
                if isinstance(pma_config, dict):
                    configured_default = pma_config.get("default_agent")
            try:
                candidate = validate_agent_id(configured_default or "")
            except ValueError:
                candidate = None
            if candidate and candidate in available_ids:
                return candidate
            return available_default

        try:
            agent_id = validate_agent_id(agent or "")
        except ValueError:
            agent_id = _resolve_default_agent()

        hub_root = request.app.state.config.root
        prompt_base = load_pma_prompt(hub_root)
        supervisor = getattr(request.app.state, "hub_supervisor", None)
        snapshot = await build_hub_snapshot(supervisor)
        prompt = format_pma_prompt(prompt_base, snapshot, message)

        interrupt_event = await _get_interrupt_event()
        if interrupt_event.is_set():
            await _set_active(False)
            return {"status": "interrupted", "detail": "PMA chat interrupted"}

        async def _run() -> dict[str, Any]:
            supervisor = getattr(request.app.state, "app_server_supervisor", None)
            opencode = getattr(request.app.state, "opencode_supervisor", None)
            registry = getattr(request.app.state, "app_server_threads", None)
            stall_timeout_seconds = None
            try:
                stall_timeout_seconds = (
                    request.app.state.config.opencode.session_stall_timeout_seconds
                )
            except Exception:
                stall_timeout_seconds = None

            if agent_id == "opencode":
                if opencode is None:
                    return {"status": "error", "detail": "OpenCode unavailable"}
                return await _execute_opencode(
                    opencode,
                    hub_root,
                    prompt,
                    interrupt_event,
                    model=model,
                    reasoning=reasoning,
                    thread_registry=registry,
                    thread_key=PMA_OPENCODE_KEY,
                    stall_timeout_seconds=stall_timeout_seconds,
                )
            if supervisor is None:
                return {"status": "error", "detail": "App-server unavailable"}
            return await _execute_app_server(
                supervisor,
                hub_root,
                prompt,
                interrupt_event,
                model=model,
                reasoning=reasoning,
                thread_registry=registry,
                thread_key=PMA_KEY,
            )

        async def _stream() -> AsyncIterator[str]:
            yield format_sse("status", {"status": "queued"})
            try:
                result = await _run()
                if result.get("status") == "ok":
                    raw_events = result.pop("raw_events", []) or []
                    for event in raw_events:
                        yield format_sse("app-server", event)
                    yield format_sse("update", result)
                    yield format_sse("done", {"status": "ok"})
                elif result.get("status") == "interrupted":
                    yield format_sse(
                        "interrupted",
                        {"detail": result.get("detail") or "PMA chat interrupted"},
                    )
                else:
                    yield format_sse(
                        "error", {"detail": result.get("detail") or "PMA chat failed"}
                    )
            except Exception:
                logger.exception("pma chat stream failed")
                yield format_sse("error", {"detail": "PMA chat failed"})
            finally:
                await _set_active(False)
                await _clear_interrupt_event()

        if stream:
            return StreamingResponse(
                _stream(),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        try:
            result = await _run()
            return result
        finally:
            await _set_active(False)
            await _clear_interrupt_event()

    @router.post("/interrupt")
    async def pma_interrupt() -> dict[str, Any]:
        event = await _get_interrupt_event()
        event.set()
        return {"status": "ok", "interrupted": True}

    @router.post("/thread/reset")
    async def reset_pma_thread(request: Request) -> dict[str, Any]:
        body = await request.json()
        agent = (body.get("agent") or "").strip().lower()
        registry = request.app.state.app_server_threads
        cleared = []
        if agent in ("", "all", None):
            if registry.reset_thread(PMA_KEY):
                cleared.append(PMA_KEY)
            if registry.reset_thread(PMA_OPENCODE_KEY):
                cleared.append(PMA_OPENCODE_KEY)
        elif agent == "opencode":
            if registry.reset_thread(PMA_OPENCODE_KEY):
                cleared.append(PMA_OPENCODE_KEY)
        else:
            if registry.reset_thread(PMA_KEY):
                cleared.append(PMA_KEY)
        return {"status": "ok", "cleared": cleared}

    @router.get("/turns/{turn_id}/events")
    async def stream_pma_turn_events(
        turn_id: str, request: Request, thread_id: str, agent: str = "codex"
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
                raise HTTPException(status_code=404, detail="OpenCode unavailable")
            harness = OpenCodeHarness(supervisor)
            return StreamingResponse(
                harness.stream_events(
                    request.app.state.config.root, thread_id, turn_id
                ),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        raise HTTPException(status_code=404, detail="Unknown agent")

    return router


__all__ = ["build_pma_routes"]
