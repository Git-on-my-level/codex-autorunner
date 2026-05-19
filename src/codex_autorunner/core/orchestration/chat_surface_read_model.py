from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..domain.refs import SurfaceRef
from ..domain.workspace_scope import (
    WorkspaceScopeIndex,
    workspace_scope_index_from_snapshots,
)
from ..hub_topology import load_hub_state
from ..state_roots import resolve_repo_flows_db_path
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

_TERMINAL_SUCCESS_STATUSES = {
    "completed",
    "complete",
    "ok",
    "succeeded",
    "success",
    "delivered",
}
_TERMINAL_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled", "timeout"}
_RUNNING_STATUSES = {"running", "in_progress", "started", "claimed", "delivering"}
_QUEUED_STATUSES = {"queued", "pending"}
_DELIVERY_RETRY_STATUSES = {"retry_scheduled"}
_DYNAMIC_LIFECYCLES = frozenset({"idle", "queued", "running", "failed"})
_COMPACT_SEED_BLOCK_PATTERNS = (
    re.compile(
        r"(?is)\bContext from previous conversation:\s*.*?"
        r"(?:Continue from this context\. Ask for missing info if needed\.|$)"
    ),
    re.compile(
        r"(?is)\bCompacted context summary:\s*.*?(?=\n\s*\n|\Z)",
    ),
    re.compile(
        r"(?is)\bContext summary \(from compaction\):\s*.*?(?=\n\s*\n|\Z)",
    ),
)

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
    worktree_id: Optional[str] = None
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
        worktree_id: Optional[str] = None,
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
        self.worktree_id = _prefer(self.worktree_id, worktree_id)
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
                "worktree_id": self.worktree_id,
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
        state_path = self._hub_root / ".codex-autorunner" / "hub_state.json"
        self._scope_index = workspace_scope_index_from_snapshots(
            load_hub_state(state_path, self._hub_root).repos
        )

    def snapshot(
        self, *, limit: int = DEFAULT_CHAT_SURFACE_SNAPSHOT_LIMIT
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        row_limit = _bounded_limit(limit, MAX_CHAT_SURFACE_SNAPSHOT_LIMIT)
        surfaces = self._projected_surfaces(limit=row_limit)
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
        projection = self._query_chat_index_projection(
            view=view,
            query=query,
            surface_kind=surface_kind,
            group_by=group_by,
            parent_group_id=parent_group_id,
            offset=offset,
            limit=limit,
        )
        window = projection["window"]
        groups = projection["groups"]
        counters = projection["counters"]
        total_count = int(projection["total_count"])
        bounded_offset = int(projection["offset"])
        bounded_limit = int(projection["limit"])

        cursor = self.latest_chat_index_projection_revision()
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
            "counters": counters,
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

    def chat_index_patch_batch(
        self,
        cursor: Optional[int],
        *,
        view: str = "all",
        query: Optional[str] = None,
        surface_kind: Optional[str] = None,
        group_by: Optional[str] = None,
        parent_group_id: Optional[str] = None,
        limit: int = DEFAULT_CHAT_SURFACE_EVENT_LIMIT,
        window_limit: int = DEFAULT_CHAT_INDEX_LIMIT,
    ) -> dict[str, Any]:
        """Return compact chat-index projection revision patches.

        The browser stream cursor is intentionally the chat-index projection
        revision, not the raw chat-surface journal offset.  Raw events remain the
        reconstruction source, but stale clients repair from a bounded snapshot
        instead of replaying historical journal rows.
        """

        started_at = time.perf_counter()
        after_cursor = max(0, int(cursor or 0))
        latest_cursor = self.latest_chat_index_projection_revision()
        if after_cursor > latest_cursor:
            gap_cursor = latest_cursor
            return {
                "contract_version": "chat_index_patch_stream.v1",
                "cursor": gap_cursor,
                "events": [
                    _chat_index_cursor_gap_event(
                        cursor=gap_cursor,
                        requested_cursor=after_cursor,
                        latest_cursor=latest_cursor,
                    )
                ],
                "limits": {
                    "requested": int(limit),
                    "returned": 1,
                    "max": MAX_CHAT_SURFACE_EVENT_LIMIT,
                },
            }

        if after_cursor < latest_cursor:
            snapshot = self.chat_index_snapshot(
                view=view,
                query=query,
                surface_kind=surface_kind,
                group_by=group_by,
                parent_group_id=parent_group_id,
                offset=0,
                limit=window_limit,
            )
            payload_events = [
                _chat_index_projection_invalidated_event(
                    cursor=latest_cursor,
                    requested_cursor=after_cursor,
                    snapshot=snapshot,
                )
            ]
            next_cursor = latest_cursor
        else:
            payload_events = []
            next_cursor = after_cursor

        payload = {
            "contract_version": "chat_index_patch_stream.v1",
            "cursor": next_cursor,
            "events": payload_events,
            "limits": {
                "requested": int(limit),
                "returned": len(payload_events),
                "max": MAX_CHAT_SURFACE_EVENT_LIMIT,
            },
        }
        _log_read_model_metric(
            "stream_read_latency",
            started_at,
            snapshot="chat_index",
            returned=len(payload_events),
            cursor=next_cursor,
            cursor_gap_count=sum(
                1
                for event in payload_events
                if event.get("envelope", {}).get("event_type")
                == "projection.cursor_gap"
            ),
        )
        return payload

    def chat_index_archive_targets(self) -> list[dict[str, Any]]:
        """Return every non-archived chat-index row the bulk archive command owns.

        This is intentionally projection-backed so bulk archive and the `/chats`
        counters cannot drift apart.  Managed-thread rows are archiveable through
        the thread lifecycle store; notification-only rows are archiveable through
        a chat-surface lifecycle event.
        """

        self._ensure_chat_index_projection_current()
        where_sql = (
            f"{_CHAT_INDEX_NON_ARCHIVED_SQL} "
            "AND (managed_thread_id IS NOT NULL "
            "OR surface_kind_list LIKE '%|notification|%')"
        )
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            rows = [
                _chat_index_row_from_projection(row)
                for row in conn.execute(
                    f"""
                    SELECT row_json
                      FROM orch_chat_index_projection
                     WHERE {where_sql}
                     ORDER BY sort_unread_priority DESC,
                              sort_last_activity_desc ASC,
                              row_id ASC
                    """
                ).fetchall()
            ]
        return [row for row in rows if row]

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
                self._projected_surfaces(limit=None)
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

    def latest_chat_index_projection_revision(self) -> int:
        self._ensure_chat_index_projection_current()
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            row = conn.execute(
                """
                SELECT value
                  FROM orch_chat_index_projection_meta
                 WHERE key = 'projection_revision'
                """
            ).fetchone()
        if row is None:
            return 0
        try:
            return max(0, int(row["value"] or 0))
        except (TypeError, ValueError):
            return 0

    def chat_index_projection_status(self) -> dict[str, Any]:
        """Return current SQL projection state without rebuilding it."""

        source_signature = self._chat_index_source_signature()
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            if not _table_exists(
                conn, "orch_chat_index_projection"
            ) or not _table_exists(conn, "orch_chat_index_projection_meta"):
                return {
                    "source_signature": source_signature,
                    "stored_source_signature": None,
                    "projection_revision": 0,
                    "row_count": 0,
                    "needs_rebuild": True,
                }
            meta = {
                str(row["key"]): str(row["value"])
                for row in conn.execute(
                    """
                    SELECT key, value
                      FROM orch_chat_index_projection_meta
                     WHERE key IN ('source_signature', 'projection_revision')
                    """
                ).fetchall()
            }
            row = conn.execute(
                "SELECT COUNT(*) AS row_count FROM orch_chat_index_projection"
            ).fetchone()
        stored_signature = meta.get("source_signature")
        return {
            "source_signature": source_signature,
            "stored_source_signature": stored_signature,
            "projection_revision": max(
                0, _safe_int(meta.get("projection_revision"), 0)
            ),
            "row_count": int(row["row_count"] or 0) if row is not None else 0,
            "needs_rebuild": stored_signature != source_signature,
        }

    def repair_stale_bound_surface_archive_state(
        self, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Emit current-generation facts for active bindings shadowed by archives."""

        surfaces = self._projected_surfaces(limit=None)
        projected_by_surface = {
            (
                _normalize_kind(surface.get("surface_kind")),
                _normalize_text(surface.get("surface_key")),
            ): surface
            for surface in surfaces
        }
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            binding_rows = conn.execute(
                """
                SELECT b.binding_id,
                       b.surface_kind,
                       b.surface_key,
                       b.target_id,
                       b.repo_id AS binding_repo_id,
                       b.resource_kind AS binding_resource_kind,
                       b.resource_id AS binding_resource_id,
                       b.metadata_json AS binding_metadata_json,
                       b.updated_at AS binding_updated_at,
                       t.repo_id AS thread_repo_id,
                       t.resource_kind AS thread_resource_kind,
                       t.resource_id AS thread_resource_id,
                       t.workspace_root AS thread_workspace_root,
                       t.lifecycle_status AS thread_lifecycle_status
                  FROM orch_bindings b
                  JOIN orch_thread_targets t
                    ON t.thread_target_id = b.target_id
                 WHERE b.disabled_at IS NULL
                   AND lower(b.surface_kind) IN ('discord', 'telegram')
                   AND COALESCE(t.lifecycle_status, 'active') != 'archived'
                 ORDER BY b.surface_kind ASC, b.surface_key ASC, b.binding_id ASC
                """
            ).fetchall()

        candidates: list[dict[str, Any]] = []
        for row in binding_rows:
            surface_kind = _normalize_kind(row["surface_kind"])
            surface_key = _normalize_text(row["surface_key"])
            managed_thread_id = _normalize_text(row["target_id"])
            if (
                surface_kind not in {"discord", "telegram"}
                or surface_key is None
                or managed_thread_id is None
            ):
                continue
            surface = projected_by_surface.get((surface_kind, surface_key))
            if surface is None:
                continue
            lifecycle_status = _normalize_kind(surface.get("lifecycle_status"))
            lifecycle = _normalize_kind(surface.get("lifecycle"))
            if lifecycle_status != "archived" and lifecycle != "archived":
                continue
            metadata = _json_object(row["binding_metadata_json"])
            candidates.append(
                {
                    "surface_kind": surface_kind,
                    "surface_key": surface_key,
                    "managed_thread_id": managed_thread_id,
                    "binding_id": _normalize_text(row["binding_id"]),
                    "repo_id": _normalize_text(row["binding_repo_id"])
                    or _normalize_text(row["thread_repo_id"]),
                    "resource_kind": _normalize_text(row["binding_resource_kind"])
                    or _normalize_text(row["thread_resource_kind"]),
                    "resource_id": _normalize_text(row["binding_resource_id"])
                    or _normalize_text(row["thread_resource_id"]),
                    "workspace_root": _normalize_text(row["thread_workspace_root"]),
                    "display_name": _normalize_text(metadata.get("display_name")),
                    "stale_lifecycle": lifecycle,
                    "stale_lifecycle_status": lifecycle_status,
                }
            )

        result: dict[str, Any] = {
            "dry_run": dry_run,
            "matched": len(candidates),
            "repaired": 0,
            "already_recorded": 0,
            "candidates": candidates,
            "projection": None,
        }
        if dry_run:
            return result

        repaired = 0
        already_recorded = 0
        for candidate in candidates:
            append = self._journal.append_event(
                idempotency_key=(
                    "repair.stale_bound_surface_archive_state:"
                    f"{candidate['surface_kind']}:"
                    f"{candidate['surface_key']}:"
                    f"{candidate['managed_thread_id']}"
                ),
                event_type="surface.rebound",
                surface_kind=str(candidate["surface_kind"]),
                surface_key=str(candidate["surface_key"]),
                managed_thread_id=str(candidate["managed_thread_id"]),
                repo_id=_normalize_text(candidate.get("repo_id")),
                resource_kind=_normalize_text(candidate.get("resource_kind")),
                resource_id=_normalize_text(candidate.get("resource_id")),
                workspace_root=_normalize_text(candidate.get("workspace_root")),
                lifecycle_status="active",
                status="bound",
                source_kind="chat_index_repair",
                source_id="stale_bound_surface_archive_state",
                payload={
                    "repair": "stale_bound_surface_archive_state",
                    "binding_id": candidate.get("binding_id"),
                    "display": {
                        "display_name": candidate.get("display_name"),
                    },
                },
            )
            if append.inserted:
                repaired += 1
            else:
                already_recorded += 1

        projection = self.rebuild_chat_index_projection()
        result.update(
            {
                "repaired": repaired,
                "already_recorded": already_recorded,
                "projection": projection,
            }
        )
        if candidates:
            logger.info(
                "repaired stale bound surface archive state",
                extra={
                    "event": "chat_index_repair",
                    "repair": "stale_bound_surface_archive_state",
                    "matched": len(candidates),
                    "repaired": repaired,
                    "already_recorded": already_recorded,
                },
            )
        return result

    def rebuild_chat_index_projection(self) -> dict[str, Any]:
        before = self.chat_index_projection_status()
        source_signature = self._chat_index_source_signature()
        surfaces = self._projected_surfaces(limit=None)
        rows = sorted(
            _chat_index_rows_from_surfaces(surfaces), key=_chat_index_sort_key
        )
        rebuilt_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            with conn:
                existing_meta = {
                    str(row["key"]): str(row["value"])
                    for row in conn.execute(
                        """
                        SELECT key, value
                          FROM orch_chat_index_projection_meta
                         WHERE key IN ('source_signature', 'projection_revision')
                        """
                    ).fetchall()
                }
                if existing_meta.get("source_signature") == source_signature:
                    projection_revision = max(
                        1, _safe_int(existing_meta.get("projection_revision"), 1)
                    )
                else:
                    projection_revision = (
                        _safe_int(existing_meta.get("projection_revision"), 0) + 1
                    )
                conn.execute("DELETE FROM orch_chat_index_projection")
                conn.executemany(
                    """
                    INSERT INTO orch_chat_index_projection (
                        row_id,
                        chat_id,
                        managed_thread_id,
                        surface_kinds_json,
                        surface_kind_list,
                        lifecycle_status,
                        runtime_status,
                        effective_status,
                        queue_depth,
                        unread_count,
                        unread,
                        last_activity_at,
                        updated_at,
                        created_at,
                        repo_id,
                        worktree_id,
                        resource_kind,
                        resource_id,
                        ticket_id,
                        run_id,
                        group_id,
                        search_text,
                        sort_unread_priority,
                        sort_last_activity_desc,
                        row_json,
                        source_signature,
                        rebuilt_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        _chat_index_projection_params(
                            row,
                            source_signature=source_signature,
                            rebuilt_at=rebuilt_at,
                        )
                        for row in rows
                    ],
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO orch_chat_index_projection_meta (
                        key,
                        value,
                        updated_at
                    ) VALUES ('source_signature', ?, ?)
                    """,
                    (source_signature, rebuilt_at),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO orch_chat_index_projection_meta (
                        key,
                        value,
                        updated_at
                    ) VALUES ('projection_revision', ?, ?)
                    """,
                    (str(projection_revision), rebuilt_at),
                )
        return {
            "rebuilt": True,
            "row_count": len(rows),
            "projection_revision": projection_revision,
            "previous_projection_revision": before.get("projection_revision", 0),
            "source_signature": source_signature,
            "previous_source_signature": before.get("stored_source_signature"),
            "source_changed": before.get("stored_source_signature") != source_signature,
            "rebuilt_at": rebuilt_at,
        }

    def _ensure_chat_index_projection_current(self) -> None:
        source_signature = self._chat_index_source_signature()
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            if not _table_exists(
                conn, "orch_chat_index_projection"
            ) or not _table_exists(conn, "orch_chat_index_projection_meta"):
                needs_rebuild = True
            else:
                row = conn.execute(
                    """
                    SELECT value
                      FROM orch_chat_index_projection_meta
                     WHERE key = 'source_signature'
                    """
                ).fetchone()
                needs_rebuild = row is None or row["value"] != source_signature
        if needs_rebuild:
            self.rebuild_chat_index_projection()

    def _chat_index_source_signature(self) -> str:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            table_updated_exprs = {
                "orch_thread_targets": "MAX(COALESCE(updated_at, created_at))",
                "orch_thread_executions": "MAX(COALESCE(finished_at, started_at, created_at))",
                "orch_bindings": "MAX(COALESCE(updated_at, created_at))",
                "orch_managed_thread_deliveries": "MAX(COALESCE(updated_at, delivered_at, created_at))",
                "orch_notification_conversations": "MAX(COALESCE(updated_at, created_at))",
                "orch_chat_surface_events": "MAX(COALESCE(created_at, occurred_at, event_id))",
                "orch_flow_run_projections": "MAX(updated_at)",
            }
            facts: dict[str, Any] = {}
            for table, updated_expr in table_updated_exprs.items():
                if not _table_exists(conn, table):
                    facts[table] = None
                    continue
                row = conn.execute(
                    f"SELECT COUNT(*) AS count, {updated_expr} AS max_updated FROM {table}"
                ).fetchone()
                facts[table] = {
                    "count": int(row["count"] or 0),
                    "max_updated": row["max_updated"],
                }
            if _table_exists(conn, "orch_thread_targets"):
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count,
                           MAX(COALESCE(updated_at, created_at)) AS max_updated
                      FROM orch_thread_targets
                     WHERE json_extract(metadata_json, '$.flow_type') = 'ticket_flow'
                       AND json_extract(metadata_json, '$.ticket_flow_link_key') IS NOT NULL
                    """
                ).fetchone()
                facts["ticket_flow_thread_links"] = {
                    "count": int(row["count"] or 0),
                    "max_updated": row["max_updated"],
                }
            else:
                facts["ticket_flow_thread_links"] = None
            facts["ticket_flow_ticket_files"] = _ticket_flow_ticket_file_signature(conn)
            if _table_exists(conn, "orch_flow_run_projections"):
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count,
                           MAX(updated_at) AS max_updated
                      FROM orch_flow_run_projections
                     WHERE flow_type = 'ticket_flow'
                    """
                ).fetchone()
                facts["ticket_flow_flow_projections"] = {
                    "count": int(row["count"] or 0),
                    "max_updated": row["max_updated"],
                }
            else:
                facts["ticket_flow_flow_projections"] = None
        directory_path = (
            self._hub_root / ".codex-autorunner" / "chat" / "channel_directory.json"
        )
        try:
            stat = directory_path.stat()
            facts["channel_directory"] = {
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
            }
        except OSError:
            facts["channel_directory"] = None
        basis = json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def _query_chat_index_projection(
        self,
        *,
        view: str,
        query: Optional[str],
        surface_kind: Optional[str],
        group_by: Optional[str],
        parent_group_id: Optional[str],
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._ensure_chat_index_projection_current()
        bounded_offset = max(0, int(offset or 0))
        bounded_limit = _bounded_limit(limit, MAX_CHAT_INDEX_LIMIT)
        where_sql, params = _chat_index_projection_where(
            view=view,
            query=query,
            surface_kind=surface_kind,
            parent_group_id=parent_group_id,
        )
        normalized_view = (view or "all").strip().lower()
        if normalized_view == "all":
            where_counters_sql, counters_params = _chat_index_projection_where(
                view=view,
                query=query,
                surface_kind=surface_kind,
                parent_group_id=parent_group_id,
                include_archived_rows=True,
            )
            counters_sql = f"""
                SELECT COALESCE(SUM(CASE WHEN {_CHAT_INDEX_NON_ARCHIVED_SQL} THEN 1 ELSE 0 END), 0) AS total,
                       COALESCE(SUM(CASE WHEN {_CHAT_INDEX_NON_ARCHIVED_SQL} AND queue_depth > 0 THEN 1 ELSE 0 END), 0) AS waiting,
                       COALESCE(SUM(CASE WHEN {_CHAT_INDEX_NON_ARCHIVED_SQL} AND effective_status = 'running' THEN 1 ELSE 0 END), 0) AS running,
                       COALESCE(SUM(CASE WHEN {_CHAT_INDEX_NON_ARCHIVED_SQL} THEN unread_count ELSE 0 END), 0) AS unread,
                       COALESCE(SUM(CASE WHEN NOT ({_CHAT_INDEX_NON_ARCHIVED_SQL}) THEN 1 ELSE 0 END), 0) AS archived
                  FROM orch_chat_index_projection
                 WHERE {where_counters_sql}
                """
        else:
            where_counters_sql, counters_params = where_sql, params
            counters_sql = f"""
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN queue_depth > 0 THEN 1 ELSE 0 END), 0) AS waiting,
                       COALESCE(SUM(CASE WHEN effective_status = 'running' THEN 1 ELSE 0 END), 0) AS running,
                       COALESCE(SUM(unread_count), 0) AS unread,
                       COALESCE(SUM(CASE WHEN NOT ({_CHAT_INDEX_NON_ARCHIVED_SQL}) THEN 1 ELSE 0 END), 0) AS archived
                  FROM orch_chat_index_projection
                 WHERE {where_counters_sql}
                """
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            counters_row = conn.execute(
                counters_sql,
                counters_params,
            ).fetchone()
            counters = {
                "total": int(counters_row["total"] or 0),
                "waiting": int(counters_row["waiting"] or 0),
                "running": int(counters_row["running"] or 0),
                "unread": int(counters_row["unread"] or 0),
                "archived": int(counters_row["archived"] or 0),
            }
            if group_by == "ticket_run" and parent_group_id is None:
                all_rows = [
                    _chat_index_row_from_projection(row)
                    for row in conn.execute(
                        f"""
                        SELECT row_json
                          FROM orch_chat_index_projection
                         WHERE {where_sql}
                         ORDER BY sort_unread_priority DESC,
                                  sort_last_activity_desc ASC,
                                  row_id ASC
                        """,
                        params,
                    ).fetchall()
                ]
                groups = _filter_chat_index_groups(
                    _ticket_run_groups(all_rows),
                    view=view,
                    query=query,
                )
                window_rows = groups[bounded_offset : bounded_offset + bounded_limit]
                total_count = len(groups)
            else:
                total_count = counters["total"]
                page_params = [*params, bounded_limit, bounded_offset]
                window_rows = [
                    _chat_index_row_from_projection(row)
                    for row in conn.execute(
                        f"""
                        SELECT row_json
                          FROM orch_chat_index_projection
                         WHERE {where_sql}
                         ORDER BY sort_unread_priority DESC,
                                  sort_last_activity_desc ASC,
                                  row_id ASC
                         LIMIT ? OFFSET ?
                        """,
                        page_params,
                    ).fetchall()
                ]
                if group_by == "ticket_run":
                    all_rows = [
                        _chat_index_row_from_projection(row)
                        for row in conn.execute(
                            f"""
                            SELECT row_json
                              FROM orch_chat_index_projection
                             WHERE {where_sql}
                             ORDER BY sort_unread_priority DESC,
                                      sort_last_activity_desc ASC,
                                      row_id ASC
                            """,
                            params,
                        ).fetchall()
                    ]
                    groups = _ticket_run_groups(all_rows)
                else:
                    groups = []
        return {
            "offset": bounded_offset,
            "limit": bounded_limit,
            "rows": window_rows,
            "groups": groups,
            "counters": counters,
            "total_count": total_count,
            "window": window_rows,
        }

    def _projected_surfaces(self, *, limit: Optional[int]) -> list[dict[str, Any]]:
        projections: dict[tuple[str, str], ChatSurfaceProjection] = {}
        self._project_channel_directory(projections)
        self._project_orchestration_tables(projections)
        self._project_events(projections)
        surfaces = sorted(
            (projection.to_dict() for projection in projections.values()),
            key=lambda item: (item["surface_kind"], item["surface_key"]),
        )
        if limit is None:
            return surfaces
        return surfaces[:limit]

    def _project_channel_directory(
        self, projections: dict[tuple[str, str], ChatSurfaceProjection]
    ) -> None:
        for entry in _read_channel_directory_entries(self._hub_root):
            surface_kind = _normalize_kind(entry.get("platform"))
            chat_id = _normalize_text(entry.get("chat_id"))
            if surface_kind is None or chat_id is None:
                continue
            owner_fields = canonical_owner_fields(
                self._scope_index,
                repo_id=entry.get("repo_id"),
                resource_kind=entry.get("resource_kind"),
                resource_id=entry.get("resource_id"),
                workspace_root=entry.get("workspace_path"),
                scope_urn=entry.get("scope_urn"),
            )
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
                repo_id=owner_fields.get("repo_id"),
                resource_kind=owner_fields.get("resource_kind"),
                resource_id=owner_fields.get("resource_id"),
                workspace_root=owner_fields.get("workspace_root"),
                scope_urn=owner_fields.get("scope_urn"),
                worktree_id=owner_fields.get("worktree_id"),
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
                """,
            ).fetchall()
            execution_rows = conn.execute(
                """
                SELECT thread_target_id,
                       execution_id,
                       status,
                       prompt_text,
                       metadata_json,
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
                """,
            ).fetchall()
            notification_rows = (
                conn.execute(
                    """
                    SELECT *
                      FROM orch_notification_conversations
                     ORDER BY updated_at ASC, created_at ASC, notification_id ASC
                    """,
                ).fetchall()
                if _table_exists(conn, "orch_notification_conversations")
                else []
            )
            flow_projection_rows = (
                conn.execute(
                    """
                    SELECT flow_run_id,
                           repo_id,
                           status,
                           summary_json,
                           updated_at
                      FROM orch_flow_run_projections
                     WHERE flow_type = 'ticket_flow'
                    """
                ).fetchall()
                if _table_exists(conn, "orch_flow_run_projections")
                else []
            )

        execution_by_thread = _latest_execution_by_thread(execution_rows)
        queue_depth_by_thread = _queue_depth_by_thread(execution_rows)
        delivery_by_surface = _latest_delivery_by_surface(delivery_rows)
        delivery_by_thread = _latest_delivery_by_thread(delivery_rows)
        thread_owner: dict[str, Mapping[str, Any]] = {}
        binding_summary_by_thread = _binding_summary_by_thread(binding_rows)
        ticket_flow_projections_by_run = _ticket_flow_projection_by_run_id(
            flow_projection_rows
        )

        for row in thread_rows:
            thread_id = str(row["thread_target_id"])
            thread_owner[thread_id] = row
            owner_fields = canonical_owner_fields(
                self._scope_index,
                repo_id=row["repo_id"],
                resource_kind=_row_get(row, "resource_kind"),
                resource_id=_row_get(row, "resource_id"),
                workspace_root=row["workspace_root"],
                scope_urn=_row_get(row, "scope_urn"),
            )
            execution = _thread_execution_for_projection(
                row, execution_by_thread.get(thread_id)
            )
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
            chat_kind = _normalize_text(metadata.get("chat_kind"))
            run_id = _normalize_text(metadata.get("run_id"))
            last_activity_at = _max_iso(
                _normalize_text(row["updated_at"]),
                _normalize_text(execution["created_at"]) if execution else None,
            )
            last_message_preview = _visible_turn_chrome_text(
                _row_get(row, "last_message_preview"),
                execution,
            )
            projection.merge(
                lifecycle=lifecycle,
                lifecycle_status=lifecycle_status,
                repo_id=owner_fields.get("repo_id"),
                resource_kind=owner_fields.get("resource_kind"),
                resource_id=owner_fields.get("resource_id"),
                workspace_root=owner_fields.get("workspace_root"),
                scope_urn=owner_fields.get("scope_urn"),
                worktree_id=owner_fields.get("worktree_id"),
                managed_thread_id=thread_id,
                display_name=_visible_chrome_text(row["display_name"]) or thread_id,
                created_at=_normalize_text(row["created_at"]),
                updated_at=last_activity_at,
                archived_at=(
                    _normalize_text(row["updated_at"])
                    if lifecycle_status == "archived"
                    else None
                ),
                fact="managed_thread",
                metadata={
                    "agent_id": _normalize_text(row["agent_id"]),
                    "agent_profile": _normalize_text(metadata.get("agent_profile")),
                    "chat_kind": chat_kind,
                    "flow_type": _normalize_text(metadata.get("flow_type")),
                    "run_id": run_id,
                    "ticket_id": _normalize_text(metadata.get("ticket_id")),
                    "ticket_path": _normalize_text(metadata.get("ticket_path")),
                    "ticket_done": _bool_or_none(metadata.get("ticket_done")),
                    "ticket_status": _normalize_text(metadata.get("ticket_status")),
                    "ticket_flow_projection": _ticket_flow_projection_for_row(
                        ticket_flow_projections_by_run,
                        run_id=run_id,
                        repo_id=_normalize_text(row["repo_id"]),
                        workspace_root=_normalize_text(metadata.get("workspace_root")),
                    ),
                    "workspace_root": _normalize_text(metadata.get("workspace_root")),
                    "backend_thread_id": _normalize_text(
                        _row_get(row, "backend_thread_id")
                    ),
                    "model": _normalize_text(metadata.get("model")),
                    "thread_kind": _normalize_text(metadata.get("thread_kind")),
                    "last_activity_at": last_activity_at,
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
                    "last_message_preview": last_message_preview,
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
            owner_fields = canonical_owner_fields(
                self._scope_index,
                repo_id=_normalize_text(row["repo_id"])
                or _normalize_text(_row_get(owner, "repo_id")),
                resource_kind=_normalize_text(_row_get(row, "resource_kind"))
                or _normalize_text(_row_get(owner, "resource_kind")),
                resource_id=_normalize_text(_row_get(row, "resource_id"))
                or _normalize_text(_row_get(owner, "resource_id")),
                workspace_root=_normalize_text(_row_get(row, "workspace_root"))
                or _normalize_text(_row_get(owner, "workspace_root")),
                scope_urn=_normalize_text(_row_get(row, "scope_urn"))
                or _normalize_text(_row_get(owner, "scope_urn")),
            )
            execution = _thread_execution_for_projection(
                owner, execution_by_thread.get(binding_thread_id or "")
            )
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
            binding_last_activity_at = _max_iso(
                _normalize_text(_row_get(owner, "updated_at")),
                _normalize_text(execution["created_at"]) if execution else None,
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
                repo_id=owner_fields.get("repo_id"),
                resource_kind=owner_fields.get("resource_kind"),
                resource_id=owner_fields.get("resource_id"),
                workspace_root=owner_fields.get("workspace_root"),
                scope_urn=owner_fields.get("scope_urn"),
                worktree_id=owner_fields.get("worktree_id"),
                managed_thread_id=binding_thread_id,
                display_name=_binding_display(row),
                created_at=_normalize_text(row["created_at"]),
                updated_at=_normalize_text(row["updated_at"]),
                fact="binding",
                metadata={
                    "mode": _normalize_text(row["mode"]),
                    "agent_id": _normalize_text(row["agent_id"]),
                    "last_activity_at": binding_last_activity_at,
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
            owner_fields = canonical_owner_fields(
                self._scope_index,
                repo_id=event.repo_id,
                resource_kind=event.resource_kind,
                resource_id=event.resource_id,
                workspace_root=event.workspace_root,
            )
            projection.merge(
                lifecycle=_event_lifecycle(event),
                lifecycle_status=event.lifecycle_status,
                repo_id=owner_fields.get("repo_id"),
                resource_kind=owner_fields.get("resource_kind"),
                resource_id=owner_fields.get("resource_id"),
                workspace_root=owner_fields.get("workspace_root"),
                scope_urn=owner_fields.get("scope_urn"),
                worktree_id=owner_fields.get("worktree_id"),
                managed_thread_id=event.managed_thread_id,
                external_conversation_id=event.external_conversation_id,
                external_provider=event.surface_kind,
                display_name=_visible_chrome_text(display.get("display_name")),
                title=_visible_chrome_text(display.get("title")),
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


def _ticket_flow_ticket_file_signature(conn: Any) -> list[dict[str, Any]]:
    if not _table_exists(conn, "orch_thread_targets"):
        return []
    rows = conn.execute(
        """
        SELECT json_extract(metadata_json, '$.workspace_root') AS workspace_root,
               json_extract(metadata_json, '$.ticket_path') AS ticket_path
          FROM orch_thread_targets
         WHERE json_extract(metadata_json, '$.flow_type') = 'ticket_flow'
           AND json_extract(metadata_json, '$.ticket_path') IS NOT NULL
        """
    ).fetchall()
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        workspace_root = _normalize_text(row["workspace_root"])
        ticket_path = _normalize_text(row["ticket_path"])
        if workspace_root is None or ticket_path is None:
            continue
        key = (workspace_root, ticket_path)
        if key in seen:
            continue
        seen.add(key)
        path = Path(ticket_path)
        if not path.is_absolute():
            path = Path(workspace_root) / path
        fact: dict[str, Any] = {
            "workspace_root": workspace_root,
            "ticket_path": ticket_path,
        }
        try:
            resolved_path = path.resolve()
            resolved_root = Path(workspace_root).resolve()
            resolved_path.relative_to(resolved_root)
            stat = resolved_path.stat()
            fact.update({"mtime_ns": stat.st_mtime_ns, "size": stat.st_size})
        except (OSError, ValueError):
            fact["missing"] = True
        facts.append(fact)
    return sorted(
        facts,
        key=lambda item: (
            str(item.get("workspace_root") or ""),
            str(item.get("ticket_path") or ""),
        ),
    )


def _ticket_flow_projection_by_run_id(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    projections: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        run_id = _normalize_text(row["flow_run_id"])
        if run_id is None:
            continue
        summary = _json_object(row["summary_json"])
        projection = _ticket_flow_projection_from_summary(
            run_id=run_id,
            status=_normalize_text(row["status"]),
            summary=summary,
            updated_at=_normalize_text(row["updated_at"]),
            repo_id=_normalize_text(row["repo_id"]),
        )
        projections.setdefault(run_id, []).append(projection)
    return projections


def _ticket_flow_projection_for_row(
    projections_by_run: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    run_id: Optional[str],
    repo_id: Optional[str],
    workspace_root: Optional[str],
) -> Optional[dict[str, Any]]:
    if run_id is None:
        return None
    candidates = list(projections_by_run.get(run_id, ()))
    local_projection = _ticket_flow_projection_from_local_store(
        run_id=run_id,
        workspace_root=workspace_root,
    )
    if local_projection is not None:
        return local_projection
    for candidate in candidates:
        if _ticket_flow_projection_matches_row(
            candidate,
            repo_id=repo_id,
            workspace_root=workspace_root,
        ):
            return dict(candidate)
    if len(candidates) == 1 and _ticket_flow_projection_matches_row(
        candidates[0],
        repo_id=repo_id,
        workspace_root=workspace_root,
    ):
        return dict(candidates[0])
    return None


def _ticket_flow_projection_matches_row(
    projection: Mapping[str, Any],
    *,
    repo_id: Optional[str],
    workspace_root: Optional[str],
) -> bool:
    projection_repo_id = _normalize_text(projection.get("repo_id"))
    if repo_id is not None and projection_repo_id is None:
        return False
    if (
        repo_id is not None
        and projection_repo_id is not None
        and repo_id != projection_repo_id
    ):
        return False
    projection_workspace = _normalize_text(projection.get("workspace_root"))
    if workspace_root is not None and projection_workspace is None:
        return False
    if (
        workspace_root is not None
        and projection_workspace is not None
        and Path(workspace_root).resolve() != Path(projection_workspace).resolve()
    ):
        return False
    return True


def _ticket_flow_projection_from_local_store(
    *,
    run_id: str,
    workspace_root: Optional[str],
) -> Optional[dict[str, Any]]:
    if workspace_root is None:
        return None
    try:
        db_path = resolve_repo_flows_db_path(Path(workspace_root).resolve())
        if not db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            record = conn.execute(
                """
                SELECT id,
                       flow_type,
                       status,
                       state,
                       created_at,
                       started_at,
                       finished_at,
                       metadata
                  FROM flow_runs
                 WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        logger.debug(
            "Could not read local ticket-flow store for chat projection",
            exc_info=True,
        )
        return None
    if record is None or record["flow_type"] != "ticket_flow":
        return None
    return _ticket_flow_projection_from_flow_row(
        record,
        workspace_root=workspace_root,
    )


def _ticket_flow_projection_from_flow_row(
    record: Mapping[str, Any],
    *,
    workspace_root: Optional[str],
) -> dict[str, Any]:
    state = _json_object(record["state"])
    ticket_engine = state.get("ticket_engine")
    ticket_engine = ticket_engine if isinstance(ticket_engine, Mapping) else {}
    metadata = _json_object(record["metadata"])
    summary = {
        "workspace_root": workspace_root,
        "current_ticket": ticket_engine.get("current_ticket")
        or state.get("current_ticket"),
        "ticket_engine": ticket_engine,
    }
    return _ticket_flow_projection_from_summary(
        run_id=str(record["id"]),
        status=_normalize_text(record["status"]),
        summary=summary,
        updated_at=(
            _normalize_text(record["finished_at"])
            or _normalize_text(record["started_at"])
            or _normalize_text(record["created_at"])
        ),
        repo_id=_normalize_text(metadata.get("repo_id")),
    )


def _ticket_flow_projection_from_summary(
    *,
    run_id: str,
    status: Optional[str],
    summary: Mapping[str, Any],
    updated_at: Optional[str],
    repo_id: Optional[str],
) -> dict[str, Any]:
    ticket_engine = summary.get("ticket_engine")
    ticket_engine = ticket_engine if isinstance(ticket_engine, Mapping) else {}
    ticket_engine_commit = ticket_engine.get("commit")
    ticket_engine_commit = (
        ticket_engine_commit if isinstance(ticket_engine_commit, Mapping) else {}
    )
    raw_current_ticket_done = ticket_engine.get("current_ticket_done")
    if raw_current_ticket_done is None:
        raw_current_ticket_done = ticket_engine_commit.get("current_ticket_done")
    if raw_current_ticket_done is None:
        raw_current_ticket_done = summary.get("current_ticket_done")
    return {
        "run_id": run_id,
        "repo_id": repo_id,
        "workspace_root": _normalize_text(summary.get("workspace_root")),
        "status": status,
        "updated_at": updated_at,
        "current_ticket": _normalize_text(
            ticket_engine.get("current_ticket") or summary.get("current_ticket")
        ),
        "current_ticket_done": _bool_or_none(raw_current_ticket_done),
        "ticket_engine_status": _normalize_text(ticket_engine.get("status")),
    }


def _chat_index_rows_from_surfaces(
    surfaces: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    surface_list = list(surfaces)
    loadable_thread_ids = {
        thread_id
        for surface in surface_list
        for thread_id in [_normalize_text(surface.get("managed_thread_id"))]
        if thread_id is not None
        and "managed_thread"
        in {str(fact) for fact in (surface.get("facts") or []) if fact is not None}
    }
    by_thread: dict[str, dict[str, Any]] = {}
    external_rows: list[dict[str, Any]] = []
    for surface in surface_list:
        surface_kind = _normalize_text(surface.get("surface_kind")) or ""
        surface_key = _normalize_text(surface.get("surface_key")) or ""
        managed_thread_id = _normalize_text(surface.get("managed_thread_id"))
        owner = surface.get("resource_owner")
        resource_owner = dict(owner) if isinstance(owner, Mapping) else {}
        display = surface.get("display")
        display_map = dict(display) if isinstance(display, Mapping) else {}
        metadata = surface.get("metadata")
        metadata_map = dict(metadata) if isinstance(metadata, Mapping) else {}
        last_message_preview = _visible_chrome_text(
            metadata_map.get("last_message_preview")
        )
        base_surface = {
            "surface_kind": surface_kind,
            "surface_key": surface_key,
            "surface_urn": surface.get("surface_urn"),
            "lifecycle": surface.get("lifecycle"),
            "display_name": _visible_chrome_text(display_map.get("display_name")),
            "title": _visible_chrome_text(display_map.get("title")),
            "binding_display_name": _surface_binding_display_name(surface),
        }
        if managed_thread_id is None:
            title = _display_title(display_map, surface_key)
            external_rows.append(
                {
                    "row_type": "chat",
                    "row_id": f"surface:{surface_kind}:{surface_key}",
                    "chat_id": f"surface:{surface_kind}:{surface_key}",
                    "managed_thread_id": None,
                    "surface": base_surface,
                    "surfaces": [base_surface],
                    "title": title,
                    "display_title": title,
                    "technical_title": f"{surface_kind}:{surface_key}",
                    "primary_surface": base_surface,
                    "surface_bindings": [base_surface],
                    "binding_display_name": base_surface["binding_display_name"],
                    "binding_display_names": _binding_display_names([base_surface]),
                    "repo_id": resource_owner.get("repo_id"),
                    "worktree_id": _worktree_id_from_owner(resource_owner),
                    "resource_kind": resource_owner.get("resource_kind"),
                    "resource_id": resource_owner.get("resource_id"),
                    "workspace_root": resource_owner.get("workspace_root"),
                    "lifecycle": surface.get("lifecycle"),
                    "lifecycle_status": surface.get("lifecycle_status"),
                    "runtime_status": metadata_map.get("runtime_status"),
                    "latest_event_cursor": surface.get("latest_event_cursor"),
                    "last_activity_at": metadata_map.get("last_activity_at"),
                    "updated_at": surface.get("updated_at"),
                    "created_at": surface.get("created_at"),
                    "last_message_preview": last_message_preview,
                    "unread": bool(metadata_map.get("unread")),
                    "unread_count": int(metadata_map.get("unread_count") or 0),
                    "active_turn_id": metadata_map.get("active_turn_id"),
                    "queue_depth": int(metadata_map.get("queue_depth") or 0),
                    "archive_state": _archive_state(surface.get("lifecycle_status")),
                    "search_text": "",
                }
            )
            continue
        if managed_thread_id not in loadable_thread_ids:
            continue
        row = by_thread.get(managed_thread_id)
        if row is None:
            row = {
                "row_type": "chat",
                "row_id": f"thread:{managed_thread_id}",
                "chat_id": managed_thread_id,
                "managed_thread_id": managed_thread_id,
                "surfaces": [],
                "title": managed_thread_id,
                "technical_title": managed_thread_id,
                "repo_id": resource_owner.get("repo_id"),
                "worktree_id": _worktree_id_from_owner(resource_owner),
                "resource_kind": resource_owner.get("resource_kind"),
                "resource_id": resource_owner.get("resource_id"),
                "workspace_root": resource_owner.get("workspace_root"),
                "lifecycle": surface.get("lifecycle"),
                "lifecycle_status": surface.get("lifecycle_status"),
                "runtime_status": metadata_map.get("runtime_status"),
                "target_runtime_status": metadata_map.get("target_runtime_status"),
                "latest_event_cursor": surface.get("latest_event_cursor"),
                "last_activity_at": metadata_map.get("last_activity_at")
                or surface.get("updated_at"),
                "updated_at": surface.get("updated_at"),
                "created_at": surface.get("created_at"),
                "last_message_preview": last_message_preview,
                "agent": metadata_map.get("agent_id"),
                "agent_profile": metadata_map.get("agent_profile"),
                "model": metadata_map.get("model"),
                "active_turn_id": metadata_map.get("active_turn_id"),
                "queue_depth": int(metadata_map.get("queue_depth") or 0),
                "unread": bool(metadata_map.get("unread")),
                "unread_count": int(metadata_map.get("unread_count") or 0),
                "cleanup_protected": bool(metadata_map.get("cleanup_protected")),
                "flow_type": metadata_map.get("flow_type"),
                "ticket_id": metadata_map.get("ticket_id"),
                "ticket_path": metadata_map.get("ticket_path"),
                "ticket_done": metadata_map.get("ticket_done"),
                "ticket_status": metadata_map.get("ticket_status"),
                "ticket_flow_projection": metadata_map.get("ticket_flow_projection"),
                "run_id": metadata_map.get("run_id"),
            }
            by_thread[managed_thread_id] = row
        row["surfaces"].append(base_surface)
        row["lifecycle"] = _choose_lifecycle(
            str(row["lifecycle"] or "bound"), surface.get("lifecycle")
        )
        row["last_activity_at"] = _max_iso(
            row.get("last_activity_at"), metadata_map.get("last_activity_at")
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
                "worktree_id",
                "resource_kind",
                "resource_id",
                "workspace_root",
                "lifecycle_status",
            ):
                if key == "worktree_id":
                    row[key] = _worktree_id_from_owner(resource_owner) or row.get(key)
                else:
                    row[key] = resource_owner.get(key) or row.get(key)
            for key in (
                "runtime_status",
                "target_runtime_status",
                "agent_id",
                "agent_profile",
                "chat_kind",
                "flow_type",
                "model",
                "active_turn_id",
                "workspace_root",
                "ticket_id",
                "ticket_path",
                "ticket_done",
                "ticket_status",
                "ticket_flow_projection",
                "run_id",
            ):
                if metadata_map.get(key) is not None:
                    row["agent" if key == "agent_id" else key] = metadata_map.get(key)
            if last_message_preview is not None:
                row["last_message_preview"] = last_message_preview
            row["queue_depth"] = max(
                int(row.get("queue_depth") or 0),
                int(metadata_map.get("queue_depth") or 0),
            )
    rows = list(by_thread.values()) + external_rows
    for row in rows:
        row["last_message_preview"] = _visible_chrome_text(
            row.get("last_message_preview")
        )
        if row.get("managed_thread_id") is None:
            friendly_title = _friendly_chat_title(row)
            if friendly_title is not None:
                row["chat_display_name"] = friendly_title
                if _is_fallback_chat_title(row.get("title"), row):
                    row["title"] = friendly_title
        else:
            # Chat identity is owned by the managed thread. Delivery surfaces,
            # notification reply contexts, and channel bindings stay visible as
            # bindings, but they must not replace the PMA-owned display title.
            identity_title = _managed_thread_identity_title(row)
            row["title"] = identity_title
            row["chat_display_name"] = identity_title
        row["display_title"] = _normalize_text(row.get("chat_display_name")) or str(
            row.get("title") or row.get("chat_id") or row.get("row_id") or ""
        )
        _apply_ticket_flow_child_state(row)
        row["technical_title"] = _normalize_text(row.get("technical_title")) or str(
            row.get("managed_thread_id") or row.get("row_id") or ""
        )
        primary_surface = row.get("surface")
        if not isinstance(primary_surface, Mapping):
            primary_surface = _primary_surface(row)
        row["primary_surface"] = dict(primary_surface) if primary_surface else None
        row["surface_bindings"] = [
            dict(surface)
            for surface in row.get("surfaces", [])
            if isinstance(surface, Mapping)
        ]
        binding_display_names = _binding_display_names(row["surface_bindings"])
        row["binding_display_names"] = binding_display_names
        row["binding_display_name"] = (
            binding_display_names[0] if binding_display_names else None
        )
        if (
            row.get("managed_thread_id") is not None
            and _normalize_kind(row.get("lifecycle_status")) != "archived"
            and _normalize_kind(row.get("lifecycle")) == "archived"
        ):
            row["lifecycle"] = _managed_thread_row_lifecycle(row)
        row["archive_state"] = _archive_state(row.get("lifecycle_status"))
        row["sort_key"] = _chat_index_sort_key_parts(row)
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
        _visible_chrome_text(display.get("title"))
        or _visible_chrome_text(display.get("display_name"))
        or fallback
    )


def _visible_chrome_text(value: Any) -> Optional[str]:
    text = _normalize_text(value)
    if text is None:
        return None
    if _contains_legacy_transport_marker(text):
        return None
    cleaned = text
    for pattern in _COMPACT_SEED_BLOCK_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return _normalize_text(cleaned)


def _contains_legacy_transport_marker(text: str) -> bool:
    lowered = text.lower()
    return "<injected context>" in lowered or "</injected context>" in lowered


def _visible_turn_chrome_text(
    stored_preview: Any,
    execution: Optional[Mapping[str, Any]],
) -> Optional[str]:
    execution_metadata = _json_object(_row_get(execution, "metadata_json"))
    return (
        _visible_chrome_text(execution_metadata.get("title_seed"))
        or _visible_chrome_text(execution_metadata.get("user_visible_text"))
        or _visible_chrome_text(stored_preview)
        or _visible_chrome_text(_row_get(execution, "prompt_text"))
    )


def _managed_thread_identity_title(row: Mapping[str, Any]) -> str:
    """Return the primary PMA-owned title for a managed-thread chat row."""

    managed_thread_id = _normalize_text(row.get("managed_thread_id"))
    title = _visible_chrome_text(row.get("title"))
    if title is not None and not _is_fallback_chat_title(title, row):
        return title
    preview = _visible_chrome_text(row.get("last_message_preview"))
    if preview is not None:
        return preview
    return managed_thread_id or _normalize_text(row.get("chat_id")) or title or ""


def _surface_binding_display_name(surface: Mapping[str, Any]) -> Optional[str]:
    display = surface.get("display")
    display_map = display if isinstance(display, Mapping) else {}
    return _visible_chrome_text(
        display_map.get("display_name")
    ) or _visible_chrome_text(display_map.get("title"))


def _binding_display_names(surfaces: Iterable[Mapping[str, Any]]) -> list[str]:
    names: list[str] = []
    for surface in surfaces:
        name = _visible_chrome_text(
            surface.get("binding_display_name")
            or surface.get("display_name")
            or surface.get("title")
        )
        if name is None or _is_surface_id_title(name, surface):
            continue
        if name not in names:
            names.append(name)
    return names


def _primary_surface(row: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    surfaces = row.get("surfaces")
    if not isinstance(surfaces, list):
        return None
    for surface in surfaces:
        if isinstance(surface, Mapping) and surface.get("surface_kind") == "pma":
            return surface
    for surface in surfaces:
        if isinstance(surface, Mapping):
            return surface
    return None


def _archive_state(lifecycle_status: Any) -> str:
    return "archived" if _normalize_kind(lifecycle_status) == "archived" else "active"


def _managed_thread_row_lifecycle(row: Mapping[str, Any]) -> str:
    if int(row.get("queue_depth") or 0) > 0:
        return "queued"
    runtime = _status_to_lifecycle(
        row.get("runtime_status") or row.get("target_runtime_status")
    )
    if runtime in {"idle", "running", "failed"}:
        return runtime
    return "bound"


def canonical_owner_fields(
    scope_index: WorkspaceScopeIndex,
    *,
    repo_id: Any = None,
    resource_kind: Any = None,
    resource_id: Any = None,
    workspace_root: Any = None,
    scope_urn: Any = None,
) -> dict[str, Optional[str]]:
    normalized_resource_kind = _normalize_kind(resource_kind)
    normalized_resource_id = _normalize_text(resource_id)
    if normalized_resource_kind not in {None, "repo", "worktree", "filesystem"}:
        resolution = scope_index.resolve(
            raw_repo_id=repo_id,
            workspace_path=workspace_root,
            scope_urn=scope_urn,
        )
        fields = (
            resolution.owner_fields()
            if resolution is not None
            else {
                "repo_id": _normalize_text(repo_id),
                "worktree_id": None,
                "resource_kind": None,
                "resource_id": None,
                "workspace_root": _normalize_text(workspace_root),
                "scope_urn": _normalize_text(scope_urn),
            }
        )
        fields["resource_kind"] = normalized_resource_kind
        fields["resource_id"] = normalized_resource_id
        return fields
    resolution = scope_index.resolve(
        raw_repo_id=repo_id,
        workspace_path=workspace_root,
        resource_kind=resource_kind,
        resource_id=resource_id,
        scope_urn=scope_urn,
    )
    if resolution is not None:
        return resolution.owner_fields()
    return {
        "repo_id": _normalize_text(repo_id),
        "worktree_id": None,
        "resource_kind": _normalize_text(resource_kind),
        "resource_id": _normalize_text(resource_id),
        "workspace_root": _normalize_text(workspace_root),
        "scope_urn": _normalize_text(scope_urn),
    }


def _worktree_id_from_owner(owner: Mapping[str, Any]) -> Optional[str]:
    if _normalize_kind(owner.get("resource_kind")) == "worktree":
        return _normalize_text(owner.get("resource_id"))
    return _normalize_text(owner.get("worktree_id"))


def _friendly_chat_title(row: Mapping[str, Any]) -> Optional[str]:
    """Return a non-technical chat surface label, preferring bound messenger names."""

    for surface in row.get("surfaces", []) or []:
        if not isinstance(surface, Mapping):
            continue
        kind = _normalize_kind(surface.get("surface_kind"))
        if kind == "pma":
            continue
        for key in ("title", "display_name"):
            value = _visible_chrome_text(surface.get(key))
            if value is None:
                continue
            if _is_surface_id_title(value, surface):
                continue
            return value
    return None


def _is_surface_id_title(value: Any, row: Mapping[str, Any]) -> bool:
    title = _normalize_text(value)
    if title is None:
        return False
    lowered = title.lower()
    return lowered.startswith("discord:") or lowered.startswith("telegram:")


def _is_fallback_chat_title(value: Any, row: Mapping[str, Any]) -> bool:
    title = _normalize_text(value)
    if title is None:
        return True
    managed_thread_id = _normalize_text(row.get("managed_thread_id"))
    if title == managed_thread_id:
        return True
    if managed_thread_id is not None and title == f"Thread {managed_thread_id}":
        return True
    if _is_surface_id_title(title, row):
        return True
    return title.lower().startswith("ticket-flow:")


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
        _visible_chrome_text(row.get("title")),
        row.get("repo_id"),
        row.get("resource_kind"),
        row.get("resource_id"),
        row.get("agent"),
        row.get("agent_profile"),
        row.get("model"),
        _visible_chrome_text(row.get("last_message_preview")),
        " ".join(row.get("surface_kinds") or []),
    ]
    return " ".join(str(value).lower() for value in values if value)


def _chat_index_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
    priority = 1 if row.get("unread") else 0
    raw = str(
        row.get("last_activity_at")
        or row.get("updated_at")
        or row.get("created_at")
        or ""
    )
    parsed = _parse_iso_timestamp(raw)
    updated_key = float("inf") if parsed is None else -parsed.timestamp()
    return (
        -priority,
        updated_key,
        str(row.get("row_id") or ""),
    )


def _chat_index_effective_status(row: Mapping[str, Any]) -> str:
    lifecycle = _normalize_kind(row.get("lifecycle"))
    lifecycle_status = _normalize_kind(row.get("lifecycle_status"))
    runtime = _status_to_lifecycle(
        row.get("runtime_status") or row.get("target_runtime_status")
    )
    if lifecycle_status == "archived":
        return "archived"
    if row.get("managed_thread_id") is None and (
        lifecycle == "archived" or runtime == "archived"
    ):
        return "archived"
    if int(row.get("queue_depth") or 0) > 0:
        return "waiting"
    if runtime == "idle":
        return "idle"
    if runtime == "failed":
        return "failed"
    if runtime == "running" or lifecycle == "running":
        return "running"
    return "idle"


def _chat_index_projection_params(
    row: Mapping[str, Any],
    *,
    source_signature: str,
    rebuilt_at: str,
) -> tuple[Any, ...]:
    surface_kinds = sorted(str(kind) for kind in row.get("surface_kinds") or [])
    raw_sort_key = row.get("sort_key")
    sort_key: Mapping[str, Any] = (
        raw_sort_key if isinstance(raw_sort_key, Mapping) else {}
    )
    last_activity_desc = sort_key.get("last_activity_desc")
    if last_activity_desc is not None:
        last_activity_desc = float(last_activity_desc)
    return (
        str(row.get("row_id") or row.get("chat_id")),
        str(row.get("chat_id") or row.get("row_id")),
        _normalize_text(row.get("managed_thread_id")),
        json.dumps(surface_kinds, sort_keys=True, separators=(",", ":")),
        "|" + "|".join(surface_kinds) + "|" if surface_kinds else "",
        _normalize_text(row.get("lifecycle_status")),
        _normalize_text(row.get("runtime_status")),
        _chat_index_effective_status(row),
        int(row.get("queue_depth") or 0),
        int(row.get("unread_count") or 0),
        1 if row.get("unread") else 0,
        _normalize_text(row.get("last_activity_at")),
        _normalize_text(row.get("updated_at")),
        _normalize_text(row.get("created_at")),
        _normalize_text(row.get("repo_id")),
        _normalize_text(row.get("worktree_id")),
        _normalize_text(row.get("resource_kind")),
        _normalize_text(row.get("resource_id")),
        _normalize_text(row.get("ticket_id")),
        _normalize_text(row.get("run_id")),
        _normalize_text(row.get("group_id")),
        str(row.get("search_text") or ""),
        int(sort_key.get("unread_priority") or 0),
        last_activity_desc,
        json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str),
        source_signature,
        rebuilt_at,
    )


def _chat_index_row_from_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(str(row["row_json"]))
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


_CHAT_INDEX_NON_ARCHIVED_SQL = (
    "(lifecycle_status IS NULL OR lifecycle_status != 'archived') "
    "AND effective_status != 'archived'"
)


def _chat_index_projection_where(
    *,
    view: str,
    query: Optional[str],
    surface_kind: Optional[str],
    parent_group_id: Optional[str],
    include_archived_rows: bool = False,
) -> tuple[str, list[Any]]:
    normalized_view = (view or "all").strip().lower()
    normalized_query = _normalize_text(query)
    normalized_query = normalized_query.lower() if normalized_query else None
    normalized_surface = _normalize_kind(surface_kind)
    clauses = ["1 = 1"]
    params: list[Any] = []
    if parent_group_id is not None:
        clauses.append("group_id = ?")
        params.append(parent_group_id)
    if normalized_view != "external":
        clauses.append("managed_thread_id IS NOT NULL")
    if normalized_surface is not None:
        clauses.append("surface_kind_list LIKE ?")
        params.append(f"%|{normalized_surface}|%")
    if normalized_view == "waiting":
        clauses.append("queue_depth > 0")
    elif normalized_view == "active":
        clauses.append("effective_status = 'running'")
    elif normalized_view == "unread":
        clauses.append("unread != 0")
    elif normalized_view == "archived":
        clauses.append(f"NOT ({_CHAT_INDEX_NON_ARCHIVED_SQL})")
    elif normalized_view == "external":
        clauses.append("surface_kind_list != ''")
        clauses.append("surface_kind_list != '|pma|'")
    elif normalized_view == "ticket_run":
        clauses.append("group_id IS NOT NULL")
    elif normalized_view != "all":
        clauses.append("0 = 1")
    if normalized_view != "archived" and not (
        include_archived_rows and normalized_view == "all"
    ):
        clauses.append(_CHAT_INDEX_NON_ARCHIVED_SQL)
    if normalized_query is not None:
        clauses.append("search_text LIKE ?")
        params.append(f"%{normalized_query}%")
    return " AND ".join(clauses), params


def _chat_index_sort_key_parts(row: Mapping[str, Any]) -> dict[str, Any]:
    key = _chat_index_sort_key(row)
    last_activity_desc: Any = key[1]
    if last_activity_desc == float("inf"):
        last_activity_desc = None
    return {
        "unread_priority": -key[0],
        "last_activity_desc": last_activity_desc,
        "row_id": key[2],
    }


def _chat_ticket_group_id(row: Mapping[str, Any]) -> Optional[str]:
    run_id = _normalize_text(row.get("run_id"))
    if _normalize_kind(row.get("flow_type")) == "ticket_flow" and run_id is not None:
        return f"run:{run_id}"
    ticket_id = _normalize_text(row.get("ticket_id") or row.get("current_ticket_id"))
    if ticket_id is not None:
        return f"ticket:{ticket_id}"
    if run_id is not None:
        return f"run:{run_id}"
    kind = _normalize_kind(row.get("resource_kind"))
    identifier = _normalize_text(row.get("resource_id"))
    if kind in {"ticket", "ticket_run", "run"} and identifier is not None:
        return f"{kind}:{identifier}"
    thread_id = _normalize_text(row.get("managed_thread_id"))
    if thread_id and thread_id.startswith("ticket-run:"):
        return ":".join(thread_id.split(":", 2)[:2])
    return None


def _apply_ticket_flow_child_state(row: dict[str, Any]) -> None:
    """Populate ticket-flow progress fields without changing chat lifecycle."""

    if _normalize_kind(row.get("flow_type")) != "ticket_flow":
        return

    ticket_file_done = _ticket_done_from_row_path(row)
    if ticket_file_done is not None:
        row["ticket_done"] = ticket_file_done
        row["ticket_status"] = (
            "done"
            if ticket_file_done
            else _ticket_status_fallback(row, allow_done=False)
        )
        row["ticket_progress_source"] = "ticket_file"
        return

    flow_store_state = _ticket_flow_store_child_state(row)
    if flow_store_state is not None:
        row["ticket_done"] = flow_store_state["ticket_done"]
        row["ticket_status"] = flow_store_state["ticket_status"]
        row["ticket_progress_source"] = "flow_store"
        return

    explicit_done = _bool_or_none(row.get("ticket_done"))
    if explicit_done is True:
        row["ticket_done"] = True
        row["ticket_status"] = "done"
        row["ticket_progress_source"] = "managed_thread"
        return
    if explicit_done is False:
        row["ticket_done"] = False

    explicit_status = _normalize_kind(row.get("ticket_status"))
    if explicit_status in {"done", "running", "waiting", "failed", "unknown"}:
        row["ticket_status"] = explicit_status
        if explicit_status == "done":
            row["ticket_done"] = True
        row["ticket_progress_source"] = "managed_thread"
        return

    row["ticket_status"] = _ticket_status_fallback(row)
    if row["ticket_status"] == "done":
        row["ticket_done"] = True
    elif _bool_or_none(row.get("ticket_done")) is None:
        row["ticket_done"] = None
    row["ticket_progress_source"] = "managed_thread_runtime"


def _ticket_status_fallback(row: Mapping[str, Any], *, allow_done: bool = True) -> str:
    if int(row.get("queue_depth") or 0) > 0:
        return "waiting"
    runtime_raw = _normalize_kind(
        row.get("runtime_status") or row.get("target_runtime_status")
    )
    runtime = _status_to_lifecycle(runtime_raw)
    if allow_done and runtime == "idle" and runtime_raw in _TERMINAL_SUCCESS_STATUSES:
        return "done"
    if runtime in {"running", "failed"}:
        return runtime
    if _normalize_kind(row.get("lifecycle")) == "running":
        return "running"
    return "unknown"


def _ticket_flow_store_child_state(
    row: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    projection = row.get("ticket_flow_projection")
    if not isinstance(projection, Mapping):
        return None
    current_ticket = _normalize_text(projection.get("current_ticket"))
    if current_ticket is None or not _row_matches_flow_store_ticket(
        row, current_ticket
    ):
        return None

    current_ticket_done = _bool_or_none(projection.get("current_ticket_done"))
    if current_ticket_done is True:
        return {"ticket_done": True, "ticket_status": "done"}

    status = _normalize_kind(
        projection.get("ticket_engine_status") or projection.get("status")
    )
    ticket_status = _flow_store_status_to_ticket_status(status)
    if current_ticket_done is False:
        if ticket_status is None or ticket_status == "done":
            ticket_status = "running"
        return {"ticket_done": False, "ticket_status": ticket_status}
    if ticket_status is None:
        return None
    return {"ticket_done": ticket_status == "done", "ticket_status": ticket_status}


def _row_matches_flow_store_ticket(row: Mapping[str, Any], current_ticket: str) -> bool:
    ticket_path = _normalize_text(row.get("ticket_path"))
    if ticket_path is not None and _ticket_ref_matches(ticket_path, current_ticket):
        return True
    ticket_id = _normalize_text(row.get("ticket_id"))
    if ticket_id is None:
        return False
    return _ticket_ref_matches(ticket_id, current_ticket)


def _ticket_ref_matches(left: str, right: str) -> bool:
    left_text = left.strip()
    right_text = right.strip()
    if left_text == right_text:
        return True
    left_name = Path(left_text).name
    right_name = Path(right_text).name
    if left_name == right_name:
        return True
    left_stem = Path(left_name).stem
    right_stem = Path(right_name).stem
    return bool(left_stem and right_stem and left_stem == right_stem)


def _flow_store_status_to_ticket_status(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    if status in {"completed", "complete", "done", "succeeded", "success"}:
        return "done"
    if status in {"failed", "error", "cancelled", "canceled", "timeout", "stopped"}:
        return "failed"
    if status in {"paused", "pending", "queued", "waiting"}:
        return "waiting"
    if status in {"running", "in_progress", "started", "claimed"}:
        return "running"
    return None


def _ticket_done_from_row_path(row: Mapping[str, Any]) -> Optional[bool]:
    ticket_path = _normalize_text(row.get("ticket_path"))
    workspace_root = _normalize_text(row.get("workspace_root"))
    if ticket_path is None or workspace_root is None:
        return None
    path = Path(ticket_path)
    if not path.is_absolute():
        path = Path(workspace_root) / path
    try:
        resolved_path = path.resolve()
        resolved_root = Path(workspace_root).resolve()
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    frontmatter = _read_markdown_frontmatter_mapping(resolved_path)
    ticket_done = _bool_or_none(frontmatter.get("done"))
    if ticket_done is None:
        return None
    return ticket_done


def _read_markdown_frontmatter_mapping(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    data: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return data
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key = key.strip()
        if key:
            data[key] = value.strip().strip("\"'")
    return {}


def _bool_or_none(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
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
        total_count = len(children)
        done_count = sum(
            1 for child in children if _ticket_child_status(child) == "done"
        )
        failed_count = sum(
            1 for child in children if _ticket_child_status(child) == "failed"
        )
        running_count = sum(
            1 for child in children if _ticket_child_status(child) == "running"
        )
        waiting_count = sum(
            1 for child in children if _ticket_child_status(child) == "waiting"
        )
        groups.append(
            {
                "row_type": "group",
                "kind": "ticket_run_group",
                "row_id": f"group:{group_id}",
                "group_id": group_id,
                "run_id": _ticket_run_id_from_children(group_id, children),
                "scope_kind": _ticket_run_scope_kind(children),
                "scope_id": _ticket_run_scope_id(children),
                "title": group_id,
                "status": _ticket_run_group_status(
                    total_count=total_count,
                    done_count=done_count,
                    running_count=running_count,
                    waiting_count=waiting_count,
                    failed_count=failed_count,
                ),
                "child_count": total_count,
                "total_count": total_count,
                "done_count": done_count,
                "waiting_count": waiting_count,
                "running_count": running_count,
                "failed_count": failed_count,
                "unread_count": sum(
                    int(child.get("unread_count") or 0) for child in children
                ),
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


def _ticket_child_status(row: Mapping[str, Any]) -> str:
    explicit = _normalize_kind(row.get("ticket_status"))
    if explicit in {"done", "running", "waiting", "failed", "unknown"}:
        return explicit
    if row.get("ticket_done") is True:
        return "done"
    effective = _chat_index_effective_status(row)
    if effective in {"running", "waiting", "failed"}:
        return effective
    if int(row.get("queue_depth") or 0) > 0:
        return "waiting"
    return "unknown"


def _ticket_run_id_from_children(
    group_id: str, children: Sequence[Mapping[str, Any]]
) -> str:
    for child in children:
        run_id = _normalize_text(child.get("run_id"))
        if run_id is not None:
            return run_id
    if group_id.startswith(("run:", "ticket-run:")):
        return group_id.split(":", 1)[1]
    return group_id


def _ticket_run_scope_kind(children: Sequence[Mapping[str, Any]]) -> str:
    return "worktree" if any(child.get("worktree_id") for child in children) else "repo"


def _ticket_run_scope_id(children: Sequence[Mapping[str, Any]]) -> str:
    for key in ("worktree_id", "repo_id", "workspace_root"):
        for child in children:
            value = _normalize_text(child.get(key))
            if value is not None:
                return value
    return "unknown"


def _ticket_run_group_status(
    *,
    total_count: int,
    done_count: int,
    running_count: int,
    waiting_count: int,
    failed_count: int,
) -> str:
    if waiting_count > 0:
        return "waiting"
    if running_count > 0:
        return "running"
    if failed_count > 0:
        return "failed"
    if total_count > 0 and done_count >= total_count:
        return "done"
    return "idle"


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
    managed_thread_id = thread.get("managed_thread_id") or thread.get(
        "thread_target_id"
    )
    stored_title = thread.get("display_name") or thread.get("name")
    chat_display_name = next(
        (
            _visible_chrome_text(row.get("chat_display_name"))
            for row in surface_rows
            if _visible_chrome_text(row.get("chat_display_name")) is not None
        ),
        None,
    )
    title = stored_title
    if chat_display_name and _is_fallback_chat_title(
        stored_title, {"managed_thread_id": managed_thread_id}
    ):
        title = chat_display_name
    return {
        "managed_thread_id": managed_thread_id,
        "title": title,
        "chat_display_name": chat_display_name,
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
        "last_message_preview": _visible_chrome_text(
            thread.get("last_message_preview")
        ),
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
        "prompt_preview": _visible_chrome_text(item.get("prompt")) or "",
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


def _chat_index_cursor_gap_event(
    *,
    cursor: int,
    requested_cursor: int,
    latest_cursor: int,
) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "envelope": {
            "event_type": "projection.cursor_gap",
            "cursor": cursor,
            "entity_kind": "chat",
            "entity_id": "chat.index",
            "operation": "invalidate",
            "generated_at": now,
        },
        "patch": {
            "rows": [],
            "groups": [],
            "removed_row_ids": [],
            "removed_group_ids": [],
            "order": None,
            "counters": None,
        },
        "repair": {
            "requested_cursor": requested_cursor,
            "latest_cursor": latest_cursor,
            "snapshot_route": "/hub/read-models/chats",
        },
    }


def _chat_index_projection_invalidated_event(
    *,
    cursor: int,
    requested_cursor: int,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    rows_raw = snapshot.get("rows") or []
    rows = [row for row in rows_raw if isinstance(row, Mapping)]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "envelope": {
            "event_type": "projection.cursor_gap",
            "cursor": cursor,
            "entity_kind": "chat",
            "entity_id": "chat.index",
            "operation": "invalidate",
            "generated_at": now,
        },
        "patch": {
            "rows": [],
            "groups": [],
            "removed_row_ids": [],
            "removed_group_ids": [],
            "order": None,
            "counters": _chat_index_patch_counters(snapshot, rows),
        },
        "repair": {
            "requested_cursor": requested_cursor,
            "latest_cursor": cursor,
            "snapshot_route": "/hub/read-models/chats",
        },
    }


def _chat_index_patch_counters(
    snapshot: Mapping[str, Any], rows: list[Mapping[str, Any]]
) -> dict[str, int]:
    counters = snapshot.get("counters")
    if isinstance(counters, Mapping):
        return {
            "total": max(0, int(counters.get("total") or 0)),
            "waiting": max(0, int(counters.get("waiting") or 0)),
            "running": max(0, int(counters.get("running") or 0)),
            "unread": max(0, int(counters.get("unread") or 0)),
            "archived": max(0, int(counters.get("archived") or 0)),
        }
    return {
        "total": int((snapshot.get("window") or {}).get("total_count") or 0),
        "waiting": sum(1 for row in rows if int(row.get("queue_depth") or 0) > 0),
        "running": sum(
            1 for row in rows if _chat_index_effective_status(row) == "running"
        ),
        "unread": sum(int(row.get("unread_count") or 0) for row in rows),
        "archived": sum(1 for row in rows if row.get("lifecycle_status") == "archived"),
    }


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


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
    if current == "archived" and candidate == "bound":
        return candidate
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


def _thread_execution_for_projection(
    row: Optional[Mapping[str, Any]],
    execution: Optional[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    if execution is None:
        return None
    execution_status = _status_to_lifecycle(_row_get(execution, "status"))
    if row is None:
        if execution_status in {"running", "queued"}:
            return None
        return execution
    if execution_status not in {"running", "queued"}:
        return execution
    lifecycle_status = _normalize_kind(_row_get(row, "lifecycle_status"))
    if lifecycle_status == "archived":
        return None
    runtime_status = _normalize_kind(_row_get(row, "runtime_status"))
    # A prior turn can leave runtime terminal while another execution is still
    # queued; keep queued rows visible for queue_depth / active-turn projections.
    if execution_status == "queued":
        return execution
    if runtime_status in {
        "completed",
        "interrupted",
        "failed",
        "archived",
    }:
        return None
    return execution


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
    metadata_runtime_status = _normalize_text(metadata.get("runtime_status"))
    terminal_runtime_status = (
        metadata_runtime_status
        if _normalize_kind(metadata_runtime_status) in _TERMINAL_SUCCESS_STATUSES
        else None
    )
    runtime_status = (
        (lifecycle if lifecycle_status == "archived" else None)
        or terminal_runtime_status
        or projected_runtime_status
        or _normalize_text(metadata.get("latest_execution_status"))
        or metadata_runtime_status
        or lifecycle
        or ""
    )
    managed_thread_id = _normalize_text(surface.get("managed_thread_id"))
    chat_display_name = _visible_chrome_text(metadata.get("chat_display_name"))
    payload: dict[str, Any] = {
        "managed_thread_id": managed_thread_id,
        "agent": _normalize_text(metadata.get("agent_id")) or "unknown",
        "agent_profile": _normalize_text(metadata.get("agent_profile")),
        "repo_id": _normalize_text(owner.get("repo_id")),
        "resource_kind": _normalize_text(owner.get("resource_kind")),
        "resource_id": _normalize_text(owner.get("resource_id")),
        "workspace_root": _normalize_text(owner.get("workspace_root")),
        "name": _visible_chrome_text(display.get("display_name")) or managed_thread_id,
        "chat_display_name": chat_display_name,
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
        "last_message_preview": _visible_chrome_text(
            metadata.get("last_message_preview")
        ),
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
    thread = payload.get("thread")
    if isinstance(thread, Mapping):
        meta = thread.get("metadata")
        meta_map = dict(meta) if isinstance(meta, Mapping) else {}
        agent_id = _normalize_text(thread.get("agent_id")) or _normalize_text(
            thread.get("agent")
        )
        details["thread"] = {
            "managed_thread_id": _normalize_text(
                thread.get("managed_thread_id") or thread.get("thread_target_id")
            ),
            "agent_id": agent_id,
            "agent_profile": _normalize_text(meta_map.get("agent_profile")),
            "model": _normalize_text(meta_map.get("model")),
        }
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
