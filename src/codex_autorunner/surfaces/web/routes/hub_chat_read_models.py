"""Read-model snapshots for PMA chat index and detail (``GET /hub/read-models/chats``).

Emitted with ``dump_read_model_contract`` (camelCase) for ``mapReadModelContract`` consumers.
Older ``GET /hub/chat/index`` hub-shaped snapshots remain for non-SPA tooling.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    Mapping,
    Optional,
    Union,
    cast,
)

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from codex_autorunner.core.managed_thread_kinds import (
    ManagedThreadChatKind,
    normalize_managed_thread_chat_kind,
)
from codex_autorunner.core.orchestration import ChatSurfaceReadService

from ..read_model_contracts import (
    ChatArtifactSummary,
    ChatDetailPatch,
    ChatDetailPatchEvent,
    ChatDetailSnapshot,
    ChatIndexCounters,
    ChatIndexGroup,
    ChatIndexPatch,
    ChatIndexPatchEvent,
    ChatIndexRow,
    ChatIndexSnapshot,
    ChatQueueSummary,
    ChatThreadProjection,
    ChatTimelineIdentity,
    ChatTimelineItem,
    ChatTimelineProvenance,
    PageWindow,
    ProjectionCursor,
    ReadModelEventEnvelope,
    RepairPolicy,
    dump_read_model_contract,
    read_model_now,
)
from .shared import SSE_HEADERS

if TYPE_CHECKING:
    from ..app_state import HubAppContext

ChatIndexContractFilter = Literal[
    "all", "waiting", "active", "unread", "archived", "ticket_runs", "external"
]
ChatSurfaceStatus = Literal["waiting", "running", "idle", "archived", "failed"]
SurfaceLiteral = Literal[
    "pma", "file_chat", "telegram", "discord", "app_server", "other"
]

SNAPSHOT_CHAT_INDEX_ROUTE = "/hub/read-models/chats"
CHAT_INDEX_PATCH_ROUTE = "/hub/read-models/chats/patches"
_POLL_INTERVAL_SECONDS = 1.5


def _sse_frame(event: str, payload: dict[str, Any], *, event_id: Optional[str]) -> str:
    event_id_line = f"id: {event_id}\n" if event_id else ""
    return (
        f"event: {event}\n"
        f"{event_id_line}"
        f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"
    )


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
        cursor: Annotated[Optional[str], Query()] = None,
    ):
        bounded_offset = _resolve_offset(cursor, offset)
        return dump_read_model_contract(
            service.chat_index_contract(
                filter_param=filter_param,
                query=search,
                surface_kind=surface_kind,
                group_by=group_by,
                parent_group_id=parent_group_id,
                offset=bounded_offset,
                limit=limit,
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
    def chat_read_model_detail(
        chat_id: str,
        timeline_limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        try:
            return dump_read_model_contract(
                service.chat_detail_contract(
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


class ChatReadModelService:
    """Thin read-model façade over ChatSurfaceReadService."""

    def __init__(self, hub_root):
        self._surface = ChatSurfaceReadService(hub_root, durable=True)

    def chat_index_contract(
        self,
        *,
        filter_param: ChatIndexContractFilter,
        query: Optional[str],
        surface_kind: Optional[str],
        group_by: Optional[str],
        parent_group_id: Optional[str],
        offset: int,
        limit: int,
    ) -> ChatIndexSnapshot:
        hub_view = _contract_filter_to_hub_view(filter_param)
        payload = self._surface.chat_index_snapshot(
            view=hub_view,
            query=query,
            surface_kind=surface_kind,
            group_by=group_by,
            parent_group_id=parent_group_id,
            offset=offset,
            limit=limit,
        )
        hub_window = payload.get("window") or {}
        hub_rows_raw = payload.get("rows") or []
        hub_rows = hub_rows_raw if isinstance(hub_rows_raw, list) else []
        hub_groups_payload_raw = payload.get("groups") or []
        hub_groups_payload = (
            hub_groups_payload_raw if isinstance(hub_groups_payload_raw, list) else []
        )

        rows: list[ChatIndexRow] = []
        groups_contract: list[ChatIndexGroup] = []
        for raw in hub_rows:
            if not isinstance(raw, dict):
                continue
            row_type = str(raw.get("row_type") or "chat").lower()
            if row_type == "group":
                groups_contract.append(
                    hub_group_dict_to_contract(cast(dict[str, Any], raw))
                )
                continue
            rows.append(hub_chat_row_to_chat_index_row(cast(dict[str, Any], raw)))

        existing_group_ids = {g.group_id for g in groups_contract}
        for raw in hub_groups_payload:
            if not isinstance(raw, dict):
                continue
            grp = hub_group_dict_to_contract(cast(dict[str, Any], raw))
            if grp.group_id not in existing_group_ids:
                groups_contract.append(grp)

        total = _int_fallback(hub_window.get("total_count"), len(hub_rows))
        hub_off = _int_fallback(hub_window.get("offset"), offset)
        returned_len = len(hub_rows)
        win_limit = max(1, _int_fallback(hub_window.get("limit"), limit))
        proj_cursor = projection_cursor_chat_index(
            _int_fallback(payload.get("cursor"), 0)
        )
        counters = counters_from_hub_payload(payload.get("counters"), rows, total)

        return ChatIndexSnapshot(
            cursor=proj_cursor,
            window=PageWindow(
                limit=win_limit,
                next_cursor=(
                    str(hub_off + returned_len)
                    if bool(hub_window.get("has_more"))
                    else None
                ),
                previous_cursor=(
                    str(max(0, hub_off - win_limit)) if hub_off > 0 else None
                ),
                total_estimate=total,
                total_is_exact=True,
            ),
            filter=filter_param,
            query=query,
            rows=rows,
            groups=groups_contract,
            counters=counters,
            repair=RepairPolicy(snapshot_route=SNAPSHOT_CHAT_INDEX_ROUTE),
        )

    def chat_index_patch_contracts(
        self,
        cursor: Optional[int],
        *,
        filter_param: ChatIndexContractFilter,
        query: Optional[str],
        surface_kind: Optional[str],
        group_by: Optional[str],
        parent_group_id: Optional[str],
        event_limit: int,
        window_limit: int,
    ) -> dict[str, Any]:
        hub_view = _contract_filter_to_hub_view(filter_param)
        batch = self._surface.chat_index_patch_batch(
            cursor,
            view=hub_view,
            query=query,
            surface_kind=surface_kind,
            group_by=group_by,
            parent_group_id=parent_group_id,
            limit=event_limit,
            window_limit=window_limit,
        )
        events = [
            dump_read_model_contract(self._chat_index_patch_event_from_hub(raw))
            for raw in batch.get("events") or []
            if isinstance(raw, dict)
        ]
        return {"cursor": batch.get("cursor"), "events": events}

    def _chat_index_patch_event_from_hub(
        self, raw: Mapping[str, Any]
    ) -> ChatIndexPatchEvent:
        envelope_raw = raw.get("envelope") or {}
        envelope_map = envelope_raw if isinstance(envelope_raw, Mapping) else {}
        sequence = _int_fallback(envelope_map.get("cursor"), 0)
        event_type = str(envelope_map.get("event_type") or "chat.index.patch")
        operation = str(envelope_map.get("operation") or "patch")
        if operation not in {
            "upsert",
            "patch",
            "delete",
            "reorder",
            "invalidate",
            "reset",
        }:
            operation = "patch"
        generated_at = (
            parse_iso_optional(str(envelope_map.get("generated_at") or ""))
            or read_model_now()
        )
        patch_raw = raw.get("patch") or {}
        patch_map = patch_raw if isinstance(patch_raw, Mapping) else {}
        rows = [
            hub_chat_row_to_chat_index_row(cast(Mapping[str, Any], row))
            for row in patch_map.get("rows") or []
            if isinstance(row, Mapping)
        ]
        groups = [
            hub_group_dict_to_contract(cast(Mapping[str, Any], group))
            for group in patch_map.get("groups") or []
            if isinstance(group, Mapping)
        ]
        counters = _chat_index_counters_from_raw(patch_map.get("counters"), rows)
        return ChatIndexPatchEvent(
            envelope=ReadModelEventEnvelope(
                event_type=event_type,
                cursor=projection_cursor_chat_index(sequence),
                entity_kind=str(envelope_map.get("entity_kind") or "chat"),
                entity_id=str(envelope_map.get("entity_id") or "chat.index"),
                operation=cast(
                    Literal[
                        "upsert",
                        "patch",
                        "delete",
                        "reorder",
                        "invalidate",
                        "reset",
                    ],
                    operation,
                ),
                generated_at=generated_at,
            ),
            patch=ChatIndexPatch(
                rows=rows,
                groups=groups,
                removed_row_ids=_str_list(patch_map.get("removed_row_ids")),
                removed_group_ids=_str_list(patch_map.get("removed_group_ids")),
                order=(
                    _str_list(patch_map.get("order"))
                    if patch_map.get("order") is not None
                    else None
                ),
                counters=counters,
            ),
        )

    def chat_detail_contract(
        self,
        chat_id: str,
        *,
        timeline_limit: int,
    ) -> ChatDetailSnapshot:
        raw_any = self._surface.chat_detail_snapshot(
            chat_id,
            timeline_limit=timeline_limit,
        )
        raw = cast(dict[str, Any], raw_any)
        thread_row = hub_thread_detail_to_projection(
            cast(Mapping[str, Any], raw.get("thread") or {})
        )

        timeline_block = raw.get("timeline") or {}
        if not isinstance(timeline_block, dict):
            timeline_block = {}
        tl_window = timeline_block.get("window") or {}
        if not isinstance(tl_window, dict):
            tl_window = {}
        items_raw = timeline_block.get("items") or []
        items_list = items_raw if isinstance(items_raw, list) else []

        timeline_items = [
            hub_timeline_item_dict_to_contract(cast(dict[str, Any], item))
            for item in items_list
            if isinstance(item, dict)
        ]

        qs = raw.get("queue_summary") or {}
        if not isinstance(qs, dict):
            qs = {}

        queued = qs.get("items") or []
        queued_list = queued if isinstance(queued, list) else []

        queued_turn_ids: list[str] = []
        for entry in queued_list:
            if not isinstance(entry, dict):
                continue
            mt = entry.get("managed_turn_id") or entry.get("turn_id")
            text = _str_or_none(mt)
            if text:
                queued_turn_ids.append(text)

        artifact_payloads = raw.get("artifacts") or []
        artifact_list = artifact_payloads if isinstance(artifact_payloads, list) else []

        artifacts: list[ChatArtifactSummary] = []
        for artifact in artifact_list:
            if not isinstance(artifact, dict):
                continue
            summary = artifact_summary_from_hub(cast(dict[str, Any], artifact))
            if summary is not None:
                artifacts.append(summary)

        active_turn = raw.get("active_turn_status")
        active_turn_id: Optional[str] = None
        if isinstance(active_turn, dict):
            active_turn_id = _str_or_none(active_turn.get("managed_turn_id"))

        tl_page = PageWindow(
            limit=_int_fallback(tl_window.get("limit"), timeline_limit),
            next_cursor=None,
            previous_cursor=(
                _str_or_none(tl_window.get("oldest_order_key"))
                if tl_window.get("has_older")
                else None
            ),
            total_estimate=_int_fallback(
                timeline_block.get("item_count"), len(timeline_items)
            ),
            total_is_exact=True,
        )

        encode_chat = chat_id.strip() or ""

        return ChatDetailSnapshot(
            cursor=projection_cursor_chat_detail(raw.get("cursor")),
            thread=thread_row,
            timeline_window=tl_page,
            timeline=timeline_items,
            queue=ChatQueueSummary(
                depth=_int_fallback(qs.get("depth"), 0),
                active_turn_id=active_turn_id,
                queued_turn_ids=queued_turn_ids,
            ),
            artifacts=artifacts,
            repair=RepairPolicy(
                snapshot_route=f"/hub/read-models/chats/{encode_chat}",
            ),
        )

    def chat_detail_patch_contracts(
        self,
        chat_id: str,
        cursor: Optional[int],
        *,
        event_limit: int,
    ) -> dict[str, Any]:
        batch = self._surface.chat_patches_since(cursor, limit=event_limit)
        events = [
            dump_read_model_contract(
                self._chat_detail_patch_event_from_hub(chat_id, raw)
            )
            for raw in batch.get("patches") or []
            if isinstance(raw, Mapping)
            and str(raw.get("managed_thread_id") or "") == chat_id
        ]
        return {"cursor": batch.get("cursor"), "events": events}

    def _chat_detail_patch_event_from_hub(
        self,
        chat_id: str,
        raw: Mapping[str, Any],
    ) -> ChatDetailPatchEvent:
        sequence = _int_fallback(raw.get("cursor"), 0)
        occurred = (
            parse_iso_optional(str(raw.get("occurred_at") or "")) or read_model_now()
        )
        return ChatDetailPatchEvent(
            envelope=ReadModelEventEnvelope(
                event_type="chat.detail.patch",
                cursor=projection_cursor_chat_detail(sequence),
                entity_kind="chat",
                entity_id=chat_id,
                operation="reset",
                generated_at=occurred,
            ),
            patch=ChatDetailPatch(),
        )


def _contract_filter_to_hub_view(contract_filter: str) -> str:
    return "ticket_run" if contract_filter == "ticket_runs" else contract_filter


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


def _chat_index_counters_from_raw(
    raw: Any, rows: list[ChatIndexRow]
) -> Optional[ChatIndexCounters]:
    if not isinstance(raw, Mapping):
        return None
    total = _int_fallback(raw.get("total"), len(rows))
    return ChatIndexCounters(
        total=max(0, total),
        waiting=max(0, _int_fallback(raw.get("waiting"), 0)),
        running=max(0, _int_fallback(raw.get("running"), 0)),
        unread=max(0, _int_fallback(raw.get("unread"), 0)),
        archived=max(0, _int_fallback(raw.get("archived"), 0)),
    )


def parse_iso_optional(raw: str) -> Optional[datetime]:
    stripped = raw.strip()
    try:
        if stripped.endswith("Z"):
            stripped = stripped[:-1] + "+00:00"
        return datetime.fromisoformat(stripped).astimezone()
    except (ValueError, TypeError):
        return None


def _normalize_kind_text(kind: Optional[str]) -> str:
    return kind.strip().lower() if isinstance(kind, str) else ""


def surface_from_hub_row(row: Mapping[str, Any]) -> SurfaceLiteral:
    kinds_raw = row.get("surface_kinds")
    kinds: list[str] = []
    if isinstance(kinds_raw, list):
        kinds = [str(k).strip().lower() for k in kinds_raw if k is not None]
    lowered = set(kinds)

    surf = row.get("surface")
    if isinstance(surf, dict):
        fk = surf.get("surface_kind")
        if fk is not None:
            lowered.add(str(fk).strip().lower())

    if "discord" in lowered:
        return "discord"
    if "telegram" in lowered:
        return "telegram"
    if "app_server" in lowered:
        return "app_server"
    if "file_chat" in lowered:
        return "file_chat"
    if lowered == {"other"} or (
        lowered and all(str(k) == "other" or not str(k).strip() for k in lowered)
    ):
        return "other"
    return "pma"


def _chat_surface_status_from_row(row: Mapping[str, Any]) -> ChatSurfaceStatus:
    lifecycle = str(row.get("lifecycle") or "").strip().lower()
    lifecycle_status = str(row.get("lifecycle_status") or "").strip().lower()
    runtime = str(row.get("runtime_status") or row.get("target_runtime_status") or "")
    runtime_l = runtime.strip().lower()
    archive_state = str(row.get("archive_state") or "").strip().lower()
    if lifecycle_status == "archived" or archive_state == "archived":
        return "archived"
    if row.get("managed_thread_id") is None and (
        lifecycle == "archived" or runtime_l == "archived"
    ):
        return "archived"
    queue_depth = _int_fallback(row.get("queue_depth"), 0)
    if queue_depth > 0:
        return "waiting"
    if runtime_l in {
        "completed",
        "complete",
        "ok",
        "succeeded",
        "success",
        "delivered",
    }:
        return "idle"
    if runtime_l in {"failed", "error", "blocked", "invalid"}:
        return "failed"
    if lifecycle == "running" or runtime_l == "running":
        return "running"
    return "idle"


def hub_chat_row_to_chat_index_row(raw: Mapping[str, Any]) -> ChatIndexRow:
    managed_thread_id = _str_or_none(raw.get("managed_thread_id"))
    row_id = _str_or_none(raw.get("row_id")) or "unknown-chat-row"
    chat_id = managed_thread_id or row_id

    resource_kind_norm = _normalize_kind_text(_str_or_none(raw.get("resource_kind")))
    resource_id = _str_or_none(raw.get("resource_id"))

    ticket_id: Optional[str] = None
    if resource_kind_norm == "ticket" and resource_id:
        ticket_id = resource_id
    else:
        ticket_id = _str_or_none(raw.get("ticket_id") or raw.get("current_ticket_id"))

    run_id: Optional[str] = None
    if resource_kind_norm in {"run", "ticket_run"} and resource_id:
        run_id = resource_id
    else:
        run_id = _str_or_none(raw.get("run_id"))

    unread_count = _int_fallback(raw.get("unread_count"), 0)
    if raw.get("unread") is True and unread_count == 0:
        unread_count = 1

    last_iso = (
        _str_or_none(raw.get("last_activity_at"))
        or _str_or_none(raw.get("updated_at"))
        or _str_or_none(raw.get("created_at"))
    )
    last_activity: Optional[datetime] = None
    if last_iso:
        last_activity = parse_iso_optional(last_iso)

    repo_id_out = _str_or_none(raw.get("repo_id"))
    worktree_id_out: Optional[str] = (
        resource_id if resource_kind_norm == "worktree" and resource_id else None
    )

    ck_type: ManagedThreadChatKind | None = normalize_managed_thread_chat_kind(
        raw.get("chat_kind") or raw.get("thread_kind"),
        default=None,
    )

    normalized_status = _chat_surface_status_from_row(raw)
    surface_bindings = [
        dict(cast(Mapping[str, Any], surface))
        for surface in raw.get("surface_bindings") or raw.get("surfaces") or []
        if isinstance(surface, Mapping)
    ]
    primary_surface = raw.get("primary_surface") or raw.get("surface")
    primary_surface_out = (
        dict(cast(Mapping[str, Any], primary_surface))
        if isinstance(primary_surface, Mapping)
        else None
    )
    binding_display_names = _str_list(raw.get("binding_display_names"))
    binding_display_name = _str_or_none(raw.get("binding_display_name")) or (
        binding_display_names[0] if binding_display_names else None
    )

    return ChatIndexRow(
        chat_id=chat_id,
        surface=surface_from_hub_row(raw),
        title=str(raw.get("title") or chat_id),
        display_title=_str_or_none(raw.get("display_title"))
        or _str_or_none(raw.get("chat_display_name")),
        technical_title=_str_or_none(raw.get("technical_title")),
        primary_surface=primary_surface_out,
        surface_bindings=surface_bindings,
        binding_display_name=binding_display_name,
        binding_display_names=binding_display_names,
        lifecycle=_str_or_none(raw.get("lifecycle")),
        runtime_status=_str_or_none(
            raw.get("runtime_status") or raw.get("target_runtime_status")
        ),
        archive_state=(
            "archived"
            if _normalize_kind_text(_str_or_none(raw.get("archive_state")))
            == "archived"
            or _normalize_kind_text(_str_or_none(raw.get("lifecycle_status")))
            == "archived"
            else "active"
        ),
        status=normalized_status,
        unread_count=unread_count,
        last_activity_at=last_activity,
        sort_key=(
            dict(cast(Mapping[str, Any], raw.get("sort_key")))
            if isinstance(raw.get("sort_key"), Mapping)
            else None
        ),
        resource_kind=_str_or_none(raw.get("resource_kind")),
        resource_id=_str_or_none(raw.get("resource_id")),
        workspace_root=_str_or_none(raw.get("workspace_root")),
        repo_id=repo_id_out,
        worktree_id=worktree_id_out,
        ticket_id=ticket_id,
        run_id=run_id,
        agent=_str_or_none(raw.get("agent") or raw.get("agent_id")),
        agent_profile=_str_or_none(raw.get("agent_profile")),
        chat_kind=ck_type,
        model=_str_or_none(raw.get("model")),
        group_id=_str_or_none(raw.get("group_id")),
    )


def hub_group_dict_to_contract(raw: Mapping[str, Any]) -> ChatIndexGroup:
    group_id = str(raw.get("group_id") or raw.get("row_id") or "").strip()
    kind: Literal["ticket_run", "surface", "repo", "worktree"] = (
        "ticket_run"
        if group_id.startswith(("ticket:", "run:", "ticket-run:"))
        else "surface"
    )
    label = str(raw.get("title") or group_id)
    child_count = max(0, _int_fallback(raw.get("child_count"), 0))

    return ChatIndexGroup(
        group_id=group_id,
        kind=kind,
        label=label,
        child_count=child_count,
        expanded_child_window=None,
    )


def counters_from_contract_rows(
    rows: list[ChatIndexRow], total: int
) -> ChatIndexCounters:
    return ChatIndexCounters(
        total=max(0, total),
        waiting=sum(1 for r in rows if r.status == "waiting"),
        running=sum(1 for r in rows if r.status == "running"),
        unread=sum(r.unread_count for r in rows),
        archived=sum(1 for r in rows if r.status == "archived"),
    )


def counters_from_hub_payload(
    raw: Any, rows: list[ChatIndexRow], total: int
) -> ChatIndexCounters:
    if isinstance(raw, Mapping):
        return ChatIndexCounters(
            total=max(0, _int_fallback(raw.get("total"), total)),
            waiting=max(0, _int_fallback(raw.get("waiting"), 0)),
            running=max(0, _int_fallback(raw.get("running"), 0)),
            unread=max(0, _int_fallback(raw.get("unread"), 0)),
            archived=max(0, _int_fallback(raw.get("archived"), 0)),
        )
    return counters_from_contract_rows(rows, total)


def projection_cursor_chat_index(sequence: Union[int, str]) -> ProjectionCursor:
    if isinstance(sequence, int):
        seq_int = sequence
    else:
        digits = str(sequence).strip()
        seq_int = int(digits) if digits.isdigit() else 1
    seq_eff = seq_int if seq_int > 0 else 1
    return ProjectionCursor(
        value=f"chat.index:{seq_eff}",
        sequence=seq_eff,
        source="chat.index.projection",
        issued_at=read_model_now(),
    )


def projection_cursor_chat_detail(cursor_value: Any) -> ProjectionCursor:
    seq_int = (
        cursor_value
        if isinstance(cursor_value, int)
        else _int_fallback(cursor_value, 1)
    )
    if seq_int <= 0:
        seq_eff = 1
    else:
        seq_eff = seq_int
    return ProjectionCursor(
        value=f"chat.detail:{seq_eff}",
        sequence=seq_eff,
        source="chat.surface.journal",
        issued_at=read_model_now(),
    )


def _detail_thread_as_index_shape(thread: Mapping[str, Any]) -> dict[str, Any]:
    """Shape ``_chat_detail_thread_metadata`` payloads like chat index rows (TS parity)."""

    md = thread.get("metadata")
    meta = dict(cast(Mapping[Any, Any], md)) if isinstance(md, Mapping) else {}

    surfaces_raw = thread.get("surfaces")
    surface_list = surfaces_raw if isinstance(surfaces_raw, list) else []
    surface_kinds: list[str] = []
    first_surface_dict: Optional[dict[str, Any]] = None
    for surf in surface_list:
        if not isinstance(surf, dict):
            continue
        fk = surf.get("surface_kind")
        if fk is not None:
            surface_kinds.append(str(fk))
            if first_surface_dict is None:
                first_surface_dict = dict(cast(Mapping[str, Any], surf))

    return {
        "managed_thread_id": thread.get("managed_thread_id")
        or thread.get("thread_target_id"),
        "row_id": thread.get("managed_thread_id") or thread.get("thread_target_id"),
        "title": thread.get("title"),
        "display_name": thread.get("chat_display_name") or thread.get("display_name"),
        "lifecycle": thread.get("lifecycle"),
        "lifecycle_status": thread.get("lifecycle_status") or thread.get("status"),
        "runtime_status": thread.get("runtime_status")
        or thread.get("normalized_status"),
        "target_runtime_status": thread.get("target_runtime_status"),
        "queue_depth": 0,
        "resource_kind": thread.get("resource_kind"),
        "resource_id": thread.get("resource_id"),
        "repo_id": thread.get("repo_id"),
        "worktree_id": thread.get("worktree_id") or thread.get("worktree_repo_id"),
        "agent": thread.get("agent") or thread.get("agent_id"),
        "agent_id": thread.get("agent_id"),
        "agent_profile": thread.get("agent_profile") or meta.get("agent_profile"),
        "model": thread.get("model") or meta.get("model"),
        "chat_kind": meta.get("chat_kind") or meta.get("thread_kind"),
        "thread_kind": meta.get("thread_kind"),
        "surface_kinds": surface_kinds,
        "surface": first_surface_dict,
        "unread_count": thread.get("unread_count", 0),
        "unread": thread.get("unread"),
        "last_activity_at": thread.get("last_activity_at"),
        "updated_at": thread.get("updated_at"),
        "created_at": thread.get("created_at"),
        "ticket_id": thread.get("ticket_id"),
        "current_ticket_id": thread.get("current_ticket_id"),
        "run_id": thread.get("run_id"),
        "group_id": thread.get("group_id"),
    }


def hub_thread_detail_to_projection(thread: Mapping[str, Any]) -> ChatThreadProjection:
    row = hub_chat_row_to_chat_index_row(_detail_thread_as_index_shape(thread))
    return ChatThreadProjection(
        chat_id=row.chat_id,
        surface=str(row.surface),
        title=row.title,
        status=row.status,
        repo_id=row.repo_id,
        worktree_id=row.worktree_id,
        ticket_id=row.ticket_id,
        run_id=row.run_id,
        agent=row.agent,
        agent_profile=row.agent_profile,
        chat_kind=row.chat_kind,
        model=row.model,
        archived=row.status == "archived",
    )


_ChatTimelineKind = Literal[
    "user_message",
    "assistant_message",
    "tool_event",
    "progress",
    "artifact",
    "system",
]


def _timeline_kind(value: Any) -> _ChatTimelineKind:
    text = str(value or "").strip().lower()
    if text in {"assistant_message", "assistant"}:
        return "assistant_message"
    if text in {"user_message", "user"}:
        return "user_message"
    if text in {"tool_event", "tool"}:
        return "tool_event"
    if text == "progress":
        return "progress"
    if text == "artifact":
        return "artifact"
    if text == "system":
        return "system"
    return "system"


def _timeline_role(
    value: Any,
) -> Optional[Literal["user", "assistant", "tool", "system"]]:
    text = str(value or "").strip().lower()
    if text in {"user", "assistant", "tool", "system"}:
        return text  # type: ignore[return-value]
    return None


def _timeline_section(
    kind: _ChatTimelineKind,
    raw_kind: Any,
) -> Literal[
    "user_message",
    "activity",
    "assistant_message",
    "terminal_metadata",
    "thread_metadata",
]:
    text = str(raw_kind or "").strip().lower()
    if kind == "user_message":
        return "user_message"
    if kind == "assistant_message":
        return "assistant_message"
    if text in {"status", "delivery", "terminal_metadata"}:
        return "terminal_metadata"
    if text in {"lifecycle", "compaction", "thread_metadata"}:
        return "thread_metadata"
    return "activity"


def _timeline_section_order(section: str) -> int:
    order = {
        "user_message": 10,
        "activity": 20,
        "assistant_message": 30,
        "terminal_metadata": 40,
        "thread_metadata": 50,
    }
    return order.get(section, 90)


def _str_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if v is not None and str(v).strip()]


def _any_list(values: Any) -> list[Any]:
    return list(values) if isinstance(values, list) else []


def hub_timeline_item_dict_to_contract(raw: dict[str, Any]) -> ChatTimelineItem:
    identity_raw = raw.get("identity")
    if not isinstance(identity_raw, dict):
        raise ValueError(
            "timeline item missing identity (expected dict with timeline_item_id)"
        )
    timeline_item_id = _str_or_none(identity_raw.get("timeline_item_id"))
    if not timeline_item_id:
        raise ValueError("timeline identity.timeline_item_id is required")

    prov_raw = raw.get("provenance")
    if not isinstance(prov_raw, dict):
        raise ValueError("timeline item missing provenance object")

    created_raw = raw.get("timestamp") or raw.get("created_at")
    created_txt = (
        created_raw.isoformat()
        if isinstance(created_raw, datetime)
        else _str_or_none(str(created_raw) if created_raw is not None else None)
        or "1970-01-01T00:00:00+00:00"
    )
    created_dt = parse_iso_optional(str(created_txt))
    if created_dt is None:
        raise ValueError("timeline item has invalid timestamp/created_at")

    item_id = _str_or_none(raw.get("item_id") or raw.get("id")) or "timeline-item"

    identity = ChatTimelineIdentity(
        timeline_item_id=timeline_item_id,
        progress_item_ids=_str_list(identity_raw.get("progress_item_ids")),
        correlation_id=_str_or_none(identity_raw.get("correlation_id")),
    )
    provenance = ChatTimelineProvenance(
        source_event_ids=_any_list(prov_raw.get("source_event_ids")),
        progress_event_ids=_any_list(prov_raw.get("progress_event_ids")),
        cursor_event_id=_str_or_none(prov_raw.get("cursor_event_id")),
    )

    payload_raw = raw.get("payload")
    payload = payload_raw if isinstance(payload_raw, Mapping) else {}
    kind = _timeline_kind(raw.get("kind"))
    section = _timeline_section(kind, raw.get("kind"))
    text_val = (
        raw.get("text")
        or raw.get("summary")
        or raw.get("payload_text")
        or payload.get("text")
        or payload.get("text_preview")
        or payload.get("summary")
    )
    text_out = _str_or_none(text_val) if text_val is not None else None

    return ChatTimelineItem(
        item_id=item_id,
        kind=kind,
        role=_timeline_role(raw.get("role")),
        managed_turn_id=_str_or_none(raw.get("managed_turn_id")),
        order_key=str(raw.get("order_key") or item_id),
        section=section,
        section_order=_timeline_section_order(section),
        created_at=created_dt,
        text=text_out,
        artifact_ids=[],
        client_message_id=_str_or_none(
            raw.get("client_message_id") or raw.get("clientMessageId")
        ),
        backend_message_id=_str_or_none(raw.get("backend_message_id")),
        identity=identity,
        provenance=provenance,
    )


def artifact_summary_from_hub(
    artifact: Mapping[str, Any],
) -> Optional[ChatArtifactSummary]:
    path = artifact.get("path") or artifact.get("name")
    artifact_id = _str_or_none(path) if path is not None else None
    if not artifact_id:
        artifact_id = _str_or_none(str(artifact.get("artifact_id")))
    name = (
        _str_or_none(artifact.get("name"))
        or artifact_id
        or _str_or_none(str(artifact.get("kind")))
    )
    if not artifact_id or not name:
        return None

    href = _str_or_none(artifact.get("href") or artifact.get("url"))
    kind_txt = _str_or_none(artifact.get("kind")) or "attachment"
    updated_raw = artifact.get("updated_at")
    updated_dt: Optional[datetime] = None
    if isinstance(updated_raw, datetime):
        updated_dt = updated_raw
    elif isinstance(updated_raw, str) and updated_raw.strip():
        updated_dt = parse_iso_optional(updated_raw)

    return ChatArtifactSummary(
        artifact_id=artifact_id,
        name=name,
        kind=kind_txt,
        href=href,
        updated_at=updated_dt,
    )
