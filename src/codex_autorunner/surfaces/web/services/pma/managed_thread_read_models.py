from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from .....adapters.chat.approval_modes import normalize_approval_mode
from .....adapters.chat.channel_directory import (
    ChannelDirectoryStore,
    channel_entry_key,
)
from .....core.car_context import (
    default_managed_thread_context_profile,
    normalize_car_context_profile,
)
from .....core.chat_bindings import active_chat_binding_metadata_by_thread
from .....core.hub_control_plane.models import THREAD_TARGET_LIST_LIFECYCLE_STATUSES
from .....core.managed_thread_kinds import infer_managed_thread_chat_kind
from .....core.managed_thread_status import derive_managed_thread_operator_status
from .....core.orchestration import ActiveWorkSummary, ManagedThreadExecutionStore
from .....core.orchestration.models import Binding, ThreadTarget
from .....core.text_utils import _truncate_text
from .....tickets.files import ticket_is_done
from ..chat_status_contract import normalize_chat_effective_status
from .common import normalize_optional_text
from .managed_thread_scope import (
    ManagedThreadCreateResolution,
    ManagedThreadWorkspaceProvision,
    _normalize_resource_owner,
    _normalize_workspace_root_input,
    managed_thread_metadata_for_provisioned_workspace,
    provision_managed_thread_workspace,
    resolve_managed_thread_create_resolution,
)

_logger = logging.getLogger(__name__)


def _resolve_running_or_latest_execution(
    service: Any,
    managed_thread_id: str,
) -> Any:
    """Prefer running execution, else latest; minimize redundant store reads when possible."""
    thread_store = getattr(service, "thread_store", None)
    if isinstance(thread_store, ManagedThreadExecutionStore):
        get_latest_execution = getattr(service, "get_latest_execution", None)
        if callable(get_latest_execution):
            return get_latest_execution(managed_thread_id)
        return None
    get_running_execution = getattr(service, "get_running_execution", None)
    get_latest_execution = getattr(service, "get_latest_execution", None)
    execution = None
    if callable(get_running_execution):
        execution = get_running_execution(managed_thread_id)
    if execution is None and callable(get_latest_execution):
        execution = get_latest_execution(managed_thread_id)
    return execution


@dataclass(frozen=True)
class ManagedThreadListQuery:
    agent_id: Optional[str]
    lifecycle_status: Optional[str]
    runtime_status: Optional[str]
    repo_id: Optional[str]
    resource_kind: Optional[str]
    resource_id: Optional[str]
    limit: int


@dataclass(frozen=True)
class ManagedThreadOwnerScopedQuery:
    agent_id: Optional[str]
    repo_id: Optional[str]
    resource_kind: Optional[str]
    resource_id: Optional[str]
    limit: int


def _build_operator_status_fields(
    *,
    normalized_status: Optional[str],
    lifecycle_status: Optional[str],
) -> dict[str, Any]:
    operator_status = derive_managed_thread_operator_status(
        normalized_status=normalized_status,
        lifecycle_status=lifecycle_status,
    )
    return {
        "operator_status": operator_status,
        "is_reusable": operator_status in {"idle", "reusable"},
    }


def _serialize_managed_thread(thread: dict[str, Any]) -> dict[str, Any]:
    payload = dict(thread)
    lifecycle_status = normalize_optional_text(
        thread.get("lifecycle_status") or thread.get("status")
    )
    normalized_status = normalize_optional_text(thread.get("normalized_status"))
    payload["lifecycle_status"] = lifecycle_status
    payload["normalized_status"] = normalized_status or lifecycle_status or ""
    payload["status"] = payload["normalized_status"]
    payload["status_reason"] = normalize_optional_text(
        thread.get("status_reason") or thread.get("status_reason_code")
    )
    payload["status_changed_at"] = normalize_optional_text(
        thread.get("status_changed_at") or thread.get("status_updated_at")
    )
    payload["status_terminal"] = bool(thread.get("status_terminal"))
    payload["status_turn_id"] = normalize_optional_text(thread.get("status_turn_id"))
    payload["accepts_messages"] = lifecycle_status == "active"
    payload["resource_kind"] = normalize_optional_text(thread.get("resource_kind"))
    payload["resource_id"] = normalize_optional_text(thread.get("resource_id"))
    metadata = thread.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    payload["context_profile"] = normalize_car_context_profile(
        metadata.get("context_profile"),
        default=default_managed_thread_context_profile(),
    )
    payload["approval_mode"] = normalize_approval_mode(
        metadata.get("approval_mode"),
        default="yolo",
    )
    payload["agent_profile"] = normalize_optional_text(
        thread.get("agent_profile") or metadata.get("agent_profile")
    )
    payload.update(
        _ticket_flow_thread_fields(
            metadata=metadata,
            workspace_root=normalize_optional_text(thread.get("workspace_root")),
            display_name=normalize_optional_text(
                thread.get("name") or thread.get("display_name")
            ),
        )
    )
    payload.update(
        _build_operator_status_fields(
            normalized_status=payload["normalized_status"],
            lifecycle_status=lifecycle_status,
        )
    )
    return payload


def _ticket_done_from_path(
    *, workspace_root: Optional[str], ticket_path: Optional[str]
) -> Optional[bool]:
    if not workspace_root or not ticket_path:
        return None
    try:
        path = Path(ticket_path)
        if not path.is_absolute():
            path = Path(workspace_root) / path
        if not path.is_file():
            return None
        return bool(ticket_is_done(path))
    except (OSError, ValueError):
        return None


def _ticket_flow_thread_fields(
    *,
    metadata: dict[str, Any],
    workspace_root: Optional[str],
    display_name: Optional[str],
) -> dict[str, Any]:
    flow_type = normalize_optional_text(metadata.get("flow_type"))
    thread_kind = normalize_optional_text(metadata.get("thread_kind"))
    ticket_id = normalize_optional_text(metadata.get("ticket_id"))
    ticket_path = normalize_optional_text(metadata.get("ticket_path"))
    run_id = normalize_optional_text(metadata.get("run_id"))
    is_ticket_flow = (
        flow_type == "ticket_flow"
        or thread_kind == "ticket_flow"
        or bool(display_name and display_name.startswith("ticket-flow:"))
    )
    if not is_ticket_flow:
        return {}
    fields: dict[str, Any] = {
        "flow_type": flow_type or "ticket_flow",
        "thread_kind": thread_kind,
        "run_id": run_id,
        "ticket_id": ticket_id,
        "ticket_path": ticket_path,
        "ticket_done": _ticket_done_from_path(
            workspace_root=workspace_root, ticket_path=ticket_path
        ),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _chat_binding_defaults() -> dict[str, Any]:
    return {
        "chat_bound": False,
        "binding_kind": None,
        "binding_id": None,
        "chat_display_name": None,
        "binding_count": 0,
        "binding_kinds": [],
        "binding_ids": [],
        "chat_display_names": [],
        "cleanup_protected": False,
        "retire_protected": False,
    }


def _load_chat_binding_metadata_by_thread(hub_root: Path) -> dict[str, dict[str, Any]]:
    try:
        metadata = active_chat_binding_metadata_by_thread(hub_root=hub_root)
    except Exception as exc:  # intentional: non-critical metadata load
        _logger.warning(
            "Could not load PMA chat-binding metadata for thread response: %s", exc
        )
        return {}
    return _enrich_chat_binding_metadata_with_channel_names(metadata, hub_root=hub_root)


def _enrich_chat_binding_metadata_with_channel_names(
    metadata_by_thread: dict[str, dict[str, Any]], *, hub_root: Path
) -> dict[str, dict[str, Any]]:
    if not metadata_by_thread:
        return metadata_by_thread
    try:
        entries = ChannelDirectoryStore(hub_root).list_entries(limit=None)
    except Exception as exc:  # intentional: optional display-name enrichment
        _logger.warning(
            "Could not load chat channel directory for thread response: %s", exc
        )
        return metadata_by_thread
    display_by_key = {
        key: display
        for entry in entries
        if (key := channel_entry_key(entry))
        and (display := normalize_optional_text(entry.get("display")))
    }
    if not display_by_key:
        return metadata_by_thread

    enriched: dict[str, dict[str, Any]] = {}
    for thread_id, metadata in metadata_by_thread.items():
        if not isinstance(metadata, dict):
            continue
        item = dict(metadata)
        binding_kind = normalize_optional_text(item.get("binding_kind"))
        binding_id = normalize_optional_text(item.get("binding_id"))
        display_name = _chat_binding_display_name(
            binding_kind, binding_id, display_by_key
        )
        binding_displays: list[str] = []
        for raw_id in item.get("binding_ids") or []:
            raw_text = normalize_optional_text(raw_id)
            if not raw_text:
                continue
            raw_kind = _surface_kind_for_binding_id(raw_text, item)
            raw_display = _chat_binding_display_name(raw_kind, raw_text, display_by_key)
            if raw_display and raw_display not in binding_displays:
                binding_displays.append(raw_display)
        item["chat_display_name"] = display_name
        item["chat_display_names"] = binding_displays
        enriched[thread_id] = item
    return enriched


def _surface_kind_for_binding_id(
    binding_id: str, binding_metadata: dict[str, Any]
) -> Optional[str]:
    lowered = binding_id.lower()
    if lowered.startswith("discord:"):
        return "discord"
    if lowered.startswith("telegram:"):
        return "telegram"
    kinds = [
        str(kind).strip().lower()
        for kind in binding_metadata.get("binding_kinds") or []
        if str(kind).strip()
    ]
    if len(kinds) == 1:
        return kinds[0]
    return normalize_optional_text(binding_metadata.get("binding_kind"))


def _chat_binding_display_name(
    surface_kind: Optional[str],
    binding_id: Optional[str],
    display_by_key: dict[str, str],
) -> Optional[str]:
    if not surface_kind or not binding_id:
        return None
    for key in _chat_directory_keys_for_binding(surface_kind, binding_id):
        display = display_by_key.get(key)
        if display:
            return display
    return None


def _chat_directory_keys_for_binding(
    surface_kind: str, binding_id: str
) -> tuple[str, ...]:
    kind = surface_kind.strip().lower()
    raw = binding_id.strip()
    if not kind or not raw:
        return ()
    body = raw[len(kind) + 1 :] if raw.lower().startswith(f"{kind}:") else raw
    candidates = [f"{kind}:{body}", raw]
    if kind == "telegram":
        parts = body.split(":", 2)
        if len(parts) >= 2:
            chat_id, thread_id = parts[0].strip(), parts[1].strip()
            if chat_id and thread_id and thread_id != "root":
                candidates.append(f"telegram:{chat_id}:{thread_id}")
            if chat_id:
                candidates.append(f"telegram:{chat_id}")
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered)


def _apply_chat_binding_fields(
    payload: dict[str, Any],
    *,
    managed_thread_id: Optional[str],
    binding_metadata_by_thread: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    payload.update(_chat_binding_defaults())
    if not managed_thread_id:
        return payload
    binding_metadata = (binding_metadata_by_thread or {}).get(managed_thread_id, {})
    if not isinstance(binding_metadata, dict):
        return payload
    payload.update(
        {
            "chat_bound": bool(binding_metadata.get("chat_bound")),
            "binding_kind": normalize_optional_text(
                binding_metadata.get("binding_kind")
            ),
            "binding_id": normalize_optional_text(binding_metadata.get("binding_id")),
            "chat_display_name": normalize_optional_text(
                binding_metadata.get("chat_display_name")
            ),
            "binding_count": int(binding_metadata.get("binding_count") or 0),
            "binding_kinds": list(binding_metadata.get("binding_kinds") or []),
            "binding_ids": list(binding_metadata.get("binding_ids") or []),
            "chat_display_names": list(
                binding_metadata.get("chat_display_names") or []
            ),
            "cleanup_protected": bool(binding_metadata.get("cleanup_protected")),
            "retire_protected": bool(binding_metadata.get("cleanup_protected")),
        }
    )
    return payload


def _attach_latest_execution_fields(
    payload: dict[str, Any],
    *,
    service: Any,
    managed_thread_id: str,
    execution: Any = ...,
) -> dict[str, Any]:
    if execution is ...:
        execution = _resolve_running_or_latest_execution(service, managed_thread_id)
    if execution is None:
        payload.update(
            {
                "latest_turn_id": None,
                "latest_turn_status": None,
                "latest_turn_started_at": None,
                "latest_turn_finished_at": None,
                "last_activity_at": None,
                "latest_assistant_text": "",
                "latest_output_excerpt": "",
            }
        )
        return payload

    assistant_text = str(getattr(execution, "output_text", "") or "")
    started_at = normalize_optional_text(getattr(execution, "started_at", None))
    finished_at = normalize_optional_text(getattr(execution, "finished_at", None))
    payload.update(
        {
            "latest_turn_id": normalize_optional_text(
                getattr(execution, "execution_id", None)
            ),
            "latest_turn_status": normalize_optional_text(
                getattr(execution, "status", None)
            ),
            "latest_turn_started_at": started_at,
            "latest_turn_finished_at": finished_at,
            "last_activity_at": finished_at or started_at,
            "latest_assistant_text": assistant_text,
            "latest_output_excerpt": _truncate_text(assistant_text, 240),
        }
    )
    return payload


def _serialize_thread_target(
    thread: ThreadTarget,
    *,
    binding_metadata_by_thread: Optional[dict[str, dict[str, Any]]] = None,
    active_work_summary: Optional[ActiveWorkSummary] = None,
) -> dict[str, Any]:
    target_runtime_status = normalize_optional_text(thread.status)
    execution_status = (
        normalize_optional_text(active_work_summary.execution_status)
        if active_work_summary is not None
        else None
    )
    effective_status: str = (
        normalize_chat_effective_status(
            execution_status if execution_status in {"running", "queued"} else None
        )
        or normalize_chat_effective_status(target_runtime_status)
        or target_runtime_status
        or "idle"
    )
    payload = {
        "managed_thread_id": thread.thread_target_id,
        "agent": thread.agent_id,
        "agent_profile": normalize_optional_text(thread.agent_profile),
        "repo_id": thread.repo_id,
        "resource_kind": thread.resource_kind,
        "resource_id": thread.resource_id,
        "workspace_root": thread.workspace_root,
        "name": thread.display_name,
        "model": normalize_optional_text(getattr(thread, "model", None)),
        "backend_thread_id": thread.backend_thread_id,
        "lifecycle_status": thread.lifecycle_status,
        "runtime_status": effective_status,
        "normalized_status": effective_status,
        "status": effective_status,
        "target_runtime_status": target_runtime_status,
        "execution_status": execution_status,
        "active_turn_id": (
            active_work_summary.execution_id
            if active_work_summary is not None
            else None
        ),
        "queued_count": (
            active_work_summary.queued_count if active_work_summary is not None else 0
        ),
        "status_reason": thread.status_reason,
        "status_changed_at": thread.status_changed_at,
        "status_terminal": bool(thread.status_terminal),
        "status_turn_id": thread.status_turn_id,
        "last_turn_id": thread.last_execution_id,
        "last_message_preview": thread.last_message_preview,
        "compact_seed": thread.compact_seed,
        "context_profile": normalize_car_context_profile(
            thread.context_profile,
            default=default_managed_thread_context_profile(),
        ),
        "approval_mode": normalize_approval_mode(thread.approval_mode, default="yolo"),
        "accepts_messages": thread.lifecycle_status == "active",
    }
    payload.update(
        _ticket_flow_thread_fields(
            metadata=dict(thread.metadata or {}),
            workspace_root=thread.workspace_root,
            display_name=thread.display_name,
        )
    )
    payload["chat_kind"] = infer_managed_thread_chat_kind(
        metadata=dict(thread.metadata or {}),
        display_name=thread.display_name,
    )
    updated_at_value = normalize_optional_text(thread.updated_at)
    if not updated_at_value:
        updated_at_value = normalize_optional_text(thread.status_changed_at)
    if not updated_at_value:
        updated_at_value = normalize_optional_text(thread.created_at)
    payload["updated_at"] = updated_at_value
    payload["created_at"] = normalize_optional_text(thread.created_at)
    payload.update(
        _build_operator_status_fields(
            normalized_status=target_runtime_status,
            lifecycle_status=thread.lifecycle_status,
        )
    )
    return _apply_chat_binding_fields(
        payload,
        managed_thread_id=thread.thread_target_id,
        binding_metadata_by_thread=binding_metadata_by_thread,
    )


def resolve_managed_thread_list_query(
    *,
    agent: Optional[str],
    status: Optional[str],
    lifecycle_status: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    limit: int,
) -> ManagedThreadListQuery:
    if limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be greater than 0")
    normalized_status = normalize_optional_text(status)
    normalized_lifecycle_status = normalize_optional_text(lifecycle_status)
    if (
        normalized_status in THREAD_TARGET_LIST_LIFECYCLE_STATUSES
        and normalized_lifecycle_status is None
    ):
        normalized_lifecycle_status = normalized_status
        normalized_status = None
    normalized_resource_kind, normalized_resource_id, normalized_repo_id = (
        _normalize_resource_owner(
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
    )
    return ManagedThreadListQuery(
        agent_id=normalize_optional_text(agent),
        lifecycle_status=normalized_lifecycle_status,
        runtime_status=normalized_status,
        repo_id=normalized_repo_id,
        resource_kind=normalized_resource_kind,
        resource_id=normalized_resource_id,
        limit=limit,
    )


def resolve_owner_scoped_query(
    *,
    agent: Optional[str] = None,
    resource_kind: Optional[str],
    resource_id: Optional[str],
    limit: int,
) -> ManagedThreadOwnerScopedQuery:
    if limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be greater than 0")
    normalized_resource_kind, normalized_resource_id, normalized_repo_id = (
        _normalize_resource_owner(
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
    )
    return ManagedThreadOwnerScopedQuery(
        agent_id=normalize_optional_text(agent),
        repo_id=normalized_repo_id,
        resource_kind=normalized_resource_kind,
        resource_id=normalized_resource_id,
        limit=limit,
    )


def serialize_managed_thread_turn_summary(turn: dict[str, Any]) -> dict[str, Any]:
    metadata = turn.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        attachments = []
    return {
        "managed_turn_id": turn.get("managed_turn_id"),
        "managed_thread_id": turn.get("managed_thread_id"),
        "request_kind": turn.get("request_kind"),
        "status": turn.get("status"),
        "prompt": turn.get("prompt") or "",
        "prompt_preview": _truncate_text(turn.get("prompt") or "", 120),
        "assistant_preview": _truncate_text(turn.get("assistant_text") or "", 120),
        "assistant_text": turn.get("assistant_text") or "",
        "attachments": [item for item in attachments if isinstance(item, dict)],
        "started_at": turn.get("started_at"),
        "finished_at": turn.get("finished_at"),
        "error": turn.get("error"),
    }


def serialize_binding_record(binding: Binding) -> dict[str, Any]:
    return {
        "binding_id": binding.binding_id,
        "surface_kind": binding.surface_kind,
        "surface_key": binding.surface_key,
        "thread_target_id": binding.thread_target_id,
        "agent_id": binding.agent_id,
        "repo_id": binding.repo_id,
        "resource_kind": binding.resource_kind,
        "resource_id": binding.resource_id,
        "mode": binding.mode,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
        "disabled_at": binding.disabled_at,
    }


def serialize_active_work_summary(summary: ActiveWorkSummary) -> dict[str, Any]:
    return {
        "thread_target_id": summary.thread_target_id,
        "agent_id": summary.agent_id,
        "repo_id": summary.repo_id,
        "resource_kind": summary.resource_kind,
        "resource_id": summary.resource_id,
        "workspace_root": summary.workspace_root,
        "display_name": summary.display_name,
        "lifecycle_status": summary.lifecycle_status,
        "runtime_status": summary.runtime_status,
        "execution_id": summary.execution_id,
        "execution_status": summary.execution_status,
        "queued_count": summary.queued_count,
        "message_preview": summary.message_preview,
        "binding_count": summary.binding_count,
        "surface_kinds": list(summary.surface_kinds),
    }


__all__ = [
    "ManagedThreadCreateResolution",
    "ManagedThreadListQuery",
    "ManagedThreadOwnerScopedQuery",
    "ManagedThreadWorkspaceProvision",
    "_attach_latest_execution_fields",
    "_load_chat_binding_metadata_by_thread",
    "_normalize_resource_owner",
    "_normalize_workspace_root_input",
    "_serialize_managed_thread",
    "_serialize_thread_target",
    "managed_thread_metadata_for_provisioned_workspace",
    "provision_managed_thread_workspace",
    "resolve_managed_thread_create_resolution",
    "resolve_managed_thread_list_query",
    "resolve_owner_scoped_query",
    "serialize_active_work_summary",
    "serialize_binding_record",
    "serialize_managed_thread_turn_summary",
]
