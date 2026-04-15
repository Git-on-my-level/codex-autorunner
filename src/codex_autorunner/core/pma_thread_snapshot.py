from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from ..tickets.files import safe_relpath
from .chat_bindings import active_chat_binding_metadata_by_thread
from .freshness import build_freshness_payload
from .managed_thread_status import derive_managed_thread_operator_status
from .pma_context_shared import _truncate
from .pma_thread_store import PmaThreadStore

_logger = logging.getLogger(__name__)


def snapshot_pma_threads(
    hub_root: Path,
    *,
    limit: int = 20,
    max_preview_chars: int = 120,
    generated_at: Optional[str] = None,
    stale_threshold_seconds: Optional[int] = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    try:
        store = PmaThreadStore.connect_readonly(hub_root)
        threads = store.list_threads(limit=limit)
    except (OSError, RuntimeError, ValueError, sqlite3.OperationalError) as exc:
        _logger.warning("Could not load PMA managed threads: %s", exc)
        return []
    try:
        chat_binding_metadata = active_chat_binding_metadata_by_thread(
            hub_root=hub_root
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _logger.warning(
            "Could not load PMA chat-binding metadata for thread snapshot: %s", exc
        )
        chat_binding_metadata = {}

    snapshot_threads: list[dict[str, Any]] = []
    for thread in threads[:limit]:
        managed_thread_id = str(thread.get("managed_thread_id") or "").strip()
        workspace_raw = str(thread.get("workspace_root") or "").strip()
        workspace_root = workspace_raw
        if workspace_raw:
            try:
                workspace_root = safe_relpath(Path(workspace_raw).resolve(), hub_root)
            except (OSError, ValueError):
                workspace_root = workspace_raw
        chat_binding = chat_binding_metadata.get(managed_thread_id, {})
        entry: dict[str, Any] = {
            "managed_thread_id": managed_thread_id or thread.get("managed_thread_id"),
            "agent": thread.get("agent"),
            "repo_id": thread.get("repo_id"),
            "resource_kind": thread.get("resource_kind"),
            "resource_id": thread.get("resource_id"),
            "workspace_root": workspace_root,
            "name": thread.get("name"),
            "status": thread.get("normalized_status") or thread.get("status"),
            "lifecycle_status": thread.get("lifecycle_status") or thread.get("status"),
            "status_reason": thread.get("status_reason")
            or thread.get("status_reason_code"),
            "status_terminal": bool(thread.get("status_terminal")),
            "status_changed_at": thread.get("status_changed_at")
            or thread.get("status_updated_at"),
            "last_turn_id": thread.get("last_turn_id"),
            "last_message_preview": _truncate(
                str(thread.get("last_message_preview") or ""),
                max_preview_chars,
            ),
            "updated_at": thread.get("updated_at"),
            "chat_bound": bool(chat_binding.get("chat_bound")),
            "binding_kind": chat_binding.get("binding_kind"),
            "binding_id": chat_binding.get("binding_id"),
            "binding_count": int(chat_binding.get("binding_count") or 0),
            "binding_kinds": list(chat_binding.get("binding_kinds") or []),
            "binding_ids": list(chat_binding.get("binding_ids") or []),
            "cleanup_protected": bool(chat_binding.get("cleanup_protected")),
        }
        if generated_at is not None and stale_threshold_seconds is not None:
            entry["freshness"] = build_freshness_payload(
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                candidates=[
                    ("thread_status_changed_at", entry.get("status_changed_at")),
                    ("thread_updated_at", entry.get("updated_at")),
                ],
            )
        entry["operator_status"] = derive_managed_thread_operator_status(
            normalized_status=entry.get("status"),
            lifecycle_status=entry.get("lifecycle_status"),
        )
        snapshot_threads.append(entry)
    return snapshot_threads
