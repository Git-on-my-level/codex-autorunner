from pathlib import Path

from codex_autorunner.workspace import canonical_workspace_root, workspace_id_for_path


def test_workspace_id_stable_for_same_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first = workspace_id_for_path(repo)
    second = workspace_id_for_path(repo)
    assert first == second


def test_workspace_id_uses_canonical_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    link = tmp_path / "link"
    link.symlink_to(repo)
    assert canonical_workspace_root(link) == canonical_workspace_root(repo)
    assert workspace_id_for_path(link) == workspace_id_for_path(repo)
