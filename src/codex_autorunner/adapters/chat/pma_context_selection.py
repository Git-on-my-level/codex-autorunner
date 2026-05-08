from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ...core.car_context import (
    CarContextProfile,
    default_managed_thread_context_profile,
)
from ...core.utils import canonicalize_path


class PmaContextSelectionError(ValueError):
    """Raised when a requested PMA context cannot be resolved."""


@dataclass(frozen=True)
class PmaContextSelection:
    workspace_root: Path
    repo_id: Optional[str]
    resource_kind: Optional[str]
    resource_id: Optional[str]
    context_profile: CarContextProfile
    scope_label: str
    runtime: Optional[str] = None


@dataclass(frozen=True)
class PmaResourceOwner:
    resource_kind: Optional[str]
    resource_id: Optional[str]
    repo_id: Optional[str]


def normalize_pma_resource_owner(
    *,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    repo_id: Optional[str] = None,
) -> PmaResourceOwner:
    normalized_kind = _optional_text(resource_kind)
    normalized_id = _optional_text(resource_id)
    normalized_repo = _optional_text(repo_id)
    if normalized_repo and normalized_kind is None and normalized_id is None:
        normalized_kind = "repo"
        normalized_id = normalized_repo
    if normalized_id and normalized_kind is None:
        raise PmaContextSelectionError(
            "resource_kind is required when resource_id is provided"
        )
    if normalized_kind and normalized_id is None:
        raise PmaContextSelectionError(
            "resource_id is required when resource_kind is provided"
        )
    if normalized_kind not in {None, "repo", "worktree"}:
        raise PmaContextSelectionError("resource_kind must be one of: repo, worktree")
    normalized_repo = normalized_id if normalized_kind == "repo" else None
    return PmaResourceOwner(
        resource_kind=normalized_kind,
        resource_id=normalized_id,
        repo_id=normalized_repo,
    )


def hub_pma_context(hub_root: Path) -> PmaContextSelection:
    return PmaContextSelection(
        workspace_root=hub_root.absolute(),
        repo_id=None,
        resource_kind=None,
        resource_id=None,
        context_profile=default_managed_thread_context_profile(),
        scope_label="hub",
    )


def resolve_pma_context_selection(
    *,
    hub_root: Path,
    workspace_root: Optional[str | Path] = None,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    repo_id: Optional[str] = None,
    repos: Any = (),
) -> PmaContextSelection:
    owner = normalize_pma_resource_owner(
        resource_kind=resource_kind,
        resource_id=resource_id,
        repo_id=repo_id,
    )
    if owner.resource_kind is not None:
        return _resolve_owned_context(
            hub_root=hub_root,
            owner=owner,
            repos=repos,
        )

    workspace_text = _optional_text(workspace_root)
    if workspace_text is None or workspace_text == ".":
        return hub_pma_context(hub_root)

    workspace = Path(workspace_text)
    if not workspace.is_absolute():
        workspace = hub_root / workspace
    try:
        workspace = canonicalize_path(workspace)
    except OSError as exc:
        raise PmaContextSelectionError(
            f"workspace_root is invalid: {workspace_text}"
        ) from exc
    return PmaContextSelection(
        workspace_root=workspace.absolute(),
        repo_id=None,
        resource_kind=None,
        resource_id=None,
        context_profile=default_managed_thread_context_profile(),
        scope_label=f"workspace {workspace}",
    )


def _resolve_owned_context(
    *,
    hub_root: Path,
    owner: PmaResourceOwner,
    repos: Any,
) -> PmaContextSelection:
    if owner.resource_kind == "repo":
        assert owner.resource_id is not None
        workspace = _repo_workspace(repos, owner.resource_id)
        if workspace is None:
            raise PmaContextSelectionError(f"Repo not found: {owner.resource_id}")
        return PmaContextSelection(
            workspace_root=workspace.absolute(),
            repo_id=owner.resource_id,
            resource_kind="repo",
            resource_id=owner.resource_id,
            context_profile=default_managed_thread_context_profile(),
            scope_label=f"repo {owner.resource_id}",
        )
    if owner.resource_kind == "worktree":
        assert owner.resource_id is not None
        workspace = _repo_workspace(repos, owner.resource_id)
        if workspace is None:
            raise PmaContextSelectionError(f"Worktree not found: {owner.resource_id}")
        return PmaContextSelection(
            workspace_root=workspace.absolute(),
            repo_id=None,
            resource_kind="worktree",
            resource_id=owner.resource_id,
            context_profile=default_managed_thread_context_profile(),
            scope_label=f"worktree {owner.resource_id}",
        )
    return hub_pma_context(hub_root)


def _repo_workspace(repos: Any, repo_id: str) -> Optional[Path]:
    for entry in repos or ():
        entry_id = _entry_value(entry, "id", 0)
        if entry_id != repo_id:
            continue
        entry_path = _entry_value(entry, "path", 1)
        path = Path(entry_path) if isinstance(entry_path, str) else entry_path
        return path if isinstance(path, Path) else None
    return None


def _entry_value(entry: Any, attr: str, index: int) -> Any:
    if isinstance(entry, dict):
        return entry.get(attr)
    value = getattr(entry, attr, None)
    if value is not None:
        return value
    if isinstance(entry, (tuple, list)) and len(entry) > index:
        return entry[index]
    return None


def _optional_text(value: Any) -> Optional[str]:
    if isinstance(value, Path):
        return str(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


__all__ = [
    "PmaContextSelection",
    "PmaContextSelectionError",
    "PmaResourceOwner",
    "hub_pma_context",
    "normalize_pma_resource_owner",
    "resolve_pma_context_selection",
]
