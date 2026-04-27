from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.config import CONFIG_FILENAME
from codex_autorunner.core.git_utils import run_git

runner = CliRunner()


def _init_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], repo_path, check=True)
    run_git(["config", "user.email", "test@example.com"], repo_path, check=True)
    run_git(["config", "user.name", "Test User"], repo_path, check=True)
    run_git(["checkout", "-b", "main"], repo_path, check=True)


def _commit_repo(repo_path: Path, message: str) -> None:
    run_git(["add", "."], repo_path, check=True)
    run_git(["commit", "-m", message], repo_path, check=True)


def _configure_apps_repo(
    hub_root: Path, app_repo: Path, *, enabled: bool = True
) -> None:
    config_path = hub_root / CONFIG_FILENAME
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["apps"] = {
        "enabled": enabled,
        "repos": (
            [
                {
                    "id": "local",
                    "url": str(app_repo),
                    "trusted": True,
                    "default_ref": "main",
                }
            ]
            if enabled
            else []
        ),
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _create_app_repo(tmp_path: Path) -> Path:
    app_repo = tmp_path / "app_repo"
    _init_repo(app_repo)
    (app_repo / "apps" / "hello").mkdir(parents=True)
    (app_repo / "apps" / "hello" / "car-app.yaml").write_text(
        """schema_version: 1
id: local.hello
name: Hello App
version: 1.0.0
description: CLI-visible app.
tools:
  check:
    argv: ["python3", "scripts/check.py"]
""",
        encoding="utf-8",
    )
    _commit_repo(app_repo, "add app")
    return app_repo


def test_apps_list_json(repo, hub_env, tmp_path: Path) -> None:
    app_repo = _create_app_repo(tmp_path)
    _configure_apps_repo(hub_env.hub_root, app_repo)

    result = runner.invoke(app, ["apps", "list", "--repo", str(repo), "--json"])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["count"] == 1
    assert parsed["apps"][0]["app_id"] == "local.hello"
    assert parsed["apps"][0]["source_ref"] == "local:apps/hello@main"


def test_apps_show_json(repo, hub_env, tmp_path: Path) -> None:
    app_repo = _create_app_repo(tmp_path)
    _configure_apps_repo(hub_env.hub_root, app_repo)

    result = runner.invoke(
        app,
        ["apps", "show", "local:apps/hello", "--repo", str(repo), "--json"],
    )

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["app_id"] == "local.hello"
    assert parsed["manifest"]["name"] == "Hello App"
    assert "manifest_text" in parsed


def test_apps_show_installed_id_not_implemented(repo, hub_env, tmp_path: Path) -> None:
    app_repo = _create_app_repo(tmp_path)
    _configure_apps_repo(hub_env.hub_root, app_repo)

    result = runner.invoke(
        app,
        ["apps", "show", "local.hello", "--repo", str(repo)],
    )

    assert result.exit_code != 0
    assert "Installed app lookup is not implemented yet" in result.output
