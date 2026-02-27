from __future__ import annotations

from typing import Any, Dict, Mapping

from ..manifest import ManifestRepo, normalize_manifest_destination


def default_local_destination() -> Dict[str, Any]:
    return {"kind": "local"}


def resolve_effective_repo_destination(
    repo: ManifestRepo,
    repos_by_id: Mapping[str, ManifestRepo],
) -> Dict[str, Any]:
    own = normalize_manifest_destination(repo.destination)
    if own is not None:
        return own

    parent_id = repo.worktree_of if repo.kind == "worktree" else None
    if isinstance(parent_id, str) and parent_id:
        parent = repos_by_id.get(parent_id)
        if parent is not None:
            inherited = normalize_manifest_destination(parent.destination)
            if inherited is not None:
                return inherited

    return default_local_destination()
