from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from fastapi import HTTPException, Request

from .....adapters.chat.approval_modes import normalize_approval_mode
from .....adapters.chat.pma_context_selection import (
    PmaContextSelectionError,
    normalize_pma_resource_owner,
    resolve_pma_context_selection,
)
from .....agents.registry import resolve_agent_runtime, validate_agent_id
from .....core.car_context import normalize_car_context_profile
from .....core.domain.refs import ScopeRef, ScopeRefError
from .....core.managed_thread_kinds import normalize_managed_thread_chat_kind
from ...schemas import ManagedThreadCreateRequest
from ...services.pma import get_pma_request_context
from ...services.pma.common import normalize_optional_text
from ...services.pma.managed_thread_followup import (
    ManagedThreadFollowupPolicy,
    resolve_managed_thread_followup_policy,
)

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


def _validate_legacy_scope_field_consistency(
    payload: ManagedThreadCreateRequest,
) -> None:
    workspace_root = normalize_optional_text(payload.workspace_root)
    repo_id = normalize_optional_text(payload.repo_id)
    resource_kind = normalize_optional_text(payload.resource_kind)
    resource_id = normalize_optional_text(payload.resource_id)
    if workspace_root is not None and any((repo_id, resource_kind, resource_id)):
        raise HTTPException(
            status_code=400,
            detail="workspace_root cannot be combined with legacy owner fields",
        )
    if repo_id is None:
        return
    if resource_kind is None and resource_id is None:
        return
    if resource_kind != "repo" or resource_id != repo_id:
        raise HTTPException(
            status_code=400,
            detail="repo_id conflicts with resource_kind/resource_id",
        )


def _scope_urn_for_create_resolution(
    *,
    scope_ref: Optional[ScopeRef],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    worktree_parent_repo_id: Optional[str],
    workspace_root: Path,
    hub_root: Path,
) -> str:
    if scope_ref is not None:
        return scope_ref.to_urn()
    if resource_kind == "repo" and resource_id is not None:
        return ScopeRef(kind="repo", id=resource_id).to_urn()
    if (
        resource_kind == "worktree"
        and resource_id is not None
        and worktree_parent_repo_id is not None
    ):
        return ScopeRef(
            kind="worktree",
            id=resource_id,
            parent_repo_id=worktree_parent_repo_id,
        ).to_urn()
    try:
        if workspace_root.resolve() == hub_root.resolve():
            return ScopeRef(kind="hub").to_urn()
    except OSError:
        pass
    return ScopeRef(kind="filesystem", path=str(workspace_root)).to_urn()


def _create_genesis_metadata(
    *,
    payload: ManagedThreadCreateRequest,
    scope_ref: Optional[ScopeRef],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    repo_id: Optional[str],
    worktree_parent_repo_id: Optional[str],
    workspace_root: Path,
    hub_root: Path,
) -> dict[str, Any]:
    genesis_provided = any(
        value is not None
        for value in (
            payload.genesis,
            normalize_optional_text(payload.origin),
            normalize_optional_text(payload.scope_source),
            normalize_optional_text(payload.parent_thread_id),
            normalize_optional_text(payload.fork_mode),
            payload.client_intent,
        )
    )
    if genesis_provided:
        origin = normalize_optional_text(payload.origin) or "unknown"
        scope_source = normalize_optional_text(payload.scope_source) or "unspecified"
    else:
        origin = "legacy"
        if normalize_optional_text(payload.scope_urn) is not None:
            scope_source = "legacy_scope_urn"
        elif any(
            normalize_optional_text(value) is not None
            for value in (
                payload.workspace_root,
                payload.repo_id,
                payload.resource_kind,
                payload.resource_id,
            )
        ):
            scope_source = "legacy_scope_fields"
        else:
            scope_source = "legacy_default_hub"

    scope_urn = _scope_urn_for_create_resolution(
        scope_ref=scope_ref,
        resource_kind=resource_kind,
        resource_id=resource_id,
        worktree_parent_repo_id=worktree_parent_repo_id,
        workspace_root=workspace_root,
        hub_root=hub_root,
    )
    scope_kind = scope_ref.kind if scope_ref is not None else None
    if scope_kind is None:
        scope_kind = resource_kind or (
            "hub" if scope_urn == ScopeRef(kind="hub").to_urn() else "filesystem"
        )
    client_intent_fields = {
        "scope_urn": normalize_optional_text(payload.scope_urn),
        "workspace_root": normalize_optional_text(payload.workspace_root),
        "repo_id": normalize_optional_text(payload.repo_id),
        "resource_kind": normalize_optional_text(payload.resource_kind),
        "resource_id": normalize_optional_text(payload.resource_id),
    }
    metadata: dict[str, Any] = {
        "origin": origin,
        "scope_source": scope_source,
        "legacy": not genesis_provided,
        "scope": {
            "urn": scope_urn,
            "kind": scope_kind,
            "repo_id": repo_id,
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "workspace_root": str(workspace_root),
        },
        "client_scope_request": {
            key: value
            for key, value in client_intent_fields.items()
            if value is not None
        },
    }
    parent_thread_id = normalize_optional_text(payload.parent_thread_id)
    if parent_thread_id is not None:
        metadata["parent_thread_id"] = parent_thread_id
    fork_mode = normalize_optional_text(payload.fork_mode)
    if fork_mode is not None:
        metadata["fork_mode"] = fork_mode
    if payload.client_intent is not None:
        metadata["client_intent"] = payload.client_intent
    return metadata


def _worktree_parent_repo_id(
    repos: Any,
    worktree_id: Optional[str],
) -> Optional[str]:
    if worktree_id is None:
        return None
    for snapshot in repos or ():
        if normalize_optional_text(_repo_snapshot_value(snapshot, "id")) != worktree_id:
            continue
        return normalize_optional_text(_repo_snapshot_value(snapshot, "worktree_of"))
    return None


def _repo_snapshot_value(snapshot: Any, attr: str) -> Any:
    if isinstance(snapshot, dict):
        return snapshot.get(attr)
    return getattr(snapshot, attr, None)


def managed_thread_metadata_for_provisioned_workspace(
    resolution: ManagedThreadCreateResolution,
    provisioned_workspace: ManagedThreadWorkspaceProvision,
) -> dict[str, Any]:
    metadata = dict(resolution.metadata)
    genesis = metadata.get("genesis")
    if isinstance(genesis, dict):
        genesis = dict(genesis)
        scope = genesis.get("scope")
        if isinstance(scope, dict):
            scope = dict(scope)
            scope["actual_workspace_root"] = str(provisioned_workspace.workspace_root)
            genesis["scope"] = scope
        metadata["genesis"] = genesis
    return metadata


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
    _validate_legacy_scope_field_consistency(payload)
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
    worktree_parent_repo_id = _worktree_parent_repo_id(
        repos,
        resource_id if resource_kind == "worktree" else None,
    )
    if resource_kind == "worktree" and worktree_parent_repo_id is None:
        raise HTTPException(
            status_code=400,
            detail="Worktree scope requires worktree_of metadata",
        )
    followup_policy = resolve_managed_thread_followup_policy(
        payload,
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
        "chat_kind": normalize_managed_thread_chat_kind(payload.chat_kind),
        "genesis": _create_genesis_metadata(
            payload=payload,
            scope_ref=scope_ref,
            resource_kind=resource_kind,
            resource_id=resource_id,
            repo_id=resolved_repo_id,
            worktree_parent_repo_id=worktree_parent_repo_id,
            workspace_root=resolved_workspace,
            hub_root=hub_root,
        ),
    }
    chat_kind = normalize_optional_text(payload.chat_kind)
    if chat_kind is not None:
        metadata["chat_kind"] = chat_kind
        if chat_kind == "pma":
            metadata["thread_kind"] = "pma"
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


__all__ = [
    "ManagedThreadCreateResolution",
    "ManagedThreadWorkspaceProvision",
    "_normalize_resource_owner",
    "_normalize_workspace_root_input",
    "managed_thread_metadata_for_provisioned_workspace",
    "provision_managed_thread_workspace",
    "resolve_managed_thread_create_resolution",
]
