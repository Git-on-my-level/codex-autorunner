from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..domain.refs import SurfaceRef
from ..text_utils import _normalize_optional_text, _parse_iso_timestamp
from .chat_surface_events import ChatSurfaceEvent, SQLiteChatSurfaceEventJournal
from .sqlite import open_orchestration_sqlite

CHAT_SURFACE_READ_CONTRACT_VERSION = "chat_surface_read.v1"
PMA_CHAT_EVENTS_CONTRACT_VERSION = "pma_chat_events.v1"
DEFAULT_CHAT_SURFACE_SNAPSHOT_LIMIT = 500
MAX_CHAT_SURFACE_SNAPSHOT_LIMIT = 1000
DEFAULT_CHAT_SURFACE_EVENT_LIMIT = 100
MAX_CHAT_SURFACE_EVENT_LIMIT = 1000
DEFAULT_CHAT_INDEX_LIMIT = 50
MAX_CHAT_INDEX_LIMIT = 200
DEFAULT_CHAT_TIMELINE_LIMIT = 50
MAX_CHAT_TIMELINE_LIMIT = 200
MAX_CHAT_TIMELINE_PAGE_SOURCE_LIMIT = 1000

_TERMINAL_SUCCESS_STATUSES = {"completed", "succeeded", "success", "delivered"}
_TERMINAL_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled", "timeout"}
_RUNNING_STATUSES = {"running", "in_progress", "started", "claimed", "delivering"}
_QUEUED_STATUSES = {"queued", "pending"}
_DELIVERY_RETRY_STATUSES = {"retry_scheduled"}
_DYNAMIC_LIFECYCLES = frozenset({"idle", "queued", "running", "failed"})

logger = logging.getLogger("codex_autorunner.chat_surface_read_model")


@dataclass
class ChatSurfaceProjection:
    surface_kind: str
    surface_key: str
    lifecycle: str = "discovered"
    lifecycle_status: str = "active"
    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    workspace_root: Optional[str] = None
    scope_urn: Optional[str] = None
    managed_thread_id: Optional[str] = None
    external_conversation_ids: dict[tuple[str, str], dict[str, Optional[str]]] = field(
        default_factory=dict
    )
    display: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    archived_at: Optional[str] = None
    latest_event_cursor: Optional[int] = None
    facts: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def surface_urn(self) -> str:
        return SurfaceRef(kind=self.surface_kind, key=self.surface_key).to_urn()

    def merge(
        self,
        *,
        lifecycle: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        workspace_root: Optional[str] = None,
        scope_urn: Optional[str] = None,
        managed_thread_id: Optional[str] = None,
        external_conversation_id: Optional[str] = None,
        external_provider: Optional[str] = None,
        external_kind: Optional[str] = None,
        display_name: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        archived_at: Optional[str] = None,
        latest_event_cursor: Optional[int] = None,
        fact: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        ordered_lifecycle: bool = False,
    ) -> None:
        if ordered_lifecycle:
            self.lifecycle = _choose_ordered_lifecycle(self.lifecycle, lifecycle)
        else:
            self.lifecycle = _choose_lifecycle(self.lifecycle, lifecycle)
        if lifecycle_status is not None:
            self.lifecycle_status = lifecycle_status
        self.repo_id = _prefer(self.repo_id, repo_id)
        self.resource_kind = _prefer(self.resource_kind, resource_kind)
        self.resource_id = _prefer(self.resource_id, resource_id)
        self.workspace_root = _prefer(self.workspace_root, workspace_root)
        self.scope_urn = _prefer(self.scope_urn, scope_urn)
        self.managed_thread_id = _prefer(self.managed_thread_id, managed_thread_id)
        if external_conversation_id is not None:
            provider = external_provider or self.surface_kind
            key = (provider, external_conversation_id)
            self.external_conversation_ids[key] = {
                "provider": provider,
                "conversation_id": external_conversation_id,
                "conversation_kind": external_kind,
            }
        _merge_display(
            self.display,
            display_name=display_name,
            title=title,
            description=description,
        )
        self.created_at = _min_iso(self.created_at, created_at)
        self.updated_at = _max_iso(self.updated_at, updated_at)
        self.archived_at = _prefer(self.archived_at, archived_at)
        if latest_event_cursor is not None:
            self.latest_event_cursor = max(
                int(self.latest_event_cursor or 0), int(latest_event_cursor)
            )
        if fact is not None:
            self.facts.add(fact)
        if metadata:
            self.metadata.update(dict(metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface_kind": self.surface_kind,
            "surface_key": self.surface_key,
            "surface_urn": self.surface_urn,
            "lifecycle": self.lifecycle,
            "lifecycle_status": self.lifecycle_status,
            "resource_owner": {
                "repo_id": self.repo_id,
                "resource_kind": self.resource_kind,
                "resource_id": self.resource_id,
                "workspace_root": self.workspace_root,
                "scope_urn": self.scope_urn,
            },
            "managed_thread_id": self.managed_thread_id,
            "external_conversation_ids": sorted(
                self.external_conversation_ids.values(),
                key=lambda item: (
                    str(item.get("provider") or ""),
                    str(item.get("conversation_id") or ""),
                    str(item.get("conversation_kind") or ""),
                ),
            ),
            "display": {
                "display_name": self.display.get("display_name"),
                "title": self.display.get("title"),
                "description": self.display.get("description"),
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "latest_event_cursor": self.latest_event_cursor,
            "facts": sorted(self.facts),
            "metadata": dict(sorted(self.metadata.items())),
        }


class ChatSurfaceReadService:
    """Build protocol-neutral chat surface snapshots from orchestration facts."""

    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = Path(hub_root)
        self._durable = durable
        self._journal = SQLiteChatSurfaceEventJournal(
            self._hub_root, durable=self._durable
        )

    def snapshot(
        self, *, limit: int = DEFAULT_CHAT_SURFACE_SNAPSHOT_LIMIT
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        row_limit = _bounded_limit(limit, MAX_CHAT_SURFACE_SNAPSHOT_LIMIT)
        projections: dict[tuple[str, str], ChatSurfaceProjection] = {}
        self._project_channel_directory(projections)
        self._project_orchestration_tables(projections)
        self._project_events(projections)
        surfaces = sorted(
            (projection.to_dict() for projection in projections.values()),
            key=lambda item: (item["surface_kind"], item["surface_key"]),
        )[:row_limit]
        cursor = self._journal.latest_cursor()
        payload = {
            "contract_version": CHAT_SURFACE_READ_CONTRACT_VERSION,
            "cursor": cursor,
            "surfaces": surfaces,
            "limits": {
                "requested": int(limit),
                "returned": len(surfaces),
                "max": MAX_CHAT_SURFACE_SNAPSHOT_LIMIT,
            },
        }
        _log_read_model_metric(
            "projection_rebuild_time",
            started_at,
            returned=len(surfaces),
            limit=row_limit,
            cursor=cursor,
        )
        return payload

    def pma_compat_snapshot(
        self, *, limit: int = DEFAULT_CHAT_SURFACE_SNAPSHOT_LIMIT
    ) -> dict[str, Any]:
        """Return the legacy PMA chat snapshot shape from the generic projection."""

        snapshot = self.snapshot(limit=limit)
        threads = [
            _pma_thread_from_surface(surface)
            for surface in snapshot["surfaces"]
            if surface.get("surface_kind") == "pma"
            and surface.get("managed_thread_id") is not None
            and "managed_thread" in set(surface.get("facts") or [])
        ]
        payload = {
            "contract_version": PMA_CHAT_EVENTS_CONTRACT_VERSION,
            "cursor": int(snapshot["cursor"] or 0),
            "threads": sorted(
                threads,
                key=lambda item: (
                    str(item.get("updated_at") or ""),
                    str(item.get("created_at") or ""),
                    str(item.get("managed_thread_id") or ""),
                ),
                reverse=True,
            ),
        }
        revision_basis = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload["revision"] = hashlib.sha256(revision_basis.encode("utf-8")).hexdigest()
        return payload

    def chat_index_snapshot(
        self,
        *,
        view: str = "all",
        query: Optional[str] = None,
        surface_kind: Optional[str] = None,
        group_by: Optional[str] = None,
        parent_group_id: Optional[str] = None,
        offset: int = 0,
        limit: int = DEFAULT_CHAT_INDEX_LIMIT,
    ) -> dict[str, Any]:
        """Return the screen-shaped `/chats` index read model.

        This read model is intentionally derived from canonical orchestration
        tables plus the chat-surface journal. It gives the frontend one bounded
        window with server-owned filtering, search, grouping, and cursor repair.
        """

        started_at = time.perf_counter()
        surfaces = self.snapshot(limit=MAX_CHAT_SURFACE_SNAPSHOT_LIMIT)["surfaces"]
        rows = _chat_index_rows_from_surfaces(surfaces)
        rows = _filter_chat_index_rows(
            rows,
            view=view,
            query=query,
            surface_kind=surface_kind,
            parent_group_id=parent_group_id,
        )
        rows = sorted(rows, key=_chat_index_sort_key)
        total_count = len(rows)
        bounded_offset = max(0, int(offset or 0))
        bounded_limit = _bounded_limit(limit, MAX_CHAT_INDEX_LIMIT)

        groups = _ticket_run_groups(rows) if group_by == "ticket_run" else []
        if group_by == "ticket_run" and parent_group_id is None:
            group_rows = _filter_chat_index_groups(groups, view=view, query=query)
            total_count = len(group_rows)
            window = group_rows[bounded_offset : bounded_offset + bounded_limit]
        else:
            window = rows[bounded_offset : bounded_offset + bounded_limit]

        cursor = self.latest_cursor()
        payload = {
            "contract_version": "chat_index_read.v1",
            "cursor": cursor,
            "revision": _stable_revision(
                {
                    "cursor": cursor,
                    "view": view,
                    "query": query,
                    "surface_kind": surface_kind,
                    "group_by": group_by,
                    "parent_group_id": parent_group_id,
                    "offset": bounded_offset,
                    "limit": bounded_limit,
                    "rows": window,
                    "total_count": total_count,
                }
            ),
            "window": {
                "offset": bounded_offset,
                "limit": bounded_limit,
                "returned": len(window),
                "total_count": total_count,
                "has_more": bounded_offset + len(window) < total_count,
            },
            "query": {
                "view": view,
                "search": query,
                "surface_kind": surface_kind,
                "group_by": group_by,
                "parent_group_id": parent_group_id,
            },
            "rows": window,
            "groups": groups if group_by == "ticket_run" else [],
        }
        _log_read_model_metric(
            "snapshot_query_latency",
            started_at,
            snapshot="chat_index",
            returned=len(window),
            total_count=total_count,
            cursor=cursor,
        )
        return payload

    def chat_detail_snapshot(
        self,
        managed_thread_id: str,
        *,
        timeline_limit: int = DEFAULT_CHAT_TIMELINE_LIMIT,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        normalized_thread_id = _normalize_text(managed_thread_id)
        if normalized_thread_id is None:
            raise ValueError("managed_thread_id is required")

        from ..managed_thread_store import ManagedThreadStore
        from .managed_thread_timeline import build_managed_thread_timeline

        thread_store = ManagedThreadStore.connect_readonly(
            self._hub_root,
            durable=self._durable,
        )
        thread = thread_store.get_thread(normalized_thread_id)
        if thread is None:
            raise KeyError(normalized_thread_id)
        surface_rows = [
            row
            for row in _chat_index_rows_from_surfaces(
                self.snapshot(limit=MAX_CHAT_SURFACE_SNAPSHOT_LIMIT)["surfaces"]
            )
            if row.get("managed_thread_id") == normalized_thread_id
        ]
        timeline = build_managed_thread_timeline(
            self._hub_root,
            thread_store=thread_store,
            managed_thread_id=normalized_thread_id,
            limit=min(MAX_CHAT_TIMELINE_LIMIT, max(1, int(timeline_limit or 1))),
        )
        items = list(timeline.get("items") or [])
        visible = items[
            -min(MAX_CHAT_TIMELINE_LIMIT, max(1, int(timeline_limit or 1))) :
        ]
        queued_items = thread_store.list_pending_turn_queue_items(
            normalized_thread_id,
            limit=MAX_CHAT_TIMELINE_LIMIT,
        )
        running_turn = thread_store.get_running_turn(normalized_thread_id)
        cursor = self.latest_cursor()
        payload = {
            "contract_version": "chat_detail_read.v1",
            "cursor": cursor,
            "revision": _stable_revision(
                {
                    "cursor": cursor,
                    "thread": normalized_thread_id,
                    "thread_updated_at": thread.get("updated_at"),
                    "timeline_count": len(items),
                    "visible": [item.get("item_id") for item in visible],
                }
            ),
            "thread": _chat_detail_thread_metadata(thread, surface_rows),
            "timeline": {
                "contract_version": timeline.get("contract_version"),
                "items": visible,
                "item_count": len(items),
                "window": {
                    "limit": min(
                        MAX_CHAT_TIMELINE_LIMIT, max(1, int(timeline_limit or 1))
                    ),
                    "returned": len(visible),
                    "has_older": len(visible) < len(items),
                    "oldest_order_key": (
                        visible[0].get("order_key") if visible else None
                    ),
                },
            },
            "active_turn_status": _active_turn_status(running_turn),
            "queue_summary": {
                "depth": thread_store.get_queue_depth(normalized_thread_id),
                "items": [
                    _queue_summary_item(item, position=index)
                    for index, item in enumerate(queued_items, start=1)
                ],
            },
            "artifacts": _timeline_artifacts(visible),
            "stream": {
                "cursor": cursor,
                "patch_url": "/hub/chat/patches",
            },
        }
        _log_read_model_metric(
            "snapshot_query_latency",
            started_at,
            snapshot="chat_detail",
            returned=len(visible),
            total_count=len(items),
            cursor=cursor,
        )
        return payload

    def older_timeline_page(
        self,
        managed_thread_id: str,
        *,
        before_order_key: Optional[str],
        limit: int = DEFAULT_CHAT_TIMELINE_LIMIT,
    ) -> dict[str, Any]:
        normalized_thread_id = _normalize_text(managed_thread_id)
        if normalized_thread_id is None:
            raise ValueError("managed_thread_id is required")

        from ..managed_thread_store import ManagedThreadStore
        from .managed_thread_timeline import build_managed_thread_timeline

        thread_store = ManagedThreadStore.connect_readonly(
            self._hub_root,
            durable=self._durable,
        )
        if thread_store.get_thread(normalized_thread_id) is None:
            raise KeyError(normalized_thread_id)

        timeline = build_managed_thread_timeline(
            self._hub_root,
            thread_store=thread_store,
            managed_thread_id=normalized_thread_id,
            limit=MAX_CHAT_TIMELINE_PAGE_SOURCE_LIMIT,
        )
        all_items = list(timeline.get("items") or [])
        if before_order_key is not None:
            all_items = [
                item
                for item in all_items
                if str(item.get("order_key") or "") < str(before_order_key)
            ]
        bounded_limit = _bounded_limit(limit, MAX_CHAT_TIMELINE_LIMIT)
        page = all_items[-bounded_limit:]
        return {
            "contract_version": "chat_timeline_page.v1",
            "managed_thread_id": normalized_thread_id,
            "cursor": self.latest_cursor(),
            "items": page,
            "window": {
                "before_order_key": before_order_key,
                "limit": bounded_limit,
                "returned": len(page),
                "has_older": len(page) < len(all_items),
                "oldest_order_key": page[0].get("order_key") if page else None,
            },
        }

    def chat_patches_since(
        self,
        cursor: Optional[int],
        *,
        limit: int = DEFAULT_CHAT_SURFACE_EVENT_LIMIT,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        events = self._journal.read_events_since(
            cursor or 0,
            limit=_bounded_limit(limit, MAX_CHAT_SURFACE_EVENT_LIMIT),
        )
        patches = [_chat_patch_from_event(event) for event in events]
        next_cursor = patches[-1]["cursor"] if patches else int(cursor or 0)
        payload = {
            "contract_version": "chat_patch_stream.v1",
            "cursor": next_cursor,
            "patches": patches,
            "limits": {
                "requested": int(limit),
                "returned": len(patches),
                "max": MAX_CHAT_SURFACE_EVENT_LIMIT,
            },
        }
        _log_read_model_metric(
            "stream_read_latency",
            started_at,
            returned=len(patches),
            cursor=next_cursor,
            cursor_gap_count=(
                1
                if len(patches) >= _bounded_limit(limit, MAX_CHAT_SURFACE_EVENT_LIMIT)
                else 0
            ),
        )
        return payload

    def events_since(
        self,
        cursor: Optional[int],
        *,
        limit: int = DEFAULT_CHAT_SURFACE_EVENT_LIMIT,
    ) -> list[dict[str, Any]]:
        started_at = time.perf_counter()
        events = self._journal.read_events_since(
            cursor or 0,
            limit=_bounded_limit(limit, MAX_CHAT_SURFACE_EVENT_LIMIT),
        )
        payload = [serialize_chat_surface_event(event) for event in events]
        _log_read_model_metric(
            "stream_read_latency",
            started_at,
            returned=len(payload),
            cursor=payload[-1]["cursor"] if payload else int(cursor or 0),
            cursor_gap_count=(
                1
                if len(payload) >= _bounded_limit(limit, MAX_CHAT_SURFACE_EVENT_LIMIT)
                else 0
            ),
        )
        return payload

    def latest_cursor(self) -> int:
        return self._journal.latest_cursor()

    def _project_channel_directory(
        self, projections: dict[tuple[str, str], ChatSurfaceProjection]
    ) -> None:
        for entry in _read_channel_directory_entries(self._hub_root)[
            :MAX_CHAT_SURFACE_SNAPSHOT_LIMIT
        ]:
            surface_kind = _normalize_kind(entry.get("platform"))
            chat_id = _normalize_text(entry.get("chat_id"))
            if surface_kind is None or chat_id is None:
                continue
            thread_id = _normalize_text(entry.get("thread_id"))
            surface_key = f"{chat_id}:{thread_id}" if thread_id else chat_id
            projection = _projection(projections, surface_kind, surface_key)
            projection.merge(
                lifecycle="discovered",
                external_conversation_id=(
                    f"{surface_kind}:{surface_key}" if thread_id else chat_id
                ),
                external_provider=surface_kind,
                external_kind="channel",
                display_name=_normalize_text(entry.get("display")),
                updated_at=_normalize_text(entry.get("seen_at")),
                fact="channel_directory",
                metadata={"channel_directory_meta": entry.get("meta") or {}},
            )

    def _project_orchestration_tables(
        self, projections: dict[tuple[str, str], ChatSurfaceProjection]
    ) -> None:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            thread_rows = conn.execute(
                """
                SELECT *
                  FROM orch_thread_targets
                 ORDER BY updated_at DESC, created_at DESC, thread_target_id ASC
                 LIMIT ?
                """,
                (MAX_CHAT_SURFACE_SNAPSHOT_LIMIT,),
            ).fetchall()
            execution_rows = conn.execute(
                """
                SELECT thread_target_id,
                       execution_id,
                       status,
                       created_at,
                       started_at,
                       finished_at,
                       error_text
                  FROM orch_thread_executions
                 ORDER BY created_at ASC, execution_id ASC
                """
            ).fetchall()
            delivery_rows = (
                conn.execute(
                    """
                    SELECT managed_thread_id,
                           surface_kind,
                           surface_key,
                           state,
                           final_status,
                           delivered_at,
                           updated_at,
                           created_at
                      FROM orch_managed_thread_deliveries
                     ORDER BY updated_at ASC, created_at ASC, delivery_id ASC
                    """
                ).fetchall()
                if _table_exists(conn, "orch_managed_thread_deliveries")
                else []
            )
            binding_rows = conn.execute(
                """
                SELECT *
                  FROM orch_bindings
                 WHERE disabled_at IS NULL
                 ORDER BY surface_kind ASC, surface_key ASC, updated_at ASC, binding_id ASC
                 LIMIT ?
                """,
                (MAX_CHAT_SURFACE_SNAPSHOT_LIMIT,),
            ).fetchall()
            notification_rows = (
                conn.execute(
                    """
                    SELECT *
                      FROM orch_notification_conversations
                     ORDER BY updated_at ASC, created_at ASC, notification_id ASC
                     LIMIT ?
                    """,
                    (MAX_CHAT_SURFACE_SNAPSHOT_LIMIT,),
                ).fetchall()
                if _table_exists(conn, "orch_notification_conversations")
                else []
            )

        execution_by_thread = _latest_execution_by_thread(execution_rows)
        queue_depth_by_thread = _queue_depth_by_thread(execution_rows)
        delivery_by_surface = _latest_delivery_by_surface(delivery_rows)
        delivery_by_thread = _latest_delivery_by_thread(delivery_rows)
        thread_owner: dict[str, Mapping[str, Any]] = {}
        binding_summary_by_thread = _binding_summary_by_thread(binding_rows)

        for row in thread_rows:
            thread_id = str(row["thread_target_id"])
            thread_owner[thread_id] = row
            execution = execution_by_thread.get(thread_id)
            delivery = delivery_by_thread.get(thread_id)
            binding_summary = binding_summary_by_thread.get(thread_id, {})
            lifecycle = _thread_lifecycle(
                row, execution, queue_depth_by_thread.get(thread_id, 0)
            )
            if delivery is not None:
                lifecycle = _choose_lifecycle(
                    lifecycle, _status_to_lifecycle(delivery["state"])
                )
            projection = _projection(projections, "pma", thread_id)
            lifecycle_status = _normalize_text(row["lifecycle_status"]) or "active"
            metadata = _json_object(_row_get(row, "metadata_json"))
            projection.merge(
                lifecycle=lifecycle,
                lifecycle_status=lifecycle_status,
                repo_id=_normalize_text(row["repo_id"]),
                resource_kind=_normalize_text(_row_get(row, "resource_kind")),
                resource_id=_normalize_text(_row_get(row, "resource_id")),
                workspace_root=_normalize_text(row["workspace_root"]),
                managed_thread_id=thread_id,
                display_name=_normalize_text(row["display_name"]) or thread_id,
                created_at=_normalize_text(row["created_at"]),
                updated_at=_max_iso(
                    _normalize_text(row["updated_at"]),
                    _normalize_text(execution["created_at"]) if execution else None,
                ),
                archived_at=(
                    _normalize_text(row["updated_at"])
                    if lifecycle_status == "archived"
                    else None
                ),
                fact="managed_thread",
                metadata={
                    "agent_id": _normalize_text(row["agent_id"]),
                    "agent_profile": _normalize_text(metadata.get("agent_profile")),
                    "backend_thread_id": _normalize_text(
                        _row_get(row, "backend_thread_id")
                    ),
                    "model": _normalize_text(metadata.get("model")),
                    "runtime_status": _normalize_text(row["runtime_status"]),
                    "target_runtime_status": _normalize_text(row["runtime_status"]),
                    "queue_depth": queue_depth_by_thread.get(thread_id, 0),
                    "active_turn_id": (
                        _normalize_text(execution["execution_id"])
                        if execution
                        else None
                    ),
                    "latest_execution_status": (
                        _normalize_text(execution["status"]) if execution else None
                    ),
                    "status_reason": _normalize_text(_row_get(row, "status_reason")),
                    "status_changed_at": _normalize_text(
                        _row_get(row, "status_changed_at")
                    ),
                    "status_terminal": bool(_row_get(row, "status_terminal")),
                    "status_turn_id": _normalize_text(_row_get(row, "status_turn_id")),
                    "last_turn_id": _normalize_text(_row_get(row, "last_execution_id")),
                    "last_message_preview": _normalize_text(
                        _row_get(row, "last_message_preview")
                    ),
                    "compact_seed": _normalize_text(_row_get(row, "compact_seed")),
                    **binding_summary,
                },
            )

        for row in binding_rows:
            surface_kind = _normalize_kind(row["surface_kind"])
            surface_key = _normalize_text(row["surface_key"])
            binding_thread_id = _normalize_text(row["target_id"])
            if surface_kind is None or surface_key is None:
                continue
            owner = thread_owner.get(binding_thread_id or "")
            execution = execution_by_thread.get(binding_thread_id or "")
            delivery = delivery_by_surface.get((surface_kind, surface_key))
            lifecycle = _thread_lifecycle(
                owner,
                execution,
                queue_depth_by_thread.get(binding_thread_id or "", 0),
            )
            if delivery is not None:
                lifecycle = _choose_lifecycle(
                    lifecycle, _status_to_lifecycle(delivery["state"])
                )
            projection = _projection(projections, surface_kind, surface_key)
            projection.merge(
                lifecycle=lifecycle,
                lifecycle_status=(
                    _normalize_text(_row_get(owner, "lifecycle_status"))
                    if owner is not None
                    else "active"
                )
                or "active",
                repo_id=_normalize_text(row["repo_id"])
                or _normalize_text(_row_get(owner, "repo_id")),
                resource_kind=_normalize_text(_row_get(row, "resource_kind"))
                or _normalize_text(_row_get(owner, "resource_kind")),
                resource_id=_normalize_text(_row_get(row, "resource_id"))
                or _normalize_text(_row_get(owner, "resource_id")),
                workspace_root=_normalize_text(_row_get(owner, "workspace_root")),
                managed_thread_id=binding_thread_id,
                display_name=_binding_display(row),
                created_at=_normalize_text(row["created_at"]),
                updated_at=_normalize_text(row["updated_at"]),
                fact="binding",
                metadata={
                    "mode": _normalize_text(row["mode"]),
                    "agent_id": _normalize_text(row["agent_id"]),
                    "queue_depth": queue_depth_by_thread.get(
                        binding_thread_id or "", 0
                    ),
                },
            )

        for row in notification_rows:
            notification_id = _normalize_text(row["notification_id"])
            if notification_id is None:
                continue
            continuation_thread_id = _normalize_text(
                row["continuation_thread_target_id"]
            )
            managed_thread_id = continuation_thread_id or _normalize_text(
                row["managed_thread_id"]
            )
            projection = _projection(
                projections, "notification", f"notification:{notification_id}"
            )
            projection.merge(
                lifecycle="bound" if continuation_thread_id else "discovered",
                lifecycle_status="active",
                repo_id=_normalize_text(row["repo_id"]),
                workspace_root=_normalize_text(row["workspace_root"]),
                managed_thread_id=managed_thread_id,
                external_conversation_id=f"{row['surface_kind']}:{row['surface_key']}",
                external_provider=_normalize_kind(row["surface_kind"])
                or "notification",
                external_kind="reply_context",
                display_name=f"Notification {notification_id}",
                created_at=_normalize_text(row["created_at"]),
                updated_at=_normalize_text(row["updated_at"]),
                fact="notification_reply_context",
                metadata={
                    "notification_id": notification_id,
                    "correlation_id": _normalize_text(row["correlation_id"]),
                    "delivery_mode": _normalize_text(row["delivery_mode"]),
                    "delivered": _normalize_text(row["delivered_message_id"])
                    is not None,
                },
            )

    def _project_events(
        self, projections: dict[tuple[str, str], ChatSurfaceProjection]
    ) -> None:
        for event in self._journal.read_history(limit=MAX_CHAT_SURFACE_EVENT_LIMIT):
            projection = _projection(projections, event.surface_kind, event.surface_key)
            payload_display = event.payload.get("display")
            display = payload_display if isinstance(payload_display, Mapping) else {}
            projection.merge(
                lifecycle=_event_lifecycle(event),
                lifecycle_status=event.lifecycle_status,
                repo_id=event.repo_id,
                resource_kind=event.resource_kind,
                resource_id=event.resource_id,
                workspace_root=event.workspace_root,
                managed_thread_id=event.managed_thread_id,
                external_conversation_id=event.external_conversation_id,
                external_provider=event.surface_kind,
                display_name=_normalize_text(display.get("display_name")),
                title=_normalize_text(display.get("title")),
                created_at=event.occurred_at,
                updated_at=event.occurred_at,
                latest_event_cursor=event.cursor,
                fact="event_journal",
                metadata={
                    "latest_event_type": event.event_type,
                    "latest_event_status": event.status,
                },
                ordered_lifecycle=_event_is_ordered_after_projection(projection, event),
            )


def serialize_chat_surface_event(event: ChatSurfaceEvent) -> dict[str, Any]:
    return {
        "contract_version": CHAT_SURFACE_READ_CONTRACT_VERSION,
        "cursor": event.cursor,
        "event_type": event.event_type,
        "surface": {
            "surface_kind": event.surface_kind,
            "surface_key": event.surface_key,
            "surface_urn": SurfaceRef(
                kind=event.surface_kind, key=event.surface_key
            ).to_urn(),
        },
        "managed_thread_id": event.managed_thread_id,
        "external_conversation_id": event.external_conversation_id,
        "resource_owner": {
            "repo_id": event.repo_id,
            "resource_kind": event.resource_kind,
            "resource_id": event.resource_id,
            "workspace_root": event.workspace_root,
        },
        "lifecycle": _event_lifecycle(event),
        "lifecycle_status": event.lifecycle_status,
        "status": event.status,
        "occurred_at": event.occurred_at,
        "created_at": event.created_at,
        "source": {
            "kind": event.source_kind,
        },
        "details": _public_event_details(event.payload),
    }


def parse_chat_surface_cursor(raw: Any) -> int:
    normalized = _normalize_optional_text(raw)
    if normalized is None:
        return 0
    try:
        value = int(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor must be a non-negative integer") from exc
    if value < 0:
        raise ValueError("cursor must be a non-negative integer")
    return value


def _stable_revision(payload: Mapping[str, Any]) -> str:
    basis = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _chat_index_rows_from_surfaces(
    surfaces: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_thread: dict[str, dict[str, Any]] = {}
    external_rows: list[dict[str, Any]] = []
    for surface in surfaces:
        surface_kind = _normalize_text(surface.get("surface_kind")) or ""
        surface_key = _normalize_text(surface.get("surface_key")) or ""
        managed_thread_id = _normalize_text(surface.get("managed_thread_id"))
        owner = surface.get("resource_owner")
        resource_owner = dict(owner) if isinstance(owner, Mapping) else {}
        display = surface.get("display")
        display_map = dict(display) if isinstance(display, Mapping) else {}
        metadata = surface.get("metadata")
        metadata_map = dict(metadata) if isinstance(metadata, Mapping) else {}
        base_surface = {
            "surface_kind": surface_kind,
            "surface_key": surface_key,
            "surface_urn": surface.get("surface_urn"),
            "lifecycle": surface.get("lifecycle"),
            "display_name": display_map.get("display_name"),
        }
        if managed_thread_id is None:
            external_rows.append(
                {
                    "row_type": "chat",
                    "row_id": f"surface:{surface_kind}:{surface_key}",
                    "managed_thread_id": None,
                    "surface": base_surface,
                    "surfaces": [base_surface],
                    "title": _display_title(display_map, surface_key),
                    "repo_id": resource_owner.get("repo_id"),
                    "resource_kind": resource_owner.get("resource_kind"),
                    "resource_id": resource_owner.get("resource_id"),
                    "workspace_root": resource_owner.get("workspace_root"),
                    "lifecycle": surface.get("lifecycle"),
                    "lifecycle_status": surface.get("lifecycle_status"),
                    "runtime_status": metadata_map.get("runtime_status"),
                    "latest_event_cursor": surface.get("latest_event_cursor"),
                    "updated_at": surface.get("updated_at"),
                    "created_at": surface.get("created_at"),
                    "last_message_preview": metadata_map.get("last_message_preview"),
                    "unread": bool(metadata_map.get("unread")),
                    "active_turn_id": metadata_map.get("active_turn_id"),
                    "queue_depth": int(metadata_map.get("queue_depth") or 0),
                    "search_text": "",
                }
            )
            continue
        row = by_thread.get(managed_thread_id)
        if row is None:
            row = {
                "row_type": "chat",
                "row_id": f"thread:{managed_thread_id}",
                "managed_thread_id": managed_thread_id,
                "surfaces": [],
                "title": managed_thread_id,
                "repo_id": resource_owner.get("repo_id"),
                "resource_kind": resource_owner.get("resource_kind"),
                "resource_id": resource_owner.get("resource_id"),
                "workspace_root": resource_owner.get("workspace_root"),
                "lifecycle": surface.get("lifecycle"),
                "lifecycle_status": surface.get("lifecycle_status"),
                "runtime_status": metadata_map.get("runtime_status"),
                "target_runtime_status": metadata_map.get("target_runtime_status"),
                "latest_event_cursor": surface.get("latest_event_cursor"),
                "updated_at": surface.get("updated_at"),
                "created_at": surface.get("created_at"),
                "last_message_preview": metadata_map.get("last_message_preview"),
                "agent": metadata_map.get("agent_id"),
                "agent_profile": metadata_map.get("agent_profile"),
                "model": metadata_map.get("model"),
                "active_turn_id": metadata_map.get("active_turn_id"),
                "queue_depth": int(metadata_map.get("queue_depth") or 0),
                "unread": bool(metadata_map.get("unread")),
                "cleanup_protected": bool(metadata_map.get("cleanup_protected")),
            }
            by_thread[managed_thread_id] = row
        row["surfaces"].append(base_surface)
        row["lifecycle"] = _choose_lifecycle(
            str(row["lifecycle"] or "bound"), surface.get("lifecycle")
        )
        row["updated_at"] = _max_iso(row.get("updated_at"), surface.get("updated_at"))
        row["latest_event_cursor"] = (
            max(
                int(row.get("latest_event_cursor") or 0),
                int(surface.get("latest_event_cursor") or 0),
            )
            or None
        )
        if surface_kind == "pma" or row.get("surface") is None:
            row["surface"] = base_surface
            row["title"] = _display_title(display_map, managed_thread_id)
            for key in (
                "repo_id",
                "resource_kind",
                "resource_id",
                "workspace_root",
                "lifecycle_status",
            ):
                row[key] = resource_owner.get(key) or row.get(key)
            for key in (
                "runtime_status",
                "target_runtime_status",
                "last_message_preview",
                "agent_id",
                "agent_profile",
                "model",
                "active_turn_id",
            ):
                if metadata_map.get(key) is not None:
                    row["agent" if key == "agent_id" else key] = metadata_map.get(key)
            row["queue_depth"] = max(
                int(row.get("queue_depth") or 0),
                int(metadata_map.get("queue_depth") or 0),
            )
    rows = list(by_thread.values()) + external_rows
    for row in rows:
        row["group_id"] = _chat_ticket_group_id(row)
        row["surface_kinds"] = sorted(
            {
                str(surface.get("surface_kind"))
                for surface in row.get("surfaces", [])
                if surface.get("surface_kind") is not None
            }
        )
        row["search_text"] = _chat_row_search_text(row)
    return rows


def _display_title(display: Mapping[str, Any], fallback: str) -> str:
    return (
        _normalize_text(display.get("title"))
        or _normalize_text(display.get("display_name"))
        or fallback
    )


def _log_read_model_metric(
    metric: str,
    started_at: float,
    **fields: Any,
) -> None:
    logger.debug(
        "chat_surface_read_model_metric",
        extra={
            "event": "chat_surface_read_model_metric",
            "metric": metric,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
            **fields,
        },
    )


def _chat_row_search_text(row: Mapping[str, Any]) -> str:
    values = [
        row.get("managed_thread_id"),
        row.get("title"),
        row.get("repo_id"),
        row.get("resource_kind"),
        row.get("resource_id"),
        row.get("agent"),
        row.get("agent_profile"),
        row.get("model"),
        row.get("last_message_preview"),
        " ".join(row.get("surface_kinds") or []),
    ]
    return " ".join(str(value).lower() for value in values if value)


def _filter_chat_index_rows(
    rows: list[dict[str, Any]],
    *,
    view: str,
    query: Optional[str],
    surface_kind: Optional[str],
    parent_group_id: Optional[str],
) -> list[dict[str, Any]]:
    normalized_view = (view or "all").strip().lower()
    normalized_query = _normalize_text(query)
    normalized_query = normalized_query.lower() if normalized_query else None
    normalized_surface = _normalize_kind(surface_kind)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if parent_group_id is not None and row.get("group_id") != parent_group_id:
            continue
        if normalized_surface is not None and normalized_surface not in set(
            row.get("surface_kinds") or []
        ):
            continue
        lifecycle = str(row.get("lifecycle") or "")
        if normalized_view == "waiting" and int(row.get("queue_depth") or 0) <= 0:
            continue
        if normalized_view == "active" and lifecycle != "running":
            continue
        if normalized_view == "unread" and not row.get("unread"):
            continue
        if normalized_view == "archived" and row.get("lifecycle_status") != "archived":
            continue
        if normalized_view == "external" and set(row.get("surface_kinds") or []) <= {
            "pma"
        }:
            continue
        if normalized_view == "ticket_run" and row.get("group_id") is None:
            continue
        if normalized_view not in {
            "all",
            "waiting",
            "active",
            "unread",
            "archived",
            "external",
            "ticket_run",
        }:
            continue
        if normalized_view != "archived" and row.get("lifecycle_status") == "archived":
            continue
        if normalized_query is not None and normalized_query not in row["search_text"]:
            continue
        filtered.append(row)
    return filtered


def _chat_index_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
    lifecycle = str(row.get("lifecycle") or "")
    priority = 0
    if int(row.get("queue_depth") or 0) > 0:
        priority = 3
    elif lifecycle == "running":
        priority = 2
    elif row.get("unread"):
        priority = 1
    raw = str(row.get("updated_at") or row.get("created_at") or "")
    parsed = _parse_iso_timestamp(raw)
    updated_key = float("inf") if parsed is None else -parsed.timestamp()
    return (
        -priority,
        updated_key,
        str(row.get("row_id") or ""),
    )


def _chat_ticket_group_id(row: Mapping[str, Any]) -> Optional[str]:
    kind = _normalize_kind(row.get("resource_kind"))
    identifier = _normalize_text(row.get("resource_id"))
    if kind in {"ticket", "ticket_run", "run"} and identifier is not None:
        return f"{kind}:{identifier}"
    thread_id = _normalize_text(row.get("managed_thread_id"))
    if thread_id and thread_id.startswith("ticket-run:"):
        return ":".join(thread_id.split(":", 2)[:2])
    return None


def _ticket_run_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        group_id = row.get("group_id")
        if isinstance(group_id, str) and group_id:
            grouped.setdefault(group_id, []).append(row)
    groups: list[dict[str, Any]] = []
    for group_id, children in grouped.items():
        latest = max(str(child.get("updated_at") or "") for child in children)
        groups.append(
            {
                "row_type": "group",
                "row_id": f"group:{group_id}",
                "group_id": group_id,
                "title": group_id,
                "child_count": len(children),
                "waiting_count": sum(
                    1 for child in children if int(child.get("queue_depth") or 0) > 0
                ),
                "running_count": sum(
                    1 for child in children if child.get("lifecycle") == "running"
                ),
                "unread_count": sum(1 for child in children if child.get("unread")),
                "updated_at": latest,
                "sample_child_ids": [
                    child.get("row_id") for child in children[:3] if child.get("row_id")
                ],
                "search_text": " ".join(child["search_text"] for child in children),
            }
        )
    return sorted(
        groups, key=lambda group: str(group.get("updated_at") or ""), reverse=True
    )


def _filter_chat_index_groups(
    groups: list[dict[str, Any]], *, view: str, query: Optional[str]
) -> list[dict[str, Any]]:
    normalized_query = _normalize_text(query)
    normalized_query = normalized_query.lower() if normalized_query else None
    normalized_view = (view or "all").strip().lower()
    result: list[dict[str, Any]] = []
    for group in groups:
        if normalized_view == "waiting" and int(group.get("waiting_count") or 0) <= 0:
            continue
        if normalized_view == "active" and int(group.get("running_count") or 0) <= 0:
            continue
        if normalized_view == "unread" and int(group.get("unread_count") or 0) <= 0:
            continue
        if normalized_query and normalized_query not in str(
            group.get("search_text") or ""
        ):
            continue
        result.append(group)
    return result


def _chat_detail_thread_metadata(
    thread: Mapping[str, Any], surface_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    metadata = thread.get("metadata")
    metadata_map = dict(metadata) if isinstance(metadata, Mapping) else {}
    return {
        "managed_thread_id": thread.get("managed_thread_id")
        or thread.get("thread_target_id"),
        "title": thread.get("display_name"),
        "agent": thread.get("agent") or thread.get("agent_id"),
        "agent_profile": metadata_map.get("agent_profile"),
        "model": metadata_map.get("model"),
        "repo_id": thread.get("repo_id"),
        "resource_kind": thread.get("resource_kind"),
        "resource_id": thread.get("resource_id"),
        "workspace_root": thread.get("workspace_root"),
        "lifecycle_status": thread.get("lifecycle_status") or thread.get("status"),
        "runtime_status": thread.get("normalized_status")
        or thread.get("runtime_status"),
        "backend_thread_id": thread.get("backend_thread_id"),
        "last_turn_id": thread.get("last_execution_id"),
        "last_message_preview": thread.get("last_message_preview"),
        "compact_seed": thread.get("compact_seed"),
        "surfaces": [
            surface
            for row in surface_rows
            for surface in row.get("surfaces", [])
            if isinstance(surface, dict)
        ],
    }


def _active_turn_status(turn: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    if turn is None:
        return None
    return {
        "managed_turn_id": turn.get("managed_turn_id"),
        "status": turn.get("status"),
        "request_kind": turn.get("request_kind"),
        "started_at": turn.get("started_at"),
        "created_at": turn.get("created_at"),
        "model": turn.get("model"),
        "reasoning": turn.get("reasoning"),
    }


def _queue_summary_item(item: Mapping[str, Any], *, position: int) -> dict[str, Any]:
    return {
        "managed_turn_id": item.get("managed_turn_id"),
        "position": position,
        "state": item.get("state"),
        "request_kind": item.get("request_kind"),
        "prompt_preview": _normalize_text(item.get("prompt")) or "",
        "enqueued_at": item.get("enqueued_at"),
        "visible_at": item.get("visible_at"),
    }


def _timeline_artifacts(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        for attachment in payload.get("attachments") or []:
            if not isinstance(attachment, Mapping):
                continue
            key = str(attachment.get("path") or attachment.get("name") or attachment)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(dict(attachment))
    return artifacts


def _chat_patch_from_event(event: ChatSurfaceEvent) -> dict[str, Any]:
    patch_type_by_event = {
        "surface.bound": "row_update",
        "surface.rebound": "row_update",
        "surface.archived": "archive_restore",
        "lifecycle.status_changed": "lifecycle_change",
        "queue.state_changed": "queue_change",
        "execution.progress": "progress_change",
        "delivery.status_changed": "delivery_lifecycle_change",
        "notification.reply_context_changed": "delivery_lifecycle_change",
        "channel_directory.discovered": "row_update",
    }
    details = _public_event_details(event.payload)
    requested = (
        _normalize_text(details.get("patch_type"))
        if isinstance(details, Mapping)
        else None
    )
    patch_type = requested or patch_type_by_event.get(event.event_type, "row_update")
    if patch_type not in {
        "row_update",
        "group_update",
        "timeline_append",
        "timeline_patch",
        "queue_change",
        "progress_change",
        "artifacts",
        "archive_restore",
        "compaction",
        "delivery_lifecycle_change",
        "lifecycle_change",
    }:
        patch_type = "row_update"
    return {
        "contract_version": "chat_patch.v1",
        "cursor": event.cursor,
        "patch_id": f"chat-patch:{event.cursor}",
        "patch_type": patch_type,
        "event_type": event.event_type,
        "managed_thread_id": event.managed_thread_id,
        "surface": {
            "surface_kind": event.surface_kind,
            "surface_key": event.surface_key,
        },
        "resource_owner": {
            "repo_id": event.repo_id,
            "resource_kind": event.resource_kind,
            "resource_id": event.resource_id,
            "workspace_root": event.workspace_root,
        },
        "status": event.status,
        "lifecycle": _event_lifecycle(event),
        "occurred_at": event.occurred_at,
        "details": details,
    }


def _projection(
    projections: dict[tuple[str, str], ChatSurfaceProjection],
    surface_kind: str,
    surface_key: str,
) -> ChatSurfaceProjection:
    key = (_normalize_kind(surface_kind) or str(surface_kind), str(surface_key).strip())
    existing = projections.get(key)
    if existing is not None:
        return existing
    projection = ChatSurfaceProjection(surface_kind=key[0], surface_key=key[1])
    projections[key] = projection
    return projection


def _bounded_limit(value: int, max_value: int) -> int:
    return min(max_value, max(1, int(value)))


def _normalize_kind(value: Any) -> Optional[str]:
    normalized = _normalize_text(value)
    return normalized.lower() if normalized is not None else None


def _normalize_text(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return _normalize_optional_text(value)


def _prefer(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    return current if current is not None else _normalize_text(candidate)


def _min_iso(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    candidate = _normalize_text(candidate)
    if current is None:
        return candidate
    if candidate is None:
        return current
    return min(current, candidate)


def _max_iso(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    candidate = _normalize_text(candidate)
    if current is None:
        return candidate
    if candidate is None:
        return current
    return max(current, candidate)


def _merge_display(
    display: dict[str, Any],
    *,
    display_name: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    if display.get("display_name") is None and display_name is not None:
        display["display_name"] = display_name
    if display.get("title") is None and title is not None:
        display["title"] = title
    if display.get("description") is None and description is not None:
        display["description"] = description


def _choose_lifecycle(current: str, candidate: Optional[str]) -> str:
    if candidate is None:
        return current
    if current == "archived" or candidate == "archived":
        return "archived"
    # Journal replay can emit both "running" and "idle" for one execution; taking the
    # numeric max sticks on "running" after completion. When merging those two, prefer "idle".
    if {current, candidate} == {"idle", "running"}:
        return "idle"
    order = {
        "discovered": 0,
        "bound": 1,
        "idle": 2,
        "queued": 3,
        "running": 4,
        "failed": 5,
        "archived": 6,
    }
    return candidate if order.get(candidate, 0) >= order.get(current, 0) else current


def _choose_ordered_lifecycle(current: str, candidate: Optional[str]) -> str:
    if candidate is None:
        return current
    if current == "archived" or candidate == "archived":
        return "archived"
    if current in _DYNAMIC_LIFECYCLES and candidate in _DYNAMIC_LIFECYCLES:
        return candidate
    if current == "failed" and candidate == "bound":
        return candidate
    return _choose_lifecycle(current, candidate)


def _event_is_ordered_after_projection(
    projection: ChatSurfaceProjection, event: ChatSurfaceEvent
) -> bool:
    if projection.updated_at is None:
        return True
    if event.occurred_at is None:
        return False
    return event.occurred_at >= projection.updated_at


def _status_to_lifecycle(status: Any) -> Optional[str]:
    normalized = _normalize_kind(status)
    if normalized is None:
        return None
    if normalized in _QUEUED_STATUSES:
        return "queued"
    if normalized in _RUNNING_STATUSES:
        return "running"
    if normalized in _TERMINAL_FAILED_STATUSES:
        return "failed"
    if normalized in _TERMINAL_SUCCESS_STATUSES:
        return "idle"
    if normalized in _DELIVERY_RETRY_STATUSES:
        return "idle"
    if normalized in {"archived"}:
        return "archived"
    if normalized in {"bound", "recorded", "delivered", "continuation_bound"}:
        return "bound"
    if normalized in {"discovered"}:
        return "discovered"
    return None


def _event_lifecycle(event: ChatSurfaceEvent) -> str:
    if event.event_type == "surface.archived" or event.lifecycle_status == "archived":
        return "archived"
    if event.event_type == "channel_directory.discovered":
        return "discovered"
    if event.event_type == "notification.reply_context_changed":
        return "bound" if event.managed_thread_id is not None else "discovered"
    if event.event_type in {"surface.bound", "surface.rebound"}:
        return "bound"
    status_lifecycle = _status_to_lifecycle(event.status)
    if status_lifecycle is not None:
        return status_lifecycle
    if event.managed_thread_id is not None:
        return "bound"
    return "discovered"


def _thread_lifecycle(
    row: Optional[Mapping[str, Any]],
    execution: Optional[Mapping[str, Any]],
    queue_depth: int,
) -> str:
    lifecycle_status = _normalize_kind(_row_get(row, "lifecycle_status"))
    if lifecycle_status == "archived":
        return "archived"
    runtime_status = _status_to_lifecycle(_row_get(row, "runtime_status"))
    if runtime_status in {"running", "failed"}:
        return runtime_status
    if queue_depth > 0:
        return "queued"
    execution_status = _status_to_lifecycle(_row_get(execution, "status"))
    if execution_status is not None:
        return execution_status
    return "bound"


def _latest_execution_by_thread(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        thread_id = _normalize_text(row["thread_target_id"])
        if thread_id is not None:
            result[thread_id] = row
    return result


def _queue_depth_by_thread(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        thread_id = _normalize_text(row["thread_target_id"])
        if thread_id is None:
            continue
        if _normalize_kind(row["status"]) in _QUEUED_STATUSES:
            result[thread_id] = result.get(thread_id, 0) + 1
    return result


def _latest_delivery_by_surface(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        surface_kind = _normalize_kind(row["surface_kind"])
        surface_key = _normalize_text(row["surface_key"])
        if surface_kind is not None and surface_key is not None:
            result[(surface_kind, surface_key)] = row
    return result


def _latest_delivery_by_thread(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        thread_id = _normalize_text(row["managed_thread_id"])
        if thread_id is not None:
            result[thread_id] = row
    return result


def _binding_summary_by_thread(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for row in rows:
        thread_id = _normalize_text(row["target_id"])
        if thread_id is None:
            continue
        summary = summaries.setdefault(
            thread_id,
            {
                "chat_bound": False,
                "binding_kind": None,
                "binding_id": None,
                "chat_display_name": None,
                "binding_count": 0,
                "binding_kinds": [],
                "binding_ids": [],
                "chat_display_names": [],
                "cleanup_protected": False,
            },
        )
        metadata = _json_object(row["metadata_json"])
        surface_kind = _normalize_kind(row["surface_kind"])
        surface_key = _normalize_text(row["surface_key"])
        display_name = _binding_display(row)
        summary["chat_bound"] = True
        summary["binding_count"] = int(summary["binding_count"] or 0) + 1
        if summary["binding_kind"] is None:
            summary["binding_kind"] = surface_kind
        if summary["binding_id"] is None:
            summary["binding_id"] = surface_key
        if summary["chat_display_name"] is None:
            summary["chat_display_name"] = display_name
        if surface_kind is not None and surface_kind not in summary["binding_kinds"]:
            summary["binding_kinds"].append(surface_kind)
        if surface_key is not None and surface_key not in summary["binding_ids"]:
            summary["binding_ids"].append(surface_key)
        if (
            display_name is not None
            and display_name not in summary["chat_display_names"]
        ):
            summary["chat_display_names"].append(display_name)
        summary["cleanup_protected"] = bool(
            summary["cleanup_protected"] or metadata.get("cleanup_protected")
        )
    return summaries


def _binding_display(row: Mapping[str, Any]) -> Optional[str]:
    metadata = _json_object(row["metadata_json"])
    for key in ("display_name", "title", "name"):
        value = _normalize_text(metadata.get(key))
        if value is not None:
            return value
    return None


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pma_thread_from_surface(surface: Mapping[str, Any]) -> dict[str, Any]:
    metadata = surface.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    owner = surface.get("resource_owner")
    if not isinstance(owner, Mapping):
        owner = {}
    display = surface.get("display")
    if not isinstance(display, Mapping):
        display = {}
    lifecycle = _normalize_text(surface.get("lifecycle"))
    lifecycle_status = _normalize_text(surface.get("lifecycle_status")) or "active"
    projected_runtime_status = (
        lifecycle if lifecycle in {"idle", "queued", "running", "failed"} else None
    )
    runtime_status = (
        (lifecycle if lifecycle_status == "archived" else None)
        or projected_runtime_status
        or _normalize_text(metadata.get("latest_execution_status"))
        or _normalize_text(metadata.get("runtime_status"))
        or lifecycle
        or ""
    )
    managed_thread_id = _normalize_text(surface.get("managed_thread_id"))
    payload: dict[str, Any] = {
        "managed_thread_id": managed_thread_id,
        "agent": _normalize_text(metadata.get("agent_id")) or "unknown",
        "agent_profile": _normalize_text(metadata.get("agent_profile")),
        "repo_id": _normalize_text(owner.get("repo_id")),
        "resource_kind": _normalize_text(owner.get("resource_kind")),
        "resource_id": _normalize_text(owner.get("resource_id")),
        "workspace_root": _normalize_text(owner.get("workspace_root")),
        "name": _normalize_text(display.get("display_name")) or managed_thread_id,
        "model": _normalize_text(metadata.get("model")),
        "backend_thread_id": _normalize_text(metadata.get("backend_thread_id")),
        "lifecycle_status": lifecycle_status,
        "runtime_status": runtime_status,
        "normalized_status": runtime_status,
        "status": runtime_status,
        "target_runtime_status": _normalize_text(metadata.get("target_runtime_status")),
        "execution_status": _normalize_text(metadata.get("latest_execution_status")),
        "active_turn_id": _normalize_text(metadata.get("active_turn_id")),
        "queued_count": int(metadata.get("queue_depth") or 0),
        "status_reason": _normalize_text(metadata.get("status_reason")),
        "status_changed_at": _normalize_text(metadata.get("status_changed_at")),
        "status_terminal": bool(metadata.get("status_terminal")),
        "status_turn_id": _normalize_text(metadata.get("status_turn_id")),
        "last_turn_id": _normalize_text(metadata.get("last_turn_id")),
        "last_message_preview": _normalize_text(metadata.get("last_message_preview")),
        "compact_seed": _normalize_text(metadata.get("compact_seed")),
        "accepts_messages": lifecycle_status == "active",
        "updated_at": _normalize_text(surface.get("updated_at")),
        "created_at": _normalize_text(surface.get("created_at")),
        "operator_status": (
            "idle" if runtime_status in {"idle", "bound"} else runtime_status
        ),
        "is_reusable": runtime_status in {"idle", "bound"},
    }
    binding_defaults: dict[str, Any] = {
        "chat_bound": False,
        "binding_kind": None,
        "binding_id": None,
        "chat_display_name": None,
        "binding_count": 0,
        "binding_kinds": [],
        "binding_ids": [],
        "chat_display_names": [],
        "cleanup_protected": False,
    }
    for key, default in binding_defaults.items():
        value = metadata.get(key, default)
        payload[key] = list(value) if isinstance(default, list) else value
    return payload


def _read_channel_directory_entries(hub_root: Path) -> list[dict[str, Any]]:
    path = hub_root / ".codex-autorunner" / "chat" / "channel_directory.json"
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, Mapping):
        return []
    entries = parsed.get("entries")
    if not isinstance(entries, list):
        return []
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, Mapping):
            normalized.append(dict(entry))
    return sorted(
        normalized,
        key=lambda item: (
            str(item.get("seen_at") or ""),
            str(item.get("platform") or ""),
            str(item.get("chat_id") or ""),
            str(item.get("thread_id") or ""),
        ),
        reverse=True,
    )


def _public_event_details(payload: Mapping[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    binding = payload.get("binding")
    if isinstance(binding, Mapping):
        details["binding"] = {
            "surface_kind": _normalize_text(binding.get("surface_kind")),
            "surface_key": _normalize_text(binding.get("surface_key")),
            "managed_thread_id": _normalize_text(
                binding.get("thread_target_id") or binding.get("target_id")
            ),
            "repo_id": _normalize_text(binding.get("repo_id")),
            "resource_kind": _normalize_text(binding.get("resource_kind")),
            "resource_id": _normalize_text(binding.get("resource_id")),
            "mode": _normalize_text(binding.get("mode")),
        }
    entry = payload.get("entry")
    if isinstance(entry, Mapping):
        details["channel"] = {
            "platform": _normalize_kind(entry.get("platform")),
            "display": _normalize_text(entry.get("display")),
            "seen_at": _normalize_text(entry.get("seen_at")),
        }
    conversation = payload.get("conversation")
    if isinstance(conversation, Mapping):
        details["notification"] = {
            "notification_id": _normalize_text(conversation.get("notification_id")),
            "source_kind": _normalize_text(conversation.get("source_kind")),
            "delivery_mode": _normalize_text(conversation.get("delivery_mode")),
            "repo_id": _normalize_text(conversation.get("repo_id")),
            "run_id": _normalize_text(conversation.get("run_id")),
            "managed_thread_id": _normalize_text(conversation.get("managed_thread_id")),
            "continuation_thread_target_id": _normalize_text(
                conversation.get("continuation_thread_target_id")
            ),
        }
    for key in ("replaced", "previous_thread_target_id"):
        value = payload.get(key)
        if isinstance(value, (bool, int, float, str)) or value is None:
            details[key] = value
    return details


def _row_get(row: Optional[Mapping[str, Any]], key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


__all__ = [
    "CHAT_SURFACE_READ_CONTRACT_VERSION",
    "PMA_CHAT_EVENTS_CONTRACT_VERSION",
    "ChatSurfaceProjection",
    "ChatSurfaceReadService",
    "parse_chat_surface_cursor",
    "serialize_chat_surface_event",
]
