from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .....core.orchestration import (
    ChatSurfaceReadService,
    parse_chat_surface_cursor,
)
from ...services.pma import get_pma_request_context
from ..shared import SSE_HEADERS

CHAT_EVENTS_CONTRACT_VERSION = "pma_chat_events.v1"
_POLL_INTERVAL_SECONDS = 1.5
_HEARTBEAT_SECONDS = 15.0


def _serialize_chat_snapshot(request: Request) -> dict[str, Any]:
    context = get_pma_request_context(request)
    service = ChatSurfaceReadService(context.hub_root, durable=True)
    return service.pma_compat_snapshot(limit=500)


def _sse_frame(event: str, payload: dict[str, Any], *, event_id: Optional[str]) -> str:
    event_id_line = f"id: {event_id}\n" if event_id else ""
    return (
        f"event: {event}\n"
        f"{event_id_line}"
        f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"
    )


def build_chat_event_routes(router: APIRouter, get_runtime_state) -> None:
    _ = get_runtime_state

    @router.get("/events")
    @router.get("/threads/events")
    async def stream_pma_chat_events(
        request: Request,
        cursor: Optional[str] = None,
        once: bool = False,
        event_limit: int = Query(100, ge=1, le=1000),
    ):
        header_cursor = request.headers.get("last-event-id")
        raw_cursor = cursor if cursor is not None else header_cursor
        try:
            parsed_cursor = parse_chat_surface_cursor(raw_cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        context = get_pma_request_context(request)
        service = ChatSurfaceReadService(context.hub_root, durable=True)

        async def _stream() -> Any:
            last_revision: Optional[str] = None
            last_cursor = parsed_cursor
            last_heartbeat_at = asyncio.get_running_loop().time()
            while True:
                payload = await asyncio.to_thread(
                    service.pma_compat_snapshot, limit=500
                )
                revision = str(payload["revision"])
                if revision != last_revision:
                    yield _sse_frame("chat_snapshot", payload, event_id=revision)
                    last_revision = revision
                    last_cursor = int(payload.get("cursor") or last_cursor)
                    last_heartbeat_at = asyncio.get_running_loop().time()
                    if once:
                        return
                else:
                    generic_events = await asyncio.to_thread(
                        service.events_since,
                        last_cursor,
                        limit=event_limit,
                    )
                    if generic_events:
                        last_cursor = int(generic_events[-1]["cursor"])
                        payload = await asyncio.to_thread(
                            service.pma_compat_snapshot, limit=500
                        )
                        revision = str(payload["revision"])
                        if revision != last_revision:
                            yield _sse_frame(
                                "chat_snapshot", payload, event_id=revision
                            )
                            last_revision = revision
                            last_heartbeat_at = asyncio.get_running_loop().time()
                            continue
                    now = asyncio.get_running_loop().time()
                    if now - last_heartbeat_at >= _HEARTBEAT_SECONDS:
                        yield ": keep-alive\n\n"
                        last_heartbeat_at = now
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )


__all__ = ["CHAT_EVENTS_CONTRACT_VERSION", "build_chat_event_routes"]
