from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ....core.orchestration import (
    ChatSurfaceReadService,
    parse_chat_surface_cursor,
)
from ..app_state import HubAppContext
from .shared import SSE_HEADERS

_POLL_INTERVAL_SECONDS = 1.5
_HEARTBEAT_SECONDS = 15.0


def _sse_frame(event: str, payload: dict[str, Any], *, event_id: Optional[str]) -> str:
    event_id_line = f"id: {event_id}\n" if event_id else ""
    return (
        f"event: {event}\n"
        f"{event_id_line}"
        f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"
    )


def build_hub_chat_event_routes(context: HubAppContext) -> APIRouter:
    router = APIRouter(prefix="/hub/chat", tags=["chat"])

    @router.get("/events")
    async def stream_chat_events(
        request: Request,
        cursor: Optional[str] = None,
        limit: int = Query(500, ge=1, le=1000),
        event_limit: int = Query(100, ge=1, le=1000),
        once: bool = False,
        include_heartbeat: bool = False,
    ):
        header_cursor = request.headers.get("last-event-id")
        raw_cursor = cursor if cursor is not None else header_cursor
        try:
            parsed_cursor = parse_chat_surface_cursor(raw_cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        service = ChatSurfaceReadService(context.config.root, durable=True)

        async def _stream() -> Any:
            snapshot = await asyncio.to_thread(service.snapshot, limit=limit)
            yield _sse_frame(
                "chat.snapshot",
                snapshot,
                event_id=str(snapshot["cursor"]),
            )
            if include_heartbeat:
                yield ": keep-alive\n\n"

            last_cursor = parsed_cursor
            pending = await asyncio.to_thread(
                service.events_since,
                parsed_cursor,
                limit=event_limit,
            )
            for event_payload in pending:
                last_cursor = int(event_payload["cursor"])
                yield _sse_frame(
                    "chat.event",
                    event_payload,
                    event_id=str(event_payload["cursor"]),
                )

            if once:
                return

            last_heartbeat_at = asyncio.get_running_loop().time()
            while True:
                events = await asyncio.to_thread(
                    service.events_since,
                    last_cursor,
                    limit=event_limit,
                )
                if events:
                    for event_payload in events:
                        last_cursor = int(event_payload["cursor"])
                        yield _sse_frame(
                            "chat.event",
                            event_payload,
                            event_id=str(event_payload["cursor"]),
                        )
                    last_heartbeat_at = asyncio.get_running_loop().time()
                else:
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

    return router


__all__ = ["build_hub_chat_event_routes"]
