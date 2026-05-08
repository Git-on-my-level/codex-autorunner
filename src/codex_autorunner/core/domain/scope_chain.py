from __future__ import annotations

from typing import Optional

from .refs import ScopeRef


def parent_scope(scope: ScopeRef) -> Optional[ScopeRef]:
    """Return the immediate parent scope, or None for hub."""
    if scope.kind == "hub":
        return None
    if scope.kind == "repo":
        return ScopeRef(kind="hub")
    if scope.kind == "worktree":
        return ScopeRef(kind="repo", id=scope.parent_repo_id)
    if scope.kind == "filesystem":
        return ScopeRef(kind="hub")
    return None


def scope_chain(scope: ScopeRef) -> tuple[ScopeRef, ...]:
    """Return the ancestry from leaf to root (inclusive)."""
    chain: list[ScopeRef] = [scope]
    current = scope
    while True:
        p = parent_scope(current)
        if p is None:
            break
        chain.append(p)
        current = p
    return tuple(chain)


__all__ = [
    "parent_scope",
    "scope_chain",
]
