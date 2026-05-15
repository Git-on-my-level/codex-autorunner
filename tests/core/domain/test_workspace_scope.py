from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.domain.refs import ScopeRef
from codex_autorunner.core.domain.workspace_scope import (
    workspace_scope_index_from_snapshots,
)
from codex_autorunner.manifest import ManifestRepo


def test_workspace_path_resolves_worktree_to_parent_repo_owner_fields(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktrees" / "repo--feature"
    worktree_path.mkdir(parents=True)
    index = workspace_scope_index_from_snapshots(
        [
            ManifestRepo(
                id="repo",
                path=Path("repos/repo"),
                kind="base",
            ),
            ManifestRepo(
                id="repo--feature",
                path=worktree_path,
                kind="worktree",
                worktree_of="repo",
            ),
        ]
    )

    resolution = index.resolve(workspace_path=str(worktree_path))

    assert resolution is not None
    assert resolution.scope == ScopeRef(
        kind="worktree",
        id="repo--feature",
        parent_repo_id="repo",
    )
    assert resolution.owner_fields() == {
        "repo_id": "repo",
        "worktree_id": "repo--feature",
        "resource_kind": "worktree",
        "resource_id": "repo--feature",
        "workspace_root": str(worktree_path),
        "scope_urn": "worktree:repo/repo--feature",
    }


def test_legacy_repo_id_that_names_worktree_is_canonicalized_to_worktree(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "repo--discord-1"
    worktree_path.mkdir()
    index = workspace_scope_index_from_snapshots(
        [
            ManifestRepo(id="repo", path=tmp_path / "repo", kind="base"),
            ManifestRepo(
                id="repo--discord-1",
                path=worktree_path,
                kind="worktree",
                worktree_of="repo",
            ),
        ]
    )

    resolution = index.resolve(raw_repo_id="repo--discord-1")

    assert resolution is not None
    assert resolution.owner_fields()["repo_id"] == "repo"
    assert resolution.owner_fields()["worktree_id"] == "repo--discord-1"
    assert resolution.owner_fields()["resource_kind"] == "worktree"


def test_unknown_repo_id_preserves_supplied_workspace_path(tmp_path: Path) -> None:
    stale_path = tmp_path / "removed-repo"
    stale_path.mkdir()
    index = workspace_scope_index_from_snapshots([])

    resolution = index.resolve(raw_repo_id="removed-repo", workspace_path=stale_path)

    assert resolution is not None
    assert resolution.scope == ScopeRef(kind="repo", id="removed-repo")
    assert resolution.owner_fields()["repo_id"] == "removed-repo"
    assert resolution.owner_fields()["workspace_root"] == str(stale_path)
