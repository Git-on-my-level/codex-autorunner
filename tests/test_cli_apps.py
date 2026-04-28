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
    (app_repo / "apps" / "hello" / "scripts").mkdir(parents=True)
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
    (app_repo / "apps" / "hello" / "scripts" / "check.py").write_text(
        """import json
import sys

print(json.dumps({"argv": sys.argv[1:], "message": "cli check"}))
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


def test_apps_install_and_show_installed_json(repo, hub_env, tmp_path: Path) -> None:
    app_repo = _create_app_repo(tmp_path)
    _configure_apps_repo(hub_env.hub_root, app_repo)

    install_result = runner.invoke(
        app,
        ["apps", "install", "local:apps/hello", "--repo", str(repo), "--json"],
    )

    assert install_result.exit_code == 0
    install_payload = json.loads(install_result.output)
    assert install_payload["changed"] is True
    assert install_payload["app"]["app_id"] == "local.hello"
    assert install_payload["app"]["source_ref_string"] == "local:apps/hello@main"
    assert install_payload["app"]["bundle_verified"] is True

    installed_result = runner.invoke(
        app,
        ["apps", "installed", "--repo", str(repo), "--json"],
    )

    assert installed_result.exit_code == 0
    installed_payload = json.loads(installed_result.output)
    assert installed_payload["count"] == 1
    assert installed_payload["apps"][0]["app_id"] == "local.hello"

    show_result = runner.invoke(
        app,
        ["apps", "show", "local.hello", "--repo", str(repo), "--json"],
    )

    assert show_result.exit_code == 0
    show_payload = json.loads(show_result.output)
    assert show_payload["app_id"] == "local.hello"
    assert show_payload["bundle_verified"] is True
    assert show_payload["manifest"]["name"] == "Hello App"


def test_apps_tools_json(repo, hub_env, tmp_path: Path) -> None:
    app_repo = _create_app_repo(tmp_path)
    _configure_apps_repo(hub_env.hub_root, app_repo)
    runner.invoke(
        app,
        ["apps", "install", "local:apps/hello", "--repo", str(repo), "--json"],
    )

    result = runner.invoke(
        app,
        ["apps", "tools", "local.hello", "--repo", str(repo), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["app_id"] == "local.hello"
    assert payload["count"] == 1
    assert payload["tools"][0]["tool_id"] == "check"


def test_apps_run_json(repo, hub_env, tmp_path: Path) -> None:
    app_repo = _create_app_repo(tmp_path)
    _configure_apps_repo(hub_env.hub_root, app_repo)
    runner.invoke(
        app,
        ["apps", "install", "local:apps/hello", "--repo", str(repo), "--json"],
    )

    result = runner.invoke(
        app,
        [
            "apps",
            "run",
            "local.hello",
            "check",
            "--repo",
            str(repo),
            "--json",
            "--",
            "alpha",
            "beta",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["app_id"] == "local.hello"
    assert payload["tool_id"] == "check"
    assert payload["exit_code"] == 0
    assert payload["argv"][-2:] == ["alpha", "beta"]
    assert "cli check" in payload["stdout_excerpt"]
