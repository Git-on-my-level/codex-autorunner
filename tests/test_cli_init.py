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
    contents = config_path.read_text(encoding="utf-8")
    assert "mode: hub" in contents


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
    contents = config_path.read_text(encoding="utf-8")
    assert "mode: hub" in contents


def test_init_walks_nested_child_git_repos(tmp_path: Path):
    workspace = tmp_path / "workspace"
    nested_repo = workspace / "projects" / "demo"
    nested_repo.mkdir(parents=True)
    (nested_repo / ".git").mkdir()

    result = runner.invoke(app, ["init", str(workspace)])

    assert result.exit_code == 0
    config_path = workspace / ".codex-autorunner" / "config.yml"
    assert config_path.exists()
    contents = config_path.read_text(encoding="utf-8")
    assert "mode: hub" in contents


def test_create_app_allows_parent_without_git(tmp_path: Path):
    workspace = tmp_path / "workspace"
    nested_repo = workspace / "projects" / "demo"
    nested_repo.mkdir(parents=True)
    (nested_repo / ".git").mkdir()

    init_result = runner.invoke(app, ["init", str(workspace)])
    assert init_result.exit_code == 0

    # Should serve hub even though workspace has no .git
    from codex_autorunner.server import create_hub_app

    app_instance = create_hub_app(workspace)
    assert app_instance is not None


def test_init_errors_on_legacy_repo_config(tmp_path: Path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    config_dir = repo_root / ".codex-autorunner"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yml").write_text("mode: repo\n", encoding="utf-8")

    result = runner.invoke(app, ["init", str(repo_root)])

    assert result.exit_code != 0
    assert "repo.override.yml" in result.output
