from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from fastapi import HTTPException, Request

from .....agents.registry import resolve_agent_runtime, validate_agent_id
from .....core.car_context import (
    default_managed_thread_context_profile,
    normalize_car_context_profile,
)
from .....core.chat_bindings import active_chat_binding_metadata_by_thread
from .....core.hub_control_plane.models import THREAD_TARGET_LIST_LIFECYCLE_STATUSES
from .....core.managed_thread_status import derive_managed_thread_operator_status
from .....core.orchestration import ActiveWorkSummary
from .....core.orchestration.models import Binding, ThreadTarget
from .....core.text_utils import _truncate_text
from .....integrations.chat.approval_modes import normalize_approval_mode
from ...schemas import PmaManagedThreadCreateRequest
from ...services.pma import get_pma_request_context
from ...services.pma.managed_thread_followup import (
    ManagedThreadFollowupPolicy,
    resolve_managed_thread_followup_policy,
)
from .automation_adapter import normalize_optional_text

_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedThreadCreateResolution:
    agent_id: str
    workspace_root: Path
    repo_id: Optional[str]
    resource_kind: Optional[str]
    resource_id: Optional[str]
    requested_profile: Optional[str]
    metadata: dict[str, Any]
    followup_policy: ManagedThreadFollowupPolicy


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


def _resolve_workspace_from_repo_id(request: Request, repo_id: str) -> Path:
    snapshot = _resolve_repo_snapshot(request, repo_id)
    repo_path = getattr(snapshot, "path", None)
    if isinstance(repo_path, str):
        repo_path = Path(repo_path)
    if isinstance(repo_path, Path):
        return repo_path.absolute()
    raise HTTPException(status_code=404, detail=f"Repo not found: {repo_id}")


def _resolve_repo_snapshot(request: Request, repo_id: str) -> Any:
    supervisor = get_pma_request_context(request).hub_supervisor
    if supervisor is None:
        raise HTTPException(status_code=500, detail="Hub supervisor unavailable")
    for snapshot in supervisor.list_repos():
        if getattr(snapshot, "id", None) != repo_id:
            continue
        return snapshot
    raise HTTPException(status_code=404, detail=f"Repo not found: {repo_id}")


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


def _resolve_workspace_from_resource_owner(
    request: Request,
    *,
    resource_kind: str,
    resource_id: str,
) -> tuple[Path, Optional[str], Optional[str]]:
    supervisor = get_pma_request_context(request).hub_supervisor
    if supervisor is None:
        raise HTTPException(status_code=500, detail="Hub supervisor unavailable")
    if resource_kind == "repo":
        return _resolve_workspace_from_repo_id(request, resource_id), resource_id, None
    if resource_kind == "agent_workspace":
        for snapshot in supervisor.list_agent_workspaces():
            if getattr(snapshot, "id", None) != resource_id:
                continue
            workspace_path = getattr(snapshot, "path", None)
            if isinstance(workspace_path, str):
                workspace_path = Path(workspace_path)
            if isinstance(workspace_path, Path):
                runtime = getattr(snapshot, "runtime", None)
                normalized_runtime = (
                    str(runtime).strip().lower()
                    if isinstance(runtime, str) and runtime.strip()
                    else None
                )
                return workspace_path.absolute(), None, normalized_runtime
        raise HTTPException(
            status_code=404,
            detail=f"Agent workspace not found: {resource_id}",
        )
    raise HTTPException(status_code=400, detail="resource_kind is invalid")


def _resolve_workspace_from_input(hub_root: Path, workspace_root: str) -> Path:
    normalized = _normalize_workspace_root_input(workspace_root)
    hub_root_resolved = hub_root.absolute()
    workspace = Path(normalized)
    if not workspace.is_absolute():
        workspace = (hub_root_resolved / workspace).absolute()
    else:
        workspace = workspace.absolute()
    if not _is_within_root(workspace, hub_root):
        raise HTTPException(status_code=400, detail="workspace_root is invalid")
    return workspace


def _normalize_resource_owner(
    *,
    resource_kind: Optional[str],
    resource_id: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    normalized_resource_kind = normalize_optional_text(resource_kind)
    normalized_resource_id = normalize_optional_text(resource_id)
    if normalized_resource_id and normalized_resource_kind is None:
        raise HTTPException(
            status_code=400,
            detail="resource_kind is required when resource_id is provided",
        )
    if normalized_resource_kind and normalized_resource_id is None:
        raise HTTPException(
            status_code=400,
            detail="resource_id is required when resource_kind is provided",
        )
    if normalized_resource_kind not in {None, "repo", "agent_workspace"}:
        raise HTTPException(
            status_code=400,
            detail="resource_kind must be one of: repo, agent_workspace",
        )
    normalized_repo_id = (
        normalized_resource_id if normalized_resource_kind == "repo" else None
    )
    return normalized_resource_kind, normalized_resource_id, normalized_repo_id


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
        default=default_managed_thread_context_profile(
            resource_kind=payload["resource_kind"]
        ),
    )
    payload["approval_mode"] = normalize_approval_mode(
        metadata.get("approval_mode"),
        default="yolo",
    )
    payload["agent_profile"] = normalize_optional_text(
        thread.get("agent_profile") or metadata.get("agent_profile")
    )
    payload.update(
        _build_operator_status_fields(
            normalized_status=payload["normalized_status"],
            lifecycle_status=lifecycle_status,
        )
    )
    return payload


def _chat_binding_defaults() -> dict[str, Any]:
    return {
        "chat_bound": False,
        "binding_kind": None,
        "binding_id": None,
        "binding_count": 0,
        "binding_kinds": [],
        "binding_ids": [],
        "cleanup_protected": False,
    }


def _load_chat_binding_metadata_by_thread(hub_root: Path) -> dict[str, dict[str, Any]]:
    try:
        return active_chat_binding_metadata_by_thread(hub_root=hub_root)
    except Exception as exc:  # intentional: non-critical metadata load
        _logger.warning(
            "Could not load PMA chat-binding metadata for thread response: %s", exc
        )
        return {}


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
            "binding_count": int(binding_metadata.get("binding_count") or 0),
            "binding_kinds": list(binding_metadata.get("binding_kinds") or []),
            "binding_ids": list(binding_metadata.get("binding_ids") or []),
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
) -> dict[str, Any]:
    payload = {
        "managed_thread_id": thread.thread_target_id,
        "agent": thread.agent_id,
        "agent_profile": normalize_optional_text(thread.agent_profile),
        "repo_id": thread.repo_id,
        "resource_kind": thread.resource_kind,
        "resource_id": thread.resource_id,
        "workspace_root": thread.workspace_root,
        "name": thread.display_name,
        "backend_thread_id": thread.backend_thread_id,
        "lifecycle_status": thread.lifecycle_status,
        "normalized_status": thread.status,
        "status": thread.status,
        "status_reason": thread.status_reason,
        "status_changed_at": thread.status_changed_at,
        "status_terminal": bool(thread.status_terminal),
        "status_turn_id": thread.status_turn_id,
        "last_turn_id": thread.last_execution_id,
        "last_message_preview": thread.last_message_preview,
        "compact_seed": thread.compact_seed,
        "context_profile": normalize_car_context_profile(
            thread.context_profile,
            default=default_managed_thread_context_profile(
                resource_kind=thread.resource_kind
            ),
        ),
        "approval_mode": normalize_approval_mode(thread.approval_mode, default="yolo"),
        "accepts_messages": thread.lifecycle_status == "active",
    }
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


def _raise_agent_workspace_runtime_not_ready(
    request: Request, resource_id: str
) -> None:
    supervisor = get_pma_request_context(request).hub_supervisor
    if supervisor is None:
        raise HTTPException(status_code=500, detail="Hub supervisor unavailable")
    snapshot = supervisor.get_agent_workspace_snapshot(resource_id)
    if not snapshot.enabled:
        raise HTTPException(
            status_code=400,
            detail=f"Agent workspace '{resource_id}' is disabled",
        )
    readiness = supervisor.get_agent_workspace_runtime_readiness(resource_id)
    if not isinstance(readiness, dict):
        return
    status = str(readiness.get("status") or "").strip().lower()
    if status in {"", "ready", "deferred"}:
        return
    message = str(readiness.get("message") or "").strip()
    fix = str(readiness.get("fix") or "").strip()
    detail = message or f"Agent workspace runtime '{snapshot.runtime}' is not ready"
    if fix:
        detail = f"{detail} Fix: {fix}"
    raise HTTPException(status_code=400, detail=detail)


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
            from .....integrations.chat.agents import chat_hermes_profile_options

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
    payload: PmaManagedThreadCreateRequest,
) -> ManagedThreadCreateResolution:
    context = get_pma_request_context(request)
    hub_root = context.hub_root
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
    resource_kind, resource_id, resolved_repo_id = _normalize_resource_owner(
        resource_kind=payload.resource_kind,
        resource_id=payload.resource_id,
    )
    workspace_root = normalize_optional_text(payload.workspace_root)
    followup_policy = resolve_managed_thread_followup_policy(
        payload,
        # Terminal follow-up defaults now apply when the thread is used, not at
        # spawn time, so create-thread only honors explicit follow-up intent.
        default_terminal_followup=False,
    )

    owner_present = resource_kind is not None and resource_id is not None
    if owner_present == bool(workspace_root):
        raise HTTPException(
            status_code=400,
            detail="Exactly one of resource owner or workspace_root is required",
        )

    resolved_runtime: Optional[str] = None
    if owner_present:
        assert resource_kind is not None
        assert resource_id is not None
        resolved_workspace, resolved_repo_id, resolved_runtime = (
            _resolve_workspace_from_resource_owner(
                request,
                resource_kind=resource_kind,
                resource_id=resource_id,
            )
        )
        if not _is_within_root(resolved_workspace, hub_root):
            raise HTTPException(
                status_code=400, detail="Resolved resource path is invalid"
            )
    else:
        if workspace_root is None:
            raise HTTPException(
                status_code=400,
                detail="workspace_root is required when resource owner is omitted",
            )
        resolved_workspace = _resolve_workspace_from_input(hub_root, workspace_root)

    if resource_kind == "agent_workspace":
        if resolved_runtime is None:
            raise HTTPException(
                status_code=400,
                detail="Agent workspace runtime is unavailable",
            )
        if agent_id is None:
            agent_id = resolved_runtime
        elif agent_id != resolved_runtime:
            raise HTTPException(
                status_code=400,
                detail=(
                    "agent must match the agent workspace runtime "
                    f"('{resolved_runtime}')"
                ),
            )
        assert resource_id is not None
        _raise_agent_workspace_runtime_not_ready(request, resource_id)

    if agent_id is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "agent is required unless an agent workspace owner supplies "
                "the runtime"
            ),
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
        default=default_managed_thread_context_profile(resource_kind=resource_kind),
    )
    if context_profile is None:
        raise HTTPException(status_code=400, detail="context_profile is invalid")
    approval_mode = normalize_approval_mode(payload.approval_mode, default="yolo")
    metadata: dict[str, Any] = {
        "context_profile": context_profile,
        "approval_mode": approval_mode,
    }
    if requested_profile is not None:
        metadata["agent_profile"] = requested_profile

    return ManagedThreadCreateResolution(
        agent_id=agent_id,
        workspace_root=resolved_workspace,
        repo_id=resolved_repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        requested_profile=requested_profile,
        metadata=metadata,
        followup_policy=followup_policy,
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
        return fallback

    try:
        snapshot = _resolve_repo_snapshot(request, resolution.repo_id)
    except HTTPException:
        return fallback
    if normalize_optional_text(getattr(snapshot, "kind", None)) != "base":
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
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return fallback

    worktree_path = getattr(created, "path", None)
    if isinstance(worktree_path, str):
        worktree_path = Path(worktree_path)
    if not isinstance(worktree_path, Path):
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
    return {
        "managed_turn_id": turn.get("managed_turn_id"),
        "request_kind": turn.get("request_kind"),
        "status": turn.get("status"),
        "prompt_preview": _truncate_text(turn.get("prompt") or "", 120),
        "assistant_preview": _truncate_text(turn.get("assistant_text") or "", 120),
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
