from pathlib import Path

import pytest

from codex_autorunner.workspace.paths import normalize_workspace_rel_path


def test_rejects_absolute_and_parent_refs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(ValueError):
        normalize_workspace_rel_path(repo_root, "/etc/passwd")
    with pytest.raises(ValueError):
        normalize_workspace_rel_path(repo_root, "../secrets")


def test_allows_simple_relative_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".codex-autorunner" / "workspace").mkdir(parents=True)

    abs_path, rel = normalize_workspace_rel_path(repo_root, "notes/todo.md")
    assert (
        abs_path == repo_root / ".codex-autorunner" / "workspace" / "notes" / "todo.md"
    )
    assert rel == "notes/todo.md"


def test_blocks_symlink_escape(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    workspace = repo_root / ".codex-autorunner" / "workspace"
    workspace.mkdir(parents=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    escape = workspace / "link"
    escape.symlink_to(outside)

    with pytest.raises(ValueError):
        normalize_workspace_rel_path(repo_root, "link/secret.txt")
