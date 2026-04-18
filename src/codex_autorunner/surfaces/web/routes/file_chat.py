"""
Unified file chat routes: AI-powered editing for tickets and contextspace docs.

Targets:
- ticket:{index} -> .codex-autorunner/tickets/TICKET-###.md
- contextspace:{kind} -> .codex-autorunner/contextspace/{kind}.md
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ....core.sse import format_sse
from .file_chat_routes import FileChatRoutesState
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
from .file_chat_routes.execution import (
    execute_file_chat as extracted_execute_file_chat,
)
from .file_chat_routes.execution import (
    resolve_file_chat_agent_selection as extracted_resolve_file_chat_agent_selection,
)
from .file_chat_routes.execution_agents import FileChatError
from .file_chat_routes.runtime import (
    active_for_client as _active_for_client,
)
from .file_chat_routes.runtime import (
    begin_turn_state as _begin_turn_state,
)
from .file_chat_routes.runtime import (
    clear_interrupt_event as _clear_interrupt_event,
)
from .file_chat_routes.runtime import (
    finalize_turn_state as _finalize_turn_state,
)
from .file_chat_routes.runtime import (
    get_state as _get_state,
)
from .file_chat_routes.runtime import (
    last_for_client as _last_for_client,
)
from .file_chat_routes.runtime import (
    update_turn_state as _update_turn_state,
)
from .file_chat_routes.stream_shaping import (
    shape_stream_error,
    shape_stream_events,
    shape_stream_queued,
)
from .shared import SSE_HEADERS

__all__ = ["FileChatRoutesState", "build_file_chat_routes"]

logger = logging.getLogger(__name__)

_Target = extracted_targets._Target
_build_file_chat_prompt = extracted_targets.build_file_chat_prompt
_build_patch = extracted_targets.build_patch
_parse_target = extracted_targets.parse_target
_resolve_repo_root = extracted_targets.resolve_repo_root


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
        body = await request.json()
        target_raw = body.get("target")
        message = (body.get("message") or "").strip()
        stream = bool(body.get("stream", False))
        agent = body.get("agent", "codex")
        profile = body.get("profile")
        model = body.get("model")
        reasoning = body.get("reasoning")
        client_turn_id = (body.get("client_turn_id") or "").strip() or None

        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, str(target_raw or ""))

        if target.kind == "contextspace":
            target.path.parent.mkdir(parents=True, exist_ok=True)

        selection = extracted_resolve_file_chat_agent_selection(
            request,
            target,
            agent=agent,
            profile=profile,
        )

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
                    agent=selection.agent_id,
                    profile=selection.profile,
                    model=model,
                    reasoning=reasoning,
                    client_turn_id=client_turn_id,
                ),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        try:

            async def _on_meta(agent_id: str, thread_id: str, turn_id: str) -> None:
                await _update_turn_state(
                    request,
                    target,
                    agent=agent_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )

            try:
                result = await extracted_execute_file_chat(
                    request,
                    repo_root,
                    target,
                    message,
                    agent=selection.agent_id,
                    profile=selection.profile,
                    model=model,
                    reasoning=reasoning,
                    on_meta=_on_meta,
                )
            except (
                RuntimeError,
                asyncio.CancelledError,
                OSError,
                FileChatError,
            ) as exc:
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
        target: _Target,
        message: str,
        *,
        agent: str = "codex",
        profile: Optional[str] = None,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_turn_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        yield shape_stream_queued()
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
                extracted_execute_file_chat(
                    request,
                    repo_root,
                    target,
                    message,
                    agent=agent,
                    profile=profile,
                    model=model,
                    reasoning=reasoning,
                    on_meta=_on_meta,
                )
            )

            async def _finalize() -> None:
                result: Dict[str, Any] = {
                    "status": "error",
                    "detail": "File chat failed",
                }
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
                return

            for event in shape_stream_events(result, client_turn_id=client_turn_id):
                yield event
        except Exception:
            logger.exception("file chat stream failed")
            yield shape_stream_error()
        finally:
            await _clear_interrupt_event(request, target.state_key)

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

            async def _stream_opencode() -> AsyncIterator[str]:
                async for raw_event in harness.stream_events(
                    repo_root, thread_id, turn_id
                ):
                    payload = (
                        raw_event
                        if isinstance(raw_event, dict)
                        else {"value": raw_event}
                    )
                    yield format_sse("app-server", payload)

            return StreamingResponse(
                _stream_opencode(),
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

    @router.post("/tickets/{index}/chat")
    async def chat_ticket(index: int, request: Request):
        body = await request.json()
        message = (body.get("message") or "").strip()
        stream = bool(body.get("stream", False))
        agent = body.get("agent", "codex")
        profile = body.get("profile")
        model = body.get("model")
        reasoning = body.get("reasoning")
        client_turn_id = (body.get("client_turn_id") or "").strip() or None

        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, f"ticket:{int(index)}")
        selection = extracted_resolve_file_chat_agent_selection(
            request,
            target,
            agent=agent,
            profile=profile,
        )

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
                    agent=selection.agent_id,
                    profile=selection.profile,
                    model=model,
                    reasoning=reasoning,
                    client_turn_id=client_turn_id,
                ),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        try:
            result = await extracted_execute_file_chat(
                request,
                repo_root,
                target,
                message,
                agent=selection.agent_id,
                profile=selection.profile,
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
        except ValueError:
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
        try:
            body = await request.json()
        except ValueError:
            body = {}
        payload = dict(body) if isinstance(body, dict) else {}
        selection = extracted_resolve_file_chat_agent_selection(
            request,
            target,
            agent=payload.get("agent", "codex"),
            profile=payload.get("profile"),
        )
        thread_key = selection.thread_key
        registry = getattr(request.app.state, "app_server_threads", None)
        cleared = False
        if registry is not None:
            try:
                cleared = bool(registry.reset_thread(thread_key))
            except (
                AttributeError,
                KeyError,
                RuntimeError,
                OSError,
            ):
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
