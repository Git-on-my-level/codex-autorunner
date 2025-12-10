import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG, load_config
from codex_autorunner.hub import HubSupervisor


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


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
    assert (repo_dir / ".codex-autorunner" / "config.yml").exists()
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["repos"][0]["id"] == "demo"
    assert manifest["repos"][0]["path"] == "workspace/demo"


def test_hub_create_repo_rejects_outside_repos_root(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["hub"]["repos_root"] = "workspace"
    _write_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(load_config(hub_root))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        supervisor.create_repo("bad", repo_path=Path(".."))
