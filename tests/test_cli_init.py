from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()


def test_init_from_subdir_walks_to_repo_root(tmp_path: Path):
    repo_root = tmp_path / "project"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    nested = repo_root / "src" / "pkg"
    nested.mkdir(parents=True)

    result = runner.invoke(app, ["init", str(nested)])

    assert result.exit_code == 0
    config_path = repo_root / ".codex-autorunner" / "config.yml"
    assert config_path.exists()
    assert not (nested / ".codex-autorunner").exists()


def test_init_allows_child_git_repos_without_parent(tmp_path: Path):
    workspace = tmp_path / "workspace"
    repo_a = workspace / "repo-a"
    repo_a.mkdir(parents=True)
    (repo_a / ".git").mkdir()
    repo_b = workspace / "repo-b"
    repo_b.mkdir()
    (repo_b / ".git").mkdir()

    result = runner.invoke(app, ["init", str(workspace)])

    assert result.exit_code == 0
    config_path = workspace / ".codex-autorunner" / "config.yml"
    assert config_path.exists()


def test_init_walks_nested_child_git_repos(tmp_path: Path):
    workspace = tmp_path / "workspace"
    nested_repo = workspace / "projects" / "demo"
    nested_repo.mkdir(parents=True)
    (nested_repo / ".git").mkdir()

    result = runner.invoke(app, ["init", str(workspace)])

    assert result.exit_code == 0
    assert (workspace / ".codex-autorunner" / "config.yml").exists()
