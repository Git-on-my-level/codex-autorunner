"""
Unified file chat routes: AI-powered editing for tickets and contextspace docs.

Targets:
- ticket:{index} -> .codex-autorunner/tickets/TICKET-###.md
- contextspace:{kind} -> .codex-autorunner/contextspace/{kind}.md
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ....core.sse import format_sse
from ..services import file_chat as file_chat_service
from .file_chat_routes import FileChatRoutesState
from .shared import SSE_HEADERS

__all__ = ["FileChatRoutesState", "build_file_chat_routes"]


def _normalize_client_turn_id(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _turn_request_from_body(
    body: Dict[str, Any],
    *,
    target: str,
    already_running_detail: str,
) -> file_chat_service.FileChatTurnRequest:
    return file_chat_service.FileChatTurnRequest(
        target=target,
        message=str(body.get("message") or "").strip(),
        agent=body.get("agent", "codex"),
        profile=body.get("profile"),
        model=body.get("model"),
        reasoning=body.get("reasoning"),
        client_turn_id=_normalize_client_turn_id(body.get("client_turn_id")),
        already_running_detail=already_running_detail,
    )


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return dict(body) if isinstance(body, dict) else {}


async def _stream_sse(
    items: AsyncIterator[file_chat_service.FileChatStreamItem],
) -> AsyncIterator[str]:
    async for item in items:
        yield format_sse(item.event, item.payload)


def build_file_chat_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["file-chat"])

    @router.get("/file-chat/active")
    async def file_chat_active(
        request: Request, client_turn_id: Optional[str] = None
    ) -> Dict[str, Any]:
        snapshot = await file_chat_service.get_active_snapshot(
            request,
            client_turn_id,
        )
        return {
            "active": snapshot.active,
            "current": snapshot.current,
            "last_result": snapshot.last_result,
        }

    @router.post("/file-chat")
    async def chat_file(request: Request):
        body = await _json_body(request)
        turn = _turn_request_from_body(
            body,
            target=str(body.get("target") or ""),
            already_running_detail="File chat already running",
        )
        if bool(body.get("stream", False)):
            stream_items = await file_chat_service.open_stream_turn(request, turn)
            return StreamingResponse(
                _stream_sse(stream_items),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        return await file_chat_service.run_turn(request, turn)

    @router.get("/file-chat/pending")
    async def pending_file_patch(request: Request, target: str):
        return await file_chat_service.pending_patch(request, target)

    @router.post("/file-chat/apply")
    async def apply_file_patch(request: Request):
        return await file_chat_service.apply_patch(request, await _json_body(request))

    @router.post("/file-chat/discard")
    async def discard_file_patch(request: Request):
        return await file_chat_service.discard_patch(request, await _json_body(request))

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
            repo_root = file_chat_service.resolve_repo_root(request)

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
        body = await _json_body(request)
        result = await file_chat_service.interrupt_turn(
            request,
            str(body.get("target") or ""),
            detail="File chat interrupted",
        )
        return {"status": result.status, "detail": result.detail}

    @router.post("/tickets/{index}/chat")
    async def chat_ticket(index: int, request: Request):
        body = await _json_body(request)
        turn = _turn_request_from_body(
            body,
            target=f"ticket:{int(index)}",
            already_running_detail="Ticket chat already running",
        )
        if bool(body.get("stream", False)):
            stream_items = await file_chat_service.open_stream_turn(request, turn)
            return StreamingResponse(
                _stream_sse(stream_items),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        return await file_chat_service.run_turn(request, turn)

    @router.get("/tickets/{index}/chat/pending")
    async def pending_ticket_patch(index: int, request: Request):
        return await file_chat_service.pending_patch(request, f"ticket:{int(index)}")

    @router.post("/tickets/{index}/chat/apply")
    async def apply_ticket_patch(index: int, request: Request):
        payload = await _json_body(request)
        payload["target"] = f"ticket:{int(index)}"
        result = await file_chat_service.apply_patch(request, payload)
        return {
            "status": "ok",
            "index": int(index),
            "content": result.get("content", ""),
            "agent_message": result.get("agent_message", "Draft applied"),
        }

    @router.post("/tickets/{index}/chat/discard")
    async def discard_ticket_patch(index: int, request: Request):
        result = await file_chat_service.discard_patch(
            request,
            {"target": f"ticket:{int(index)}"},
        )
        return {
            "status": "ok",
            "index": int(index),
            "content": result.get("content", ""),
        }

    @router.post("/tickets/{index}/chat/interrupt")
    async def interrupt_ticket_chat(index: int, request: Request):
        result = await file_chat_service.interrupt_turn(
            request,
            f"ticket:{int(index)}",
            detail="Ticket chat interrupted",
        )
        return {"status": result.status, "detail": result.detail}

    @router.post("/tickets/{index}/chat/new-thread")
    async def reset_ticket_chat_thread(index: int, request: Request):
        payload = await _json_body(request)
        result = await file_chat_service.reset_ticket_thread(
            request,
            int(index),
            agent=payload.get("agent", "codex"),
            profile=payload.get("profile"),
        )
        return {
            "status": result.status,
            "index": result.index,
            "target": result.target,
            "chat_scope": result.chat_scope,
            "key": result.key,
            "cleared": result.cleared,
        }

    return router
