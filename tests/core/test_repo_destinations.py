from pathlib import Path

from codex_autorunner.core.repo_destinations import (
    default_local_destination,
    resolve_effective_repo_destination,
)
from codex_autorunner.manifest import ManifestRepo


def test_resolve_effective_destination_prefers_repo_destination() -> None:
    base = ManifestRepo(
        id="base",
        path=Path("workspace/base"),
        kind="base",
        destination={"kind": "docker", "image": "base:latest"},
    )
    worktree = ManifestRepo(
        id="base--feat",
        path=Path("worktrees/base--feat"),
        kind="worktree",
        worktree_of="base",
        destination={"kind": "docker", "image": "feat:latest"},
    )
    repos = {base.id: base, worktree.id: worktree}

    assert resolve_effective_repo_destination(worktree, repos) == {
        "kind": "docker",
        "image": "feat:latest",
    }


def test_resolve_effective_destination_inherits_base_for_worktree() -> None:
    base = ManifestRepo(
        id="base",
        path=Path("workspace/base"),
        kind="base",
        destination={"kind": "docker", "image": "base:latest"},
    )
    worktree = ManifestRepo(
        id="base--feat",
        path=Path("worktrees/base--feat"),
        kind="worktree",
        worktree_of="base",
    )
    repos = {base.id: base, worktree.id: worktree}

    assert resolve_effective_repo_destination(worktree, repos) == {
        "kind": "docker",
        "image": "base:latest",
    }


def test_resolve_effective_destination_defaults_to_local() -> None:
    base = ManifestRepo(id="base", path=Path("workspace/base"), kind="base")
    repos = {base.id: base}

    assert (
        resolve_effective_repo_destination(base, repos) == default_local_destination()
    )


def test_resolve_effective_destination_ignores_invalid_destination_shapes() -> None:
    base = ManifestRepo(
        id="base",
        path=Path("workspace/base"),
        kind="base",
        destination={"kind": "docker", "image": "base:latest"},
    )
    worktree = ManifestRepo(
        id="base--feat",
        path=Path("worktrees/base--feat"),
        kind="worktree",
        worktree_of="base",
        destination={"image": "missing-kind"},
    )
    repos = {base.id: base, worktree.id: worktree}

    assert resolve_effective_repo_destination(worktree, repos) == {
        "kind": "docker",
        "image": "base:latest",
    }
