from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .refs import ScopeRef, ScopeRefError


@dataclass(frozen=True)
class WorkspaceScopeResolution:
    scope: ScopeRef
    workspace_root: Optional[str] = None
    source: str = "unknown"

    def owner_fields(self) -> dict[str, Optional[str]]:
        if self.scope.kind == "repo":
            return {
                "repo_id": self.scope.id,
                "worktree_id": None,
                "resource_kind": "repo",
                "resource_id": self.scope.id,
                "workspace_root": self.workspace_root,
                "scope_urn": self.scope.to_urn(),
            }
        if self.scope.kind == "worktree":
            return {
                "repo_id": self.scope.parent_repo_id,
                "worktree_id": self.scope.id,
                "resource_kind": "worktree",
                "resource_id": self.scope.id,
                "workspace_root": self.workspace_root,
                "scope_urn": self.scope.to_urn(),
            }
        if self.scope.kind == "filesystem":
            return {
                "repo_id": None,
                "worktree_id": None,
                "resource_kind": "filesystem",
                "resource_id": self.scope.path,
                "workspace_root": self.workspace_root or self.scope.path,
                "scope_urn": self.scope.to_urn(),
            }
        return {
            "repo_id": None,
            "worktree_id": None,
            "resource_kind": self.scope.kind,
            "resource_id": self.scope.id,
            "workspace_root": self.workspace_root,
            "scope_urn": self.scope.to_urn(),
        }


class WorkspaceScopeIndex:
    """Resolve mixed workspace identifiers into canonical CAR scope refs."""

    def __init__(self, snapshots: Iterable[Any]) -> None:
        self._by_id: dict[str, WorkspaceScopeResolution] = {}
        self._by_path: dict[str, WorkspaceScopeResolution] = {}
        for snapshot in snapshots:
            resolution = _resolution_from_snapshot(snapshot)
            if resolution is None:
                continue
            assert resolution.scope.id is not None
            self._by_id[resolution.scope.id] = resolution
            workspace_root = resolution.workspace_root
            if workspace_root:
                self._by_path[workspace_root] = resolution
                canonical = _canonical_path(workspace_root)
                if canonical:
                    self._by_path[canonical] = resolution

    def resolve(
        self,
        *,
        raw_repo_id: Any = None,
        workspace_path: Any = None,
        resource_kind: Any = None,
        resource_id: Any = None,
        scope_urn: Any = None,
    ) -> Optional[WorkspaceScopeResolution]:
        urn = _text(scope_urn)
        if urn:
            try:
                scope = ScopeRef.from_urn(urn)
            except (ScopeRefError, ValueError):
                scope = None
            if scope is not None:
                known = self._known_resolution(scope)
                return known or WorkspaceScopeResolution(
                    scope=scope,
                    workspace_root=scope.path if scope.kind == "filesystem" else None,
                    source="scope_urn",
                )

        kind = _text(resource_kind)
        identifier = _text(resource_id)
        repo_id = _text(raw_repo_id)

        if kind == "worktree" and identifier:
            known = self._by_id.get(identifier)
            if known is not None and known.scope.kind == "worktree":
                return known
            parent_repo_id = repo_id
            if not parent_repo_id:
                known_repo = self._by_id.get(identifier)
                parent_repo_id = known_repo.scope.parent_repo_id if known_repo else None
            if parent_repo_id:
                return WorkspaceScopeResolution(
                    scope=ScopeRef(
                        kind="worktree",
                        id=identifier,
                        parent_repo_id=parent_repo_id,
                    ),
                    workspace_root=_workspace_root(workspace_path),
                    source="resource",
                )

        if kind == "repo" and identifier:
            known = self._by_id.get(identifier)
            if known is not None:
                return known
            return WorkspaceScopeResolution(
                scope=ScopeRef(kind="repo", id=identifier),
                workspace_root=_workspace_root(workspace_path),
                source="resource",
            )

        if repo_id:
            known = self._by_id.get(repo_id)
            if known is not None:
                return known
            return WorkspaceScopeResolution(
                scope=ScopeRef(kind="repo", id=repo_id),
                workspace_root=_workspace_root(workspace_path),
                source="repo_id",
            )

        for candidate in _workspace_path_candidates(workspace_path):
            canonical = _canonical_path(candidate)
            if canonical and canonical in self._by_path:
                return self._by_path[canonical]
            if candidate in self._by_path:
                return self._by_path[candidate]
        return None

    def _known_resolution(self, scope: ScopeRef) -> Optional[WorkspaceScopeResolution]:
        if scope.kind in {"repo", "worktree"} and scope.id:
            return self._by_id.get(scope.id)
        if scope.kind == "filesystem" and scope.path:
            canonical = _canonical_path(scope.path)
            if canonical and canonical in self._by_path:
                return self._by_path[canonical]
            return self._by_path.get(scope.path)
        return None


def workspace_scope_index_from_snapshots(
    snapshots: Iterable[Any],
) -> WorkspaceScopeIndex:
    return WorkspaceScopeIndex(snapshots)


def _resolution_from_snapshot(snapshot: Any) -> Optional[WorkspaceScopeResolution]:
    repo_id = _text(_field(snapshot, "id"))
    if not repo_id:
        return None
    kind = _text(_field(snapshot, "kind")) or "base"
    path = _field(snapshot, "path")
    workspace_root = str(path) if isinstance(path, Path) else _text(path)
    if kind == "worktree":
        parent_repo_id = _text(_field(snapshot, "worktree_of"))
        if not parent_repo_id:
            return None
        return WorkspaceScopeResolution(
            scope=ScopeRef(kind="worktree", id=repo_id, parent_repo_id=parent_repo_id),
            workspace_root=workspace_root,
            source="topology",
        )
    return WorkspaceScopeResolution(
        scope=ScopeRef(kind="repo", id=repo_id),
        workspace_root=workspace_root,
        source="topology",
    )


def _field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _canonical_path(value: str) -> Optional[str]:
    try:
        return str(Path(value).resolve())
    except OSError:
        return str(Path(value))


def _workspace_path_candidates(value: Any) -> list[str]:
    if isinstance(value, Path):
        return [str(value)]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _workspace_root(value: Any) -> Optional[str]:
    for candidate in _workspace_path_candidates(value):
        canonical = _canonical_path(candidate)
        return canonical or candidate
    return None


__all__ = [
    "WorkspaceScopeIndex",
    "WorkspaceScopeResolution",
    "workspace_scope_index_from_snapshots",
]
