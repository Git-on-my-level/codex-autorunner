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
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.hub import HubSupervisor


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(["add", "README.md"], path, check=True)
    run_git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        path,
        check=True,
    )


def test_hub_create_repo_cli(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["hub"]["repos_root"] = "workspace"
    _write_config(hub_root / CONFIG_FILENAME, cfg)

    runner = CliRunner()
    result = runner.invoke(app, ["hub", "create", "demo", "--path", str(hub_root)])
    assert result.exit_code == 0

    repo_dir = hub_root / "workspace" / "demo"
    assert (repo_dir / ".git").exists()
    assert (repo_dir / ".codex-autorunner" / "state.json").exists()
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["repos"][0]["id"] == "demo"
    assert manifest["repos"][0]["path"] == "workspace/demo"


def test_hub_create_repo_rejects_outside_repos_root(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["hub"]["repos_root"] = "workspace"
    _write_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(load_hub_config(hub_root))
    with pytest.raises(ValueError):
        supervisor.create_repo("bad", repo_path=Path(".."))


def test_hub_clone_repo_cli(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["hub"]["repos_root"] = "workspace"
    _write_config(hub_root / CONFIG_FILENAME, cfg)

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
    assert "Cloned repo cloned" in result.output

    repo_dir = hub_root / "workspace" / "cloned"
    assert (repo_dir / ".git").exists()
    assert (repo_dir / ".codex-autorunner" / "state.json").exists()


def test_hub_clone_repo_cli_failure_message(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    _write_config(hub_root / CONFIG_FILENAME, cfg)

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
