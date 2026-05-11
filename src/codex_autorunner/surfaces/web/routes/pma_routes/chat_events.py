from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .....core.orchestration.sqlite import open_orchestration_sqlite
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


def _latest_text(values: list[Any]) -> str:
    normalized = [
        str(value or "").strip() for value in values if str(value or "").strip()
    ]
    return max(normalized) if normalized else ""


def _chat_event_revision(hub_root: Any) -> str:
    with open_orchestration_sqlite(hub_root, migrate=True) as conn:
        thread_row = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   MAX(updated_at) AS updated_at,
                   MAX(status_updated_at) AS status_updated_at
              FROM orch_thread_targets
            """
        ).fetchone()
        binding_row = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   MAX(updated_at) AS updated_at,
                   MAX(disabled_at) AS disabled_at
              FROM orch_bindings
             WHERE target_kind = 'thread'
            """
        ).fetchone()
        execution_row = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   MAX(created_at) AS created_at,
                   MAX(started_at) AS started_at,
                   MAX(finished_at) AS finished_at
              FROM orch_thread_executions
            """
        ).fetchone()
    parts = {
        "threads": int(thread_row["count"] or 0),
        "thread_at": _latest_text(
            [thread_row["updated_at"], thread_row["status_updated_at"]]
        ),
        "bindings": int(binding_row["count"] or 0),
        "binding_at": _latest_text(
            [binding_row["updated_at"], binding_row["disabled_at"]]
        ),
        "executions": int(execution_row["count"] or 0),
        "execution_at": _latest_text(
            [
                execution_row["created_at"],
                execution_row["started_at"],
                execution_row["finished_at"],
            ]
        ),
    }
    revision_basis = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(revision_basis.encode("utf-8")).hexdigest()


def _serialize_chat_snapshot(request: Request, *, revision: str) -> dict[str, Any]:
    context = get_pma_request_context(request)
    service = build_managed_thread_orchestration_service(request)
    threads = service.list_thread_targets(limit=500)
    active_work_by_thread = {
        summary.thread_target_id: summary
        for summary in service.list_active_work_summaries(limit=max(len(threads), 1))
    }
    binding_metadata = _load_chat_binding_metadata_by_thread(context.hub_root)
    return {
        "contract_version": CHAT_EVENTS_CONTRACT_VERSION,
        "revision": revision,
        "threads": [
            _serialize_thread_target(
                thread,
                binding_metadata_by_thread=binding_metadata,
                active_work_summary=active_work_by_thread.get(thread.thread_target_id),
            )
            for thread in threads
        ],
    }


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
                revision = await asyncio.to_thread(
                    _chat_event_revision,
                    get_pma_request_context(request).hub_root,
                )
                if revision != last_revision:
                    payload = await asyncio.to_thread(
                        _serialize_chat_snapshot,
                        request,
                        revision=revision,
                    )
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
