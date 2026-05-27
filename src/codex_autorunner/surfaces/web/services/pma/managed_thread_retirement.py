from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .....core.orchestration import ChatSurfaceReadService
from .....core.orchestration.chat_surface_emitters import emit_chat_surface_event
from .....core.time_utils import now_iso
from .common import normalize_optional_text


def retirable_notification_surfaces(
    row: dict[str, Any],
) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    surfaces: list[dict[str, str]] = []
    raw_surfaces = list(row.get("surface_bindings") or row.get("surfaces") or [])
    primary = row.get("primary_surface") or row.get("surface")
    if isinstance(primary, dict):
        raw_surfaces.append(primary)
    for raw in raw_surfaces:
        if not isinstance(raw, dict):
            continue
        surface_kind = normalize_optional_text(raw.get("surface_kind"))
        surface_key = normalize_optional_text(raw.get("surface_key"))
        if surface_kind != "notification" or surface_key is None:
            continue
        key = (surface_kind, surface_key)
        if key in seen:
            continue
        seen.add(key)
        surfaces.append({"surface_kind": surface_kind, "surface_key": surface_key})
    return surfaces


def retire_active_chat_index_targets(
    *,
    hub_root: Path,
    service: Any,
    store: Any,
) -> dict[str, Any]:
    retire_targets = ChatSurfaceReadService(
        hub_root, durable=True
    ).chat_index_archive_targets()
    retired_threads: list[Any] = []
    errors: list[dict[str, str]] = []
    retired_row_ids: set[str] = set()
    retired_thread_ids: set[str] = set()
    retired_surfaces: list[dict[str, str]] = []

    for row in retire_targets:
        row_id = (
            normalize_optional_text(row.get("row_id"))
            or normalize_optional_text(row.get("chat_id"))
            or "unknown-chat-row"
        )
        row_retired = False
        managed_thread_id = normalize_optional_text(row.get("managed_thread_id"))
        if managed_thread_id is not None:
            if managed_thread_id in retired_thread_ids:
                row_retired = True
            else:
                thread = service.get_thread_target(managed_thread_id)
                if thread is None:
                    if not retirable_notification_surfaces(row):
                        errors.append(
                            {
                                "thread_id": managed_thread_id,
                                "detail": "Managed thread not found",
                            }
                        )
                elif normalize_optional_text(thread.lifecycle_status) == "archived":
                    retired_thread_ids.add(managed_thread_id)
                    row_retired = True
                else:
                    old_status = normalize_optional_text(thread.lifecycle_status)
                    updated = service.archive_thread_target(managed_thread_id)
                    store.append_action(
                        "managed_thread_retire",
                        managed_thread_id=managed_thread_id,
                        payload_json=json.dumps(
                            {"old_status": old_status}, ensure_ascii=True
                        ),
                    )
                    retired_threads.append(updated)
                    retired_thread_ids.add(managed_thread_id)
                    row_retired = True

        for surface in retirable_notification_surfaces(row):
            surface_key = surface["surface_key"]
            emit_notification_surface_retired(
                hub_root=hub_root,
                surface_key=surface_key,
                row=row,
            )
            retired_surfaces.append(surface)
            row_retired = True

        if row_retired:
            retired_row_ids.add(row_id)

    return {
        "retired_threads": retired_threads,
        "retired_surfaces": retired_surfaces,
        "retired_count": len(retired_row_ids),
        "requested_count": len(retire_targets),
        "errors": errors,
        "error_count": len(errors),
    }


def emit_notification_surface_retired(
    *,
    hub_root: Path,
    surface_key: str,
    row: dict[str, Any],
) -> bool:
    occurred_at = now_iso()
    result = emit_chat_surface_event(
        hub_root,
        idempotency_key=f"notification_surface_archive:{surface_key}",
        event_type="surface.archived",
        surface_kind="notification",
        surface_key=surface_key,
        managed_thread_id=normalize_optional_text(row.get("managed_thread_id")),
        repo_id=normalize_optional_text(row.get("repo_id")),
        resource_kind=normalize_optional_text(row.get("resource_kind")),
        resource_id=normalize_optional_text(row.get("resource_id")),
        workspace_root=normalize_optional_text(row.get("workspace_root")),
        lifecycle_status="archived",
        status="archived",
        source_kind="hub.pma.retire_active",
        source_id=normalize_optional_text(row.get("row_id"))
        or normalize_optional_text(row.get("chat_id"))
        or surface_key,
        payload={"row_id": row.get("row_id"), "chat_id": row.get("chat_id")},
        occurred_at=occurred_at,
    )
    return bool(result.inserted)
