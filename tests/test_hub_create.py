import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.hub_repo_manager import RepoManager
from codex_autorunner.core.hub_topology import HubTopologyRepository
from tests.conftest import write_test_config
from tests.support.git_test_helpers import init_git_repo as _init_git_repo


def _make_repo_manager(hub_root: Path, **cfg_overrides) -> RepoManager:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    for k, v in cfg_overrides.items():
        cfg["hub"][k] = v
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    hub_config = load_hub_config(hub_root)
    return RepoManager(
        hub_config,
        topology_repository=HubTopologyRepository(
            hub_root=hub_config.root,
            manifest_path=hub_config.manifest_path,
        ),
        on_invalidate_cache=lambda: None,
        on_snapshot_for_repo=lambda rid: None,
    )


def test_hub_create_repo_cli(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["hub"]["repos_root"] = "workspace"
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    runner = CliRunner()
    result = runner.invoke(app, ["hub", "create", "demo", "--path", str(hub_root)])
    assert result.exit_code == 0

    repo_dir = hub_root / "workspace" / "demo"
    assert (repo_dir / ".git").exists()
    assert (repo_dir / ".codex-autorunner" / "state.sqlite3").exists()
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["repos"][0]["id"] == "demo"
    assert manifest["repos"][0]["path"] == "workspace/demo"


def test_hub_create_repo_rejects_outside_repos_root(tmp_path: Path):
    hub_root = tmp_path / "hub"
    rm = _make_repo_manager(hub_root, repos_root="workspace")
    with pytest.raises(ValueError):
        rm.create_repo("bad", repo_path=Path(".."))


def test_hub_create_repo_rejects_duplicate_id(tmp_path: Path):
    hub_root = tmp_path / "hub"
    rm = _make_repo_manager(hub_root, repos_root="workspace")
    rm.create_repo("demo")
    with pytest.raises(ValueError, match="Repo id demo already exists"):
        rm.create_repo("demo", repo_path=Path("other"))


def test_hub_create_repo_rejects_paths_under_worktrees_root(tmp_path: Path):
    hub_root = tmp_path / "hub"
    rm = _make_repo_manager(hub_root, repos_root=".", worktrees_root="worktrees")
    with pytest.raises(ValueError, match="must not live under worktrees_root"):
        rm.create_repo("bad", repo_path=Path("worktrees/bad"))


def test_hub_clone_repo_cli(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["hub"]["repos_root"] = "workspace"
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    source_repo = tmp_path / "source"
    _init_git_repo(source_repo)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "clone",
            "--git-url",
            str(source_repo),
            "--id",
            "cloned",
            "--path",
            str(hub_root),
        ],
    )
    assert result.exit_code == 0
    assert "cloned cloned" in result.output or "cloned at" in result.output

    repo_dir = hub_root / "workspace" / "cloned"
    assert (repo_dir / ".git").exists()
    assert (repo_dir / ".codex-autorunner" / "state.sqlite3").exists()


def test_hub_clone_repo_rejects_duplicate_id(tmp_path: Path):
    hub_root = tmp_path / "hub"
    rm = _make_repo_manager(hub_root, repos_root="workspace")

    source_repo = tmp_path / "source"
    _init_git_repo(source_repo)

    rm.create_repo("demo")
    with pytest.raises(ValueError, match="Repo id demo already exists"):
        rm.clone_repo(
            git_url=str(source_repo),
            repo_id="demo",
            repo_path=Path("other"),
        )


def test_hub_clone_repo_cli_failure_message(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    missing_repo = tmp_path / "missing"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "clone",
            "--git-url",
            str(missing_repo),
            "--path",
            str(hub_root),
        ],
    )
    assert result.exit_code == 1
    assert "git clone failed" in result.output
