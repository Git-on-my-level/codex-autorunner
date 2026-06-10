"""Read-model snapshots for chat index and detail (``GET /hub/read-models/chats``).

Emitted with ``dump_read_model_contract`` (camelCase) for ``mapReadModelContract`` consumers.
Older ``GET /hub/chat/index`` hub-shaped snapshots remain for non-SPA tooling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Annotated, Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ....core.managed_thread_store import ManagedThreadStore
from ..read_model_contracts import dump_read_model_contract
from ..services.chat_read_models import (
    ChatIndexContractFilter,
    ChatReadModelService,
    hub_group_dict_to_contract,
)
from ..services.web_artifact_delivery import drain_web_artifact_deliveries_for_thread
from .shared import SSE_HEADERS

if TYPE_CHECKING:
    from ..app_state import HubAppContext

SNAPSHOT_CHAT_INDEX_ROUTE = "/hub/read-models/chats"
CHAT_INDEX_PATCH_ROUTE = "/hub/read-models/chats/patches"
_POLL_INTERVAL_SECONDS = 1.5
__all__ = [
    "ChatReadModelService",
    "build_hub_chat_read_model_router",
    "hub_group_dict_to_contract",
]


def _sse_frame(event: str, payload: dict[str, Any], *, event_id: Optional[str]) -> str:
    event_id_line = f"id: {event_id}\n" if event_id else ""
    return (
        f"event: {event}\n"
        f"{event_id_line}"
        f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"
    )


def _chat_facet_query(
    *,
    categories: Optional[list[str]],
    turn_kinds: Optional[list[str]],
    origin_kinds: Optional[list[str]],
    transports: Optional[list[str]],
    scope_kinds: Optional[list[str]],
    scope_ids: Optional[list[str]],
    agent_kinds: Optional[list[str]],
) -> dict[str, list[str]]:
    return {
        "categories": categories or [],
        "turn_kinds": turn_kinds or [],
        "origin_kinds": origin_kinds or [],
        "transports": transports or [],
        "scope_kinds": scope_kinds or [],
        "scope_ids": scope_ids or [],
        "agent_kinds": agent_kinds or [],
    }


def _advance_chat_detail_stream_cursor(last_cursor: int, batch_cursor: int) -> int:
    return max(last_cursor, batch_cursor)


def build_hub_chat_read_model_router(context: HubAppContext) -> APIRouter:
    router = APIRouter()

    service = ChatReadModelService(context.config.root)

    @router.get(SNAPSHOT_CHAT_INDEX_ROUTE)
    def chat_read_model_index(
        filter_param: Annotated[ChatIndexContractFilter, Query(alias="filter")] = "all",
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        search: Annotated[Optional[str], Query()] = None,
        surface_kind: Annotated[Optional[str], Query()] = None,
        group_by: Annotated[Optional[str], Query()] = None,
        parent_group_id: Annotated[Optional[str], Query()] = None,
        category: Annotated[Optional[list[str]], Query()] = None,
        turn_kind: Annotated[Optional[list[str]], Query()] = None,
        origin_kind: Annotated[Optional[list[str]], Query()] = None,
        transport: Annotated[Optional[list[str]], Query()] = None,
        scope_kind: Annotated[Optional[list[str]], Query()] = None,
        scope_id: Annotated[Optional[list[str]], Query()] = None,
        agent_kind: Annotated[Optional[list[str]], Query()] = None,
        cursor: Annotated[Optional[str], Query()] = None,
    ):
        bounded_offset = _resolve_offset(cursor, offset)
        facets = _chat_facet_query(
            categories=category,
            turn_kinds=turn_kind,
            origin_kinds=origin_kind,
            transports=transport,
            scope_kinds=scope_kind,
            scope_ids=scope_id,
            agent_kinds=agent_kind,
        )
        return dump_read_model_contract(
            service.chat_index_contract(
                filter_param=filter_param,
                query=search,
                surface_kind=surface_kind,
                group_by=group_by,
                parent_group_id=parent_group_id,
                offset=bounded_offset,
                limit=limit,
                facets=facets,
            )
        )

    @router.get(CHAT_INDEX_PATCH_ROUTE)
    async def stream_chat_index_patches(
        request: Request,
        filter_param: Annotated[ChatIndexContractFilter, Query(alias="filter")] = "all",
        search: Annotated[Optional[str], Query()] = None,
        surface_kind: Annotated[Optional[str], Query()] = None,
        group_by: Annotated[Optional[str], Query()] = None,
        parent_group_id: Annotated[Optional[str], Query()] = None,
        category: Annotated[Optional[list[str]], Query()] = None,
        turn_kind: Annotated[Optional[list[str]], Query()] = None,
        origin_kind: Annotated[Optional[list[str]], Query()] = None,
        transport: Annotated[Optional[list[str]], Query()] = None,
        scope_kind: Annotated[Optional[list[str]], Query()] = None,
        scope_id: Annotated[Optional[list[str]], Query()] = None,
        agent_kind: Annotated[Optional[list[str]], Query()] = None,
        cursor: Annotated[Optional[str], Query()] = None,
        event_limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        window_limit: Annotated[int, Query(ge=1, le=200)] = 200,
        once: bool = False,
    ):
        header_cursor = request.headers.get("last-event-id")
        raw_cursor = cursor if cursor is not None else header_cursor
        try:
            parsed_cursor = _parse_chat_index_cursor(raw_cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        facets = _chat_facet_query(
            categories=category,
            turn_kinds=turn_kind,
            origin_kinds=origin_kind,
            transports=transport,
            scope_kinds=scope_kind,
            scope_ids=scope_id,
            agent_kinds=agent_kind,
        )

        async def _stream() -> Any:
            last_cursor = parsed_cursor
            while True:
                batch = await asyncio.to_thread(
                    service.chat_index_patch_contracts,
                    last_cursor,
                    filter_param=filter_param,
                    query=search,
                    surface_kind=surface_kind,
                    group_by=group_by,
                    parent_group_id=parent_group_id,
                    event_limit=event_limit,
                    window_limit=window_limit,
                    facets=facets,
                )
                events = batch["events"]
                batch_cursor = _int_fallback(batch.get("cursor"), last_cursor)
                for event_payload in events:
                    envelope = event_payload.get("envelope") or {}
                    cursor_payload = envelope.get("cursor") or {}
                    sequence = _int_fallback(
                        cursor_payload.get("sequence"), last_cursor
                    )
                    last_cursor = sequence
                    event_type = str(envelope.get("eventType") or "chat.index.patch")
                    yield _sse_frame(
                        event_type,
                        event_payload,
                        event_id=str(sequence),
                    )
                last_cursor = max(last_cursor, batch_cursor)
                if events:
                    if once:
                        return
                    continue
                if once:
                    return
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    @router.get("/hub/read-models/chats/{chat_id}")
    async def chat_read_model_detail(
        chat_id: str,
        timeline_limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        try:
            thread_store = ManagedThreadStore.connect_readonly(
                context.config.root,
                durable=True,
            )
            thread = await asyncio.to_thread(thread_store.get_thread, chat_id)
            try:
                await drain_web_artifact_deliveries_for_thread(
                    thread=thread,
                    managed_thread_id=chat_id,
                    logger=logging.getLogger("codex_autorunner.web_artifact_delivery"),
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to drain web artifact deliveries before chat detail "
                    "snapshot (managed_thread_id=%s)",
                    chat_id,
                )
            return dump_read_model_contract(
                await asyncio.to_thread(
                    service.chat_detail_contract,
                    chat_id,
                    timeline_limit=timeline_limit,
                )
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Managed thread not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/hub/read-models/chats/{chat_id}/runtime-chain")
    def chat_runtime_chain(
        chat_id: str,
        execution_id: Annotated[Optional[str], Query()] = None,
        automation_job_id: Annotated[Optional[str], Query()] = None,
        automation_child_edge_id: Annotated[Optional[str], Query()] = None,
    ):
        return service.runtime_chain(
            chat_id=chat_id,
            managed_thread_id=chat_id,
            execution_id=execution_id,
            automation_job_id=automation_job_id,
            automation_child_edge_id=automation_child_edge_id,
        )

    @router.get("/hub/read-models/chats/{chat_id}/patches")
    async def stream_chat_detail_patches(
        request: Request,
        chat_id: str,
        cursor: Annotated[Optional[str], Query()] = None,
        event_limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        once: bool = False,
    ):
        header_cursor = request.headers.get("last-event-id")
        raw_cursor = cursor if cursor is not None else header_cursor
        try:
            parsed_cursor = _parse_chat_detail_cursor(raw_cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def _stream() -> Any:
            last_cursor = parsed_cursor
            while True:
                batch = await asyncio.to_thread(
                    service.chat_detail_patch_contracts,
                    chat_id,
                    last_cursor,
                    event_limit=event_limit,
                )
                events = batch["events"]
                batch_cursor = _int_fallback(batch.get("cursor"), last_cursor)
                for event_payload in events:
                    envelope = event_payload.get("envelope") or {}
                    cursor_payload = envelope.get("cursor") or {}
                    sequence = _int_fallback(
                        cursor_payload.get("sequence"), last_cursor
                    )
                    last_cursor = sequence
                    yield _sse_frame(
                        str(envelope.get("eventType") or "chat.detail.patch"),
                        event_payload,
                        event_id=str(sequence),
                    )
                last_cursor = _advance_chat_detail_stream_cursor(
                    last_cursor, batch_cursor
                )
                if events:
                    if once:
                        return
                    continue
                if once:
                    return
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return router


def _resolve_offset(cursor: Optional[str], offset_default: int) -> int:
    if cursor:
        stripped = cursor.strip()
        try:
            return max(0, int(stripped))
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid cursor offset",
            ) from exc
    return offset_default


def _parse_chat_index_cursor(raw: Any) -> int:
    value = _str_or_none(raw)
    if value is None:
        return 0
    if value.startswith("chat.index:"):
        value = value.split(":", 1)[1]
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("cursor must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError("cursor must be a non-negative integer")
    return parsed


def _parse_chat_detail_cursor(raw: Any) -> int:
    value = _str_or_none(raw)
    if value is None:
        return 0
    if value.startswith("chat.detail:"):
        value = value.split(":", 1)[1]
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("cursor must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError("cursor must be a non-negative integer")
    return parsed


def _str_or_none(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _int_fallback(value: Any, fallback: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return fallback
    return fallback
