from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ...services.pma import get_pma_request_context
from ..shared import SSE_HEADERS
from .managed_thread_route_helpers import (
    _load_chat_binding_metadata_by_thread,
    _serialize_thread_target,
)
from .managed_threads import build_managed_thread_orchestration_service

CHAT_EVENTS_CONTRACT_VERSION = "pma_chat_events.v1"
_POLL_INTERVAL_SECONDS = 1.5
_HEARTBEAT_SECONDS = 15.0


def _serialize_chat_snapshot(request: Request) -> dict[str, Any]:
    context = get_pma_request_context(request)
    service = build_managed_thread_orchestration_service(request)
    threads = service.list_thread_targets(limit=500)
    active_work_by_thread = {
        summary.thread_target_id: summary
        for summary in service.list_active_work_summaries(limit=max(len(threads), 1))
    }
    binding_metadata = _load_chat_binding_metadata_by_thread(context.hub_root)
    payload = {
        "contract_version": CHAT_EVENTS_CONTRACT_VERSION,
        "threads": [
            _serialize_thread_target(
                thread,
                binding_metadata_by_thread=binding_metadata,
                active_work_summary=active_work_by_thread.get(thread.thread_target_id),
            )
            for thread in threads
        ],
    }
    revision_basis = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["revision"] = hashlib.sha256(revision_basis.encode("utf-8")).hexdigest()
    return payload


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
    async def stream_pma_chat_events(request: Request, once: bool = False):
        async def _stream() -> Any:
            last_revision: Optional[str] = None
            last_heartbeat_at = asyncio.get_running_loop().time()
            while True:
                payload = await asyncio.to_thread(_serialize_chat_snapshot, request)
                revision = str(payload["revision"])
                if revision != last_revision:
                    yield _sse_frame("chat_snapshot", payload, event_id=revision)
                    last_revision = revision
                    last_heartbeat_at = asyncio.get_running_loop().time()
                    if once:
                        return
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


__all__ = ["CHAT_EVENTS_CONTRACT_VERSION", "build_chat_event_routes"]
