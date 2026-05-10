from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from fastapi import HTTPException, Request

from .....adapters.chat.approval_modes import normalize_approval_mode
from .....adapters.chat.channel_directory import (
    ChannelDirectoryStore,
    channel_entry_key,
)
from .....adapters.chat.pma_context_selection import (
    PmaContextSelectionError,
    normalize_pma_resource_owner,
    resolve_pma_context_selection,
)
from .....agents.registry import resolve_agent_runtime, validate_agent_id
from .....core.car_context import (
    default_managed_thread_context_profile,
    normalize_car_context_profile,
)
from .....core.chat_bindings import active_chat_binding_metadata_by_thread
from .....core.domain.refs import ScopeRef, ScopeRefError
from .....core.hub_control_plane.models import THREAD_TARGET_LIST_LIFECYCLE_STATUSES
from .....core.managed_thread_status import derive_managed_thread_operator_status
from .....core.orchestration import ActiveWorkSummary
from .....core.orchestration.models import Binding, ThreadTarget
from .....core.text_utils import _truncate_text
from .....tickets.files import ticket_is_done
from ...schemas import ManagedThreadCreateRequest
from ...services.pma import get_pma_request_context
from ...services.pma.managed_thread_followup import (
    ManagedThreadFollowupPolicy,
    resolve_managed_thread_followup_policy,
)
from .automation_adapter import normalize_optional_text

_logger = logging.getLogger(__name__)
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ManagedThreadCreateResolution:
    agent_id: str
    workspace_root: Path
    repo_id: Optional[str]
    resource_kind: Optional[str]
    resource_id: Optional[str]
    scope: Optional[ScopeRef]
    requested_profile: Optional[str]
    metadata: dict[str, Any]
    followup_policy: ManagedThreadFollowupPolicy
    pr_mode: bool = False
    pr_base_ref: Optional[str] = None


@dataclass(frozen=True)
class ManagedThreadWorkspaceProvision:
    workspace_root: Path
    worktree_repo_id: Optional[str] = None


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


def _is_within_root(path: Path, root: Path) -> bool:
    from .....core.state_roots import is_within_allowed_root

    return is_within_allowed_root(path, allowed_roots=[root], resolve=True)


def _resolve_repo_snapshot(request: Request, repo_id: str) -> Any:
    supervisor = get_pma_request_context(request).hub_supervisor
    if supervisor is None:
        raise HTTPException(status_code=500, detail="Hub supervisor unavailable")
    for snapshot in supervisor.list_repos():
        if getattr(snapshot, "id", None) != repo_id:
            continue
        return snapshot
    raise HTTPException(status_code=404, detail=f"Repo not found: {repo_id}")


def _normalize_workspace_root_input(workspace_root: str) -> PurePosixPath:
    cleaned = (workspace_root or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="workspace_root is invalid")
    if "\\" in cleaned or "\x00" in cleaned or _DRIVE_PREFIX_RE.match(cleaned):
        raise HTTPException(status_code=400, detail="workspace_root is invalid")
    normalized = PurePosixPath(cleaned)
    if ".." in normalized.parts:
        raise HTTPException(status_code=400, detail="workspace_root is invalid")
    return normalized


def _resolve_pr_upstream_repo_id(request: Request, repo_id: str) -> str:
    snapshot = _resolve_repo_snapshot(request, repo_id)
    kind = normalize_optional_text(getattr(snapshot, "kind", None))
    if kind == "base":
        return repo_id
    if kind == "worktree":
        base_repo_id = normalize_optional_text(getattr(snapshot, "worktree_of", None))
        if base_repo_id is None:
            raise HTTPException(
                status_code=400,
                detail="PR mode requires a worktree with worktree_of metadata",
            )
        base_snapshot = _resolve_repo_snapshot(request, base_repo_id)
        if normalize_optional_text(getattr(base_snapshot, "kind", None)) != "base":
            raise HTTPException(
                status_code=400,
                detail=f"PR upstream repo is not a base repo: {base_repo_id}",
            )
        return base_repo_id
    raise HTTPException(
        status_code=400,
        detail="PR mode requires a base repo or hub-managed worktree repo",
    )


def _slugify_worktree_branch_component(value: Any) -> str:
    text = str(value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-")
    return cleaned or "thread"


def _build_managed_thread_worktree_branch_name(
    *,
    repo_id: str,
    agent_id: str,
    display_name: Optional[str],
) -> str:
    label = _slugify_worktree_branch_component(display_name or agent_id or repo_id)
    return (
        f"pma/{_slugify_worktree_branch_component(repo_id)}/"
        f"{label}-{uuid.uuid4().hex[:10]}"
    )


def _normalize_resource_owner(
    *,
    resource_kind: Optional[str],
    resource_id: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    try:
        owner = normalize_pma_resource_owner(
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
    except PmaContextSelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return owner.resource_kind, owner.resource_id, owner.repo_id


def _scope_ref_from_payload(
    payload: ManagedThreadCreateRequest,
) -> Optional[ScopeRef]:
    scope_urn = normalize_optional_text(payload.scope_urn)
    if scope_urn is None:
        return None
    if any(
        normalize_optional_text(value) is not None
        for value in (
            payload.resource_kind,
            payload.resource_id,
            payload.repo_id,
            payload.workspace_root,
        )
    ):
        raise HTTPException(
            status_code=400,
            detail="scope_urn cannot be combined with legacy owner fields",
        )
    try:
        return ScopeRef.from_urn(scope_urn)
    except (ScopeRefError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        }
    )
    return payload


def _attach_latest_execution_fields(
    payload: dict[str, Any],
    *,
    service: Any,
    managed_thread_id: str,
) -> dict[str, Any]:
    get_running_execution = getattr(service, "get_running_execution", None)
    get_latest_execution = getattr(service, "get_latest_execution", None)
    execution = None
    if callable(get_running_execution):
        execution = get_running_execution(managed_thread_id)
    if execution is None and callable(get_latest_execution):
        execution = get_latest_execution(managed_thread_id)
    if execution is None:
        payload.update(
            {
                "latest_turn_id": None,
                "latest_turn_status": None,
                "latest_assistant_text": "",
                "latest_output_excerpt": "",
            }
        )
        return payload

    assistant_text = str(getattr(execution, "output_text", "") or "")
    payload.update(
        {
            "latest_turn_id": normalize_optional_text(
                getattr(execution, "execution_id", None)
            ),
            "latest_turn_status": normalize_optional_text(
                getattr(execution, "status", None)
            ),
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
    effective_status = (
        execution_status
        if execution_status in {"running", "queued"}
        else target_runtime_status
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
    updated_at_value = normalize_optional_text(thread.updated_at)
    if not updated_at_value:
        updated_at_value = normalize_optional_text(thread.status_changed_at)
    if not updated_at_value:
        updated_at_value = normalize_optional_text(thread.created_at)
    payload["updated_at"] = updated_at_value
    payload["created_at"] = normalize_optional_text(thread.created_at)
    payload.update(
        _build_operator_status_fields(
            normalized_status=thread.status,
            lifecycle_status=thread.lifecycle_status,
        )
    )
    return _apply_chat_binding_fields(
        payload,
        managed_thread_id=thread.thread_target_id,
        binding_metadata_by_thread=binding_metadata_by_thread,
    )


def _resolve_requested_profile(
    request: Request,
    *,
    agent_id: str,
    requested_profile: Optional[str],
) -> Optional[str]:
    context = get_pma_request_context(request)
    config = context.config
    profile_getter = getattr(config, "agent_profiles", None)
    default_profile_getter = getattr(config, "agent_default_profile", None)
    available_profiles: dict[str, Any] = {}
    if callable(profile_getter):
        try:
            available_profiles = profile_getter(agent_id) or {}
        except (ValueError, TypeError):
            available_profiles = {}
    if requested_profile is None and callable(default_profile_getter):
        try:
            requested_profile = normalize_optional_text(
                default_profile_getter(agent_id)
            )
        except (ValueError, TypeError):
            requested_profile = None
    valid_profiles = set(available_profiles.keys())
    if agent_id == "hermes":
        try:
            from .....adapters.chat.agents import chat_hermes_profile_options

            valid_profiles |= {
                opt.profile
                for opt in chat_hermes_profile_options(context.agent_context)
            }
        except Exception:  # intentional: optional hermes integration
            _logger.debug(
                "Failed to resolve hermes profile options for managed thread",
                exc_info=True,
            )
    if requested_profile is not None and requested_profile not in valid_profiles:
        resolved = resolve_agent_runtime(
            agent_id,
            requested_profile,
            context=context.agent_context,
        )
        if (
            resolved.logical_agent_id != agent_id
            or resolved.logical_profile != requested_profile
            or resolved.resolution_kind == "passthrough"
        ):
            raise HTTPException(status_code=400, detail="profile is invalid")
    return requested_profile


def resolve_managed_thread_create_resolution(
    request: Request,
    payload: ManagedThreadCreateRequest,
) -> ManagedThreadCreateResolution:
    context = get_pma_request_context(request)
    hub_root = context.hub_root
    scope_ref = _scope_ref_from_payload(payload)
    scope_workspace_root: Optional[str] = None
    scope_resource_kind: Optional[str] = None
    scope_resource_id: Optional[str] = None
    if scope_ref is not None:
        if scope_ref.kind == "filesystem":
            scope_workspace_root = scope_ref.path
        elif scope_ref.kind != "hub":
            scope_resource_kind = scope_ref.kind
            scope_resource_id = scope_ref.id
    workspace_text = normalize_optional_text(
        scope_workspace_root
        if scope_workspace_root is not None
        else payload.workspace_root
    )
    pr_base_ref = normalize_optional_text(payload.pr_base_ref)
    owner = normalize_pma_resource_owner(
        resource_kind=scope_resource_kind or payload.resource_kind,
        resource_id=scope_resource_id or payload.resource_id,
        repo_id=payload.repo_id,
    )
    if workspace_text is not None and owner.resource_kind is not None:
        raise HTTPException(
            status_code=400,
            detail="Exactly one of resource owner or workspace_root is required",
        )
    if pr_base_ref is not None and not payload.pr_mode:
        raise HTTPException(
            status_code=400,
            detail="pr_base_ref requires PR mode",
        )
    if payload.pr_mode and owner.resource_kind not in {"repo", "worktree"}:
        raise HTTPException(
            status_code=400,
            detail="PR mode requires a repo or worktree resource owner",
        )
    if workspace_text is not None and workspace_text != ".":
        _normalize_workspace_root_input(workspace_text)
    raw_agent_id = normalize_optional_text(payload.agent)
    raw_profile = normalize_optional_text(payload.profile)
    if raw_agent_id is not None:
        try:
            validate_agent_id(raw_agent_id, context.agent_context)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    runtime_resolution = (
        resolve_agent_runtime(raw_agent_id, raw_profile, context=context.agent_context)
        if raw_agent_id is not None
        else None
    )
    agent_id = (
        runtime_resolution.logical_agent_id if runtime_resolution is not None else None
    )
    supervisor = context.hub_supervisor
    repos = supervisor.list_repos() if supervisor is not None else ()
    resource_id_for_ctx = owner.resource_id
    if payload.pr_mode:
        if not resource_id_for_ctx:
            raise HTTPException(
                status_code=400,
                detail="PR mode requires a repo resource owner",
            )
        resource_id_for_ctx = _resolve_pr_upstream_repo_id(request, resource_id_for_ctx)
    try:
        pma_context = resolve_pma_context_selection(
            hub_root=hub_root,
            workspace_root=workspace_text,
            resource_kind=owner.resource_kind,
            resource_id=resource_id_for_ctx,
            repo_id=payload.repo_id,
            repos=repos,
        )
    except PmaContextSelectionError as exc:
        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    resource_kind = pma_context.resource_kind
    resource_id = pma_context.resource_id
    resolved_repo_id = pma_context.repo_id
    followup_policy = resolve_managed_thread_followup_policy(
        payload,
        # Terminal follow-up defaults now apply when the thread is used, not at
        # spawn time, so create-thread only honors explicit follow-up intent.
        default_terminal_followup=False,
    )

    resolved_workspace = pma_context.workspace_root
    if not _is_within_root(resolved_workspace, hub_root):
        raise HTTPException(status_code=400, detail="Resolved resource path is invalid")

    if agent_id is None:
        raise HTTPException(
            status_code=400,
            detail="agent is required",
        )

    requested_profile = _resolve_requested_profile(
        request,
        agent_id=agent_id,
        requested_profile=(
            runtime_resolution.logical_profile
            if runtime_resolution is not None
            else raw_profile
        ),
    )
    context_profile = normalize_car_context_profile(
        payload.context_profile,
        default=pma_context.context_profile,
    )
    if context_profile is None:
        raise HTTPException(status_code=400, detail="context_profile is invalid")
    approval_mode = normalize_approval_mode(payload.approval_mode, default="yolo")
    metadata: dict[str, Any] = {
        "context_profile": context_profile,
        "approval_mode": approval_mode,
    }
    preferred_model = normalize_optional_text(payload.model)
    if preferred_model is not None:
        metadata["model"] = preferred_model
    if payload.pr_mode:
        metadata["pr_mode"] = True
        if pr_base_ref is not None:
            metadata["pr_base_ref"] = pr_base_ref
    if requested_profile is not None:
        metadata["agent_profile"] = requested_profile

    return ManagedThreadCreateResolution(
        agent_id=agent_id,
        workspace_root=resolved_workspace,
        repo_id=resolved_repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        scope=scope_ref,
        requested_profile=requested_profile,
        metadata=metadata,
        followup_policy=followup_policy,
        pr_mode=bool(payload.pr_mode),
        pr_base_ref=pr_base_ref,
    )


def provision_managed_thread_workspace(
    request: Request,
    *,
    resolution: ManagedThreadCreateResolution,
    display_name: Optional[str] = None,
) -> ManagedThreadWorkspaceProvision:
    fallback = ManagedThreadWorkspaceProvision(workspace_root=resolution.workspace_root)
    if resolution.resource_kind != "repo" or resolution.repo_id is None:
        return fallback

    context = get_pma_request_context(request)
    supervisor = context.hub_supervisor
    if supervisor is None:
        if resolution.pr_mode:
            raise HTTPException(status_code=500, detail="Hub supervisor unavailable")
        return fallback

    try:
        snapshot = _resolve_repo_snapshot(request, resolution.repo_id)
    except HTTPException:
        if resolution.pr_mode:
            raise
        return fallback
    if normalize_optional_text(getattr(snapshot, "kind", None)) != "base":
        if resolution.pr_mode:
            raise HTTPException(
                status_code=400,
                detail="PR mode requires a base repo",
            )
        return fallback

    branch_name = _build_managed_thread_worktree_branch_name(
        repo_id=resolution.repo_id,
        agent_id=resolution.agent_id,
        display_name=display_name,
    )
    try:
        created = supervisor.create_worktree(
            base_repo_id=resolution.repo_id,
            branch=branch_name,
            start_point=resolution.pr_base_ref,
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        if resolution.pr_mode:
            raise HTTPException(
                status_code=409,
                detail=f"Unable to provision PR worktree: {exc}",
            ) from exc
        return fallback

    worktree_path = getattr(created, "path", None)
    if isinstance(worktree_path, str):
        worktree_path = Path(worktree_path)
    if not isinstance(worktree_path, Path):
        if resolution.pr_mode:
            raise HTTPException(
                status_code=409,
                detail="Unable to provision PR worktree: missing worktree path",
            )
        return fallback
    return ManagedThreadWorkspaceProvision(
        workspace_root=worktree_path.absolute(),
        worktree_repo_id=normalize_optional_text(getattr(created, "id", None)),
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
    "provision_managed_thread_workspace",
    "resolve_managed_thread_create_resolution",
    "resolve_managed_thread_list_query",
    "resolve_owner_scoped_query",
    "serialize_active_work_summary",
    "serialize_binding_record",
    "serialize_managed_thread_turn_summary",
]
