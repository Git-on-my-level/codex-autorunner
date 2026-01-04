from pathlib import Path

from codex_autorunner.telegram_bot import _paths_compatible


def _make_repo(path: Path) -> None:
    (path / ".git").mkdir(parents=True, exist_ok=True)


def test_paths_compatible_rejects_repo_parent(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "repo"
    _make_repo(repo_root)
    assert not _paths_compatible(repo_root, hub_root)


def test_paths_compatible_accepts_repo_subdir(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _make_repo(repo_root)
    resumed_root = repo_root / "src"
    resumed_root.mkdir(parents=True)
    assert _paths_compatible(repo_root, resumed_root)


def test_paths_compatible_accepts_repo_root_for_bound_subdir(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _make_repo(repo_root)
    workspace_root = repo_root / "src"
    workspace_root.mkdir(parents=True)
    assert _paths_compatible(workspace_root, repo_root)
