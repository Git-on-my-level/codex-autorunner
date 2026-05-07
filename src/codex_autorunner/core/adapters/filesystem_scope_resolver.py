from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ...manifest import Manifest
from ..domain.refs import ScopeRef, ScopeRefError
from ..domain.scope_chain import parent_scope
from ..ports.scope_resolver import ResolvedScope


class FilesystemScopeResolver:
    def __init__(self, hub_root: Path, manifest: Manifest) -> None:
        self._hub_root = hub_root
        self._manifest = manifest

    def resolve(self, ref: ScopeRef) -> ResolvedScope:
        if ref.kind == "hub":
            return ResolvedScope(
                scope=ref,
                display_name="Hub",
                workspace_root=str(self._hub_root),
            )
        if ref.kind == "repo":
            return self._resolve_repo(ref)
        if ref.kind == "worktree":
            return self._resolve_worktree(ref)
        if ref.kind == "agent_workspace":
            return self._resolve_agent_workspace(ref)
        if ref.kind == "filesystem":
            assert ref.path is not None
            return ResolvedScope(
                scope=ref,
                display_name=ref.path,
                workspace_root=ref.path,
            )
        raise ScopeRefError(f"Unknown scope kind: {ref.kind}")

    def resolve_parent(self, ref: ScopeRef) -> Optional[ScopeRef]:
        return parent_scope(ref)

    def resolve_children(self, ref: ScopeRef) -> List[ScopeRef]:
        if ref.kind == "hub":
            children: list[ScopeRef] = []
            for repo in self._manifest.repos:
                if repo.kind == "base":
                    children.append(ScopeRef(kind="repo", id=repo.id))
            for ws in self._manifest.agent_workspaces:
                children.append(ScopeRef(kind="agent_workspace", id=ws.id))
            return children
        if ref.kind == "repo":
            children = []
            for repo in self._manifest.repos:
                if repo.kind == "worktree" and repo.worktree_of == ref.id:
                    children.append(
                        ScopeRef(
                            kind="worktree",
                            id=repo.id,
                            parent_repo_id=ref.id,
                        )
                    )
            return children
        return []

    def _resolve_repo(self, ref: ScopeRef) -> ResolvedScope:
        assert ref.id is not None
        repo = self._manifest.get(ref.id)
        if repo is None:
            raise ScopeRefError(f"Unknown repo scope: {ref.id}")
        if repo.kind != "base":
            raise ScopeRefError(
                f"Scope {ref.id} is a {repo.kind} manifest entry, not a repo scope"
            )
        abs_path = self._hub_root / repo.path
        return ResolvedScope(
            scope=ref,
            display_name=repo.display_name or repo.id,
            workspace_root=str(abs_path),
            metadata={"kind": repo.kind, "branch": repo.branch},
        )

    def _resolve_worktree(self, ref: ScopeRef) -> ResolvedScope:
        assert ref.id is not None
        repo = self._manifest.get(ref.id)
        if repo is None:
            raise ScopeRefError(f"Unknown worktree scope: {ref.id}")
        if repo.kind != "worktree":
            raise ScopeRefError(
                f"Scope {ref.id} is a {repo.kind} manifest entry, not a worktree scope"
            )
        abs_path = self._hub_root / repo.path
        return ResolvedScope(
            scope=ref,
            display_name=repo.display_name or ref.id,
            workspace_root=str(abs_path),
            metadata={"worktree_of": ref.parent_repo_id, "branch": repo.branch},
        )

    def _resolve_agent_workspace(self, ref: ScopeRef) -> ResolvedScope:
        assert ref.id is not None
        ws = self._manifest.get_agent_workspace(ref.id)
        if ws is None:
            raise ScopeRefError(f"Unknown agent_workspace scope: {ref.id}")
        abs_path = self._hub_root / ws.path
        return ResolvedScope(
            scope=ref,
            display_name=ws.display_name or ref.id,
            workspace_root=str(abs_path),
            metadata={"runtime": ws.runtime},
        )


__all__ = ["FilesystemScopeResolver"]
