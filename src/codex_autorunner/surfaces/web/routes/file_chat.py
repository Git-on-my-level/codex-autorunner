"""
Unified file chat routes: AI-powered editing for tickets and contextspace docs.

Targets:
- ticket:{index} -> .codex-autorunner/tickets/TICKET-###.md
- contextspace:{kind} -> .codex-autorunner/contextspace/{kind}.md
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ....core import drafts as draft_utils
from ....core.usage import persist_opencode_usage_snapshot
from ....integrations.app_server.event_buffer import format_sse
from .file_chat_routes import (
    FileChatRoutesState as _ExtractedFileChatRoutesState,
)
from .file_chat_routes import build_file_chat_runtime_routes
from .file_chat_routes import targets as extracted_targets
from .file_chat_routes.drafts import (
    apply_file_patch as extracted_apply_file_patch,
)
from .file_chat_routes.drafts import (
    discard_file_patch as extracted_discard_file_patch,
)
from .file_chat_routes.drafts import (
    pending_file_patch as extracted_pending_file_patch,
)
from .file_chat_routes.execution import execute_file_chat as extracted_execute_file_chat
from .file_chat_routes.runtime import active_for_client as _active_for_client
from .file_chat_routes.runtime import begin_turn_state as _begin_turn_state
from .file_chat_routes.runtime import clear_interrupt_event as _clear_interrupt_event
from .file_chat_routes.runtime import finalize_turn_state as _finalize_turn_state
from .file_chat_routes.runtime import get_state as _get_state
from .file_chat_routes.runtime import last_for_client as _last_for_client
from .file_chat_routes.runtime import update_turn_state as _update_turn_state
from .shared import SSE_HEADERS

FILE_CHAT_STATE_NAME = draft_utils.FILE_CHAT_STATE_NAME
FILE_CHAT_TIMEOUT_SECONDS = 180
logger = logging.getLogger(__name__)
FileChatRoutesState = _ExtractedFileChatRoutesState

# Keep the staged extraction seam visible while this module remains the composition owner.
_EXTRACTED_FILE_CHAT_SEAMS = (
    build_file_chat_runtime_routes,
    extracted_execute_file_chat,
)

ExtractedTarget = extracted_targets._Target
_Target = extracted_targets._Target
_build_file_chat_prompt = extracted_targets.build_file_chat_prompt
_build_patch = extracted_targets.build_patch
_parse_target = extracted_targets.parse_target
_read_file = extracted_targets.read_file
_resolve_repo_root = extracted_targets.resolve_repo_root


class FileChatError(Exception):
    """Base error for file chat failures."""


def _state_path(repo_root: Path) -> Path:
    return draft_utils.state_path(repo_root)


def _load_state(repo_root: Path) -> Dict[str, Any]:
    return draft_utils.load_state(repo_root)


def _save_state(repo_root: Path, state: Dict[str, Any]) -> None:
    draft_utils.save_state(repo_root, state)


def _hash_content(content: str) -> str:
    return draft_utils.hash_content(content)


def build_file_chat_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["file-chat"])

    @router.get("/file-chat/active")
    async def file_chat_active(
        request: Request, client_turn_id: Optional[str] = None
    ) -> Dict[str, Any]:
        current = await _active_for_client(request, client_turn_id)
        last = await _last_for_client(request, client_turn_id)
        return {"active": bool(current), "current": current, "last_result": last}

    @router.post("/file-chat")
    async def chat_file(request: Request):
        """Chat with a file target - optionally streams SSE events."""
        body = await request.json()
        target_raw = body.get("target")
        message = (body.get("message") or "").strip()
        stream = bool(body.get("stream", False))
        agent = body.get("agent", "codex")
        model = body.get("model")
        reasoning = body.get("reasoning")
        client_turn_id = (body.get("client_turn_id") or "").strip() or None

        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, str(target_raw or ""))

        # Ensure target directory exists for contextspace docs (write on demand)
        if target.kind == "contextspace":
            target.path.parent.mkdir(parents=True, exist_ok=True)

        # Concurrency guard per target
        s = _get_state(request)
        async with s.chat_lock:
            existing = s.active_chats.get(target.state_key)
            if existing is not None and not existing.is_set():
                raise HTTPException(status_code=409, detail="File chat already running")
            s.active_chats[target.state_key] = asyncio.Event()

        await _begin_turn_state(request, target, client_turn_id)

        if stream:
            return StreamingResponse(
                _stream_file_chat(
                    request,
                    repo_root,
                    target,
                    message,
                    agent=agent,
                    model=model,
                    reasoning=reasoning,
                    client_turn_id=client_turn_id,
                ),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        try:
            result: Dict[str, Any]

            async def _on_meta(agent_id: str, thread_id: str, turn_id: str) -> None:
                await _update_turn_state(
                    request,
                    target,
                    agent=agent_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )

            try:
                result = await _execute_file_chat(
                    request,
                    repo_root,
                    target,
                    message,
                    agent=agent,
                    model=model,
                    reasoning=reasoning,
                    on_meta=_on_meta,
                )
            except Exception as exc:
                await _finalize_turn_state(
                    request,
                    target,
                    {
                        "status": "error",
                        "detail": str(exc),
                        "client_turn_id": client_turn_id or "",
                    },
                )
                raise
            result = dict(result or {})
            result["client_turn_id"] = client_turn_id or ""
            await _finalize_turn_state(request, target, result)
            return result
        finally:
            await _clear_interrupt_event(request, target.state_key)

    async def _stream_file_chat(
        request: Request,
        repo_root: Path,
        target: ExtractedTarget,
        message: str,
        *,
        agent: str = "codex",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_turn_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        yield format_sse("status", {"status": "queued"})
        try:

            async def _on_meta(agent_id: str, thread_id: str, turn_id: str) -> None:
                await _update_turn_state(
                    request,
                    target,
                    agent=agent_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )

            run_task = asyncio.create_task(
                _execute_file_chat(
                    request,
                    repo_root,
                    target,
                    message,
                    agent=agent,
                    model=model,
                    reasoning=reasoning,
                    on_meta=_on_meta,
                )
            )

            async def _finalize() -> None:
                result = {"status": "error", "detail": "File chat failed"}
                try:
                    result = await run_task
                except Exception as exc:
                    logger.exception("file chat task failed")
                    result = {
                        "status": "error",
                        "detail": str(exc) or "File chat failed",
                    }
                result = dict(result or {})
                result["client_turn_id"] = client_turn_id or ""
                await _finalize_turn_state(request, target, result)

            asyncio.create_task(_finalize())

            try:
                result = await asyncio.shield(run_task)
            except asyncio.CancelledError:
                # client disconnected; turn continues in background
                return

            if result.get("status") == "ok":
                raw_events = result.pop("raw_events", []) or []
                for event in raw_events:
                    yield format_sse("app-server", event)
                usage_parts = result.pop("usage_parts", []) or []
                for usage in usage_parts:
                    yield format_sse("token_usage", usage)
                result["client_turn_id"] = client_turn_id or ""
                yield format_sse("update", result)
                yield format_sse("done", {"status": "ok"})
            elif result.get("status") == "interrupted":
                yield format_sse(
                    "interrupted",
                    {"detail": result.get("detail") or "File chat interrupted"},
                )
            else:
                yield format_sse(
                    "error", {"detail": result.get("detail") or "File chat failed"}
                )
        except Exception:
            logger.exception("file chat stream failed")
            yield format_sse("error", {"detail": "File chat failed"})
        finally:
            await _clear_interrupt_event(request, target.state_key)

    async def _execute_file_chat(
        request: Request,
        repo_root: Path,
        target: ExtractedTarget,
        message: str,
        *,
        agent: str = "codex",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        on_meta: Optional[Callable[[str, str, str], Any]] = None,
        on_usage: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> Dict[str, Any]:
        return await extracted_execute_file_chat(
            request,
            repo_root,
            target,
            message,
            agent=agent,
            model=model,
            reasoning=reasoning,
            on_meta=on_meta,
            on_usage=on_usage,
        )

    async def _execute_app_server(
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
        agent_message = _parse_agent_message(output)
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

    async def _execute_opencode(
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
        from ....agents.opencode.runtime import (
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
        agent_message = _parse_agent_message(output_result.text)
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

    def _parse_agent_message(output: str) -> str:
        text = (output or "").strip()
        if not text:
            return "File updated via chat."
        for line in text.splitlines():
            if line.lower().startswith("agent:"):
                return line[len("agent:") :].strip() or "File updated via chat."
        first_line = text.splitlines()[0].strip()
        return (first_line[:97] + "...") if len(first_line) > 100 else first_line

    @router.get("/file-chat/pending")
    async def pending_file_patch(request: Request, target: str):
        return await extracted_pending_file_patch(request, target)

    @router.post("/file-chat/apply")
    async def apply_file_patch(request: Request):
        body = await request.json()
        return await extracted_apply_file_patch(request, body)

    @router.post("/file-chat/discard")
    async def discard_file_patch(request: Request):
        body = await request.json()
        return await extracted_discard_file_patch(request, body)

    @router.get("/file-chat/turns/{turn_id}/events")
    async def stream_file_chat_turn_events(
        turn_id: str, request: Request, thread_id: str, agent: str = "codex"
    ):
        agent_id = (agent or "").strip().lower()
        if agent_id == "codex":
            events = getattr(request.app.state, "app_server_events", None)
            if events is None:
                raise HTTPException(status_code=404, detail="Events unavailable")
            if not thread_id:
                raise HTTPException(status_code=400, detail="thread_id is required")
            return StreamingResponse(
                events.stream(thread_id, turn_id),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        if agent_id == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                raise HTTPException(status_code=404, detail="OpenCode unavailable")
            from ....agents.opencode.harness import OpenCodeHarness

            harness = OpenCodeHarness(supervisor)
            repo_root = _resolve_repo_root(request)
            return StreamingResponse(
                harness.stream_events(repo_root, thread_id, turn_id),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        raise HTTPException(status_code=404, detail="Unknown agent")

    @router.post("/file-chat/interrupt")
    async def interrupt_file_chat(request: Request):
        body = await request.json()
        repo_root = _resolve_repo_root(request)
        resolved = _parse_target(repo_root, str(body.get("target") or ""))
        s = _get_state(request)
        async with s.chat_lock:
            ev = s.active_chats.get(resolved.state_key)
            if ev is None:
                return {"status": "ok", "detail": "No active chat to interrupt"}
            ev.set()
            return {"status": "interrupted", "detail": "File chat interrupted"}

    # Legacy ticket endpoints (thin wrappers) to keep older UIs working.

    @router.post("/tickets/{index}/chat")
    async def chat_ticket(index: int, request: Request):
        body = await request.json()
        message = (body.get("message") or "").strip()
        stream = bool(body.get("stream", False))
        agent = body.get("agent", "codex")
        model = body.get("model")
        reasoning = body.get("reasoning")
        client_turn_id = (body.get("client_turn_id") or "").strip() or None

        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, f"ticket:{int(index)}")

        s = _get_state(request)
        async with s.chat_lock:
            existing = s.active_chats.get(target.state_key)
            if existing is not None and not existing.is_set():
                raise HTTPException(
                    status_code=409, detail="Ticket chat already running"
                )
            s.active_chats[target.state_key] = asyncio.Event()
        await _begin_turn_state(request, target, client_turn_id)

        if stream:
            return StreamingResponse(
                _stream_file_chat(
                    request,
                    repo_root,
                    target,
                    message,
                    agent=agent,
                    model=model,
                    reasoning=reasoning,
                    client_turn_id=client_turn_id,
                ),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        try:
            result = await _execute_file_chat(
                request,
                repo_root,
                target,
                message,
                agent=agent,
                model=model,
                reasoning=reasoning,
            )
            result = dict(result or {})
            result["client_turn_id"] = client_turn_id or ""
            await _finalize_turn_state(request, target, result)
            return result
        finally:
            await _clear_interrupt_event(request, target.state_key)

    @router.get("/tickets/{index}/chat/pending")
    async def pending_ticket_patch(index: int, request: Request):
        return await pending_file_patch(request, target=f"ticket:{int(index)}")

    @router.post("/tickets/{index}/chat/apply")
    async def apply_ticket_patch(index: int, request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        payload = dict(body) if isinstance(body, dict) else {}
        payload["target"] = f"ticket:{int(index)}"
        result = await extracted_apply_file_patch(request, payload)
        return {
            "status": "ok",
            "index": int(index),
            "content": result.get("content", ""),
            "agent_message": result.get("agent_message", "Draft applied"),
        }

    @router.post("/tickets/{index}/chat/discard")
    async def discard_ticket_patch(index: int, request: Request):
        result = await extracted_discard_file_patch(
            request, {"target": f"ticket:{int(index)}"}
        )
        return {
            "status": "ok",
            "index": int(index),
            "content": result.get("content", ""),
        }

    @router.post("/tickets/{index}/chat/interrupt")
    async def interrupt_ticket_chat(index: int, request: Request):
        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, f"ticket:{int(index)}")
        s = _get_state(request)
        async with s.chat_lock:
            ev = s.active_chats.get(target.state_key)
            if ev is None:
                return {"status": "ok", "detail": "No active chat to interrupt"}
            ev.set()
            return {"status": "interrupted", "detail": "Ticket chat interrupted"}

    @router.post("/tickets/{index}/chat/new-thread")
    async def reset_ticket_chat_thread(index: int, request: Request):
        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, f"ticket:{int(index)}")
        thread_key = f"file_chat.{target.state_key}"
        registry = getattr(request.app.state, "app_server_threads", None)
        cleared = False
        if registry is not None:
            try:
                cleared = bool(registry.reset_thread(thread_key))
            except Exception:
                logger.debug(
                    "ticket chat thread reset failed for key=%s",
                    thread_key,
                    exc_info=True,
                )
        return {
            "status": "ok",
            "index": int(index),
            "target": target.target,
            "chat_scope": target.chat_scope,
            "key": thread_key,
            "cleared": cleared,
        }

    return router
