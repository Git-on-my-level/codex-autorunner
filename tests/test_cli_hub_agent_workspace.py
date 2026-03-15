import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app

runner = CliRunner()


def test_hub_agent_workspace_cli_create_show_and_remove_keep_files(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    create_result = runner.invoke(
        app,
        [
            "hub",
            "agent-workspace",
            "create",
            "zc-main",
            "--runtime",
            "zeroclaw",
            "--disabled",
            "--path",
            str(hub_root),
        ],
    )
    assert create_result.exit_code == 0
    workspace_path = (
        hub_root / ".codex-autorunner" / "runtimes" / "zeroclaw" / "zc-main"
    )
    assert workspace_path.exists()

    show_result = runner.invoke(
        app,
        [
            "hub",
            "agent-workspace",
            "show",
            "zc-main",
            "--json",
            "--path",
            str(hub_root),
        ],
    )
    assert show_result.exit_code == 0
    payload = json.loads(show_result.output)
    assert payload["id"] == "zc-main"
    assert payload["runtime"] == "zeroclaw"
    assert payload["enabled"] is False

    remove_result = runner.invoke(
        app,
        [
            "hub",
            "agent-workspace",
            "remove",
            "zc-main",
            "--json",
            "--path",
            str(hub_root),
        ],
    )
    assert remove_result.exit_code == 0
    assert json.loads(remove_result.output) == {
        "status": "ok",
        "workspace_id": "zc-main",
        "delete_dir": False,
    }
    assert workspace_path.exists()


def test_hub_agent_workspace_show_reports_runtime_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    runner.invoke(
        app,
        [
            "hub",
            "agent-workspace",
            "create",
            "zc-main",
            "--runtime",
            "zeroclaw",
            "--disabled",
            "--path",
            str(hub_root),
        ],
        catch_exceptions=False,
    )

    monkeypatch.setattr(
        "codex_autorunner.core.hub.probe_agent_workspace_runtime",
        lambda _config, _workspace: {
            "runtime_id": "zeroclaw",
            "status": "incompatible",
            "version": "0.2.0",
            "message": "ZeroClaw CLI does not support --session-state-file",
            "fix": "Install a compatible ZeroClaw build.",
        },
    )

    show_result = runner.invoke(
        app,
        [
            "hub",
            "agent-workspace",
            "show",
            "zc-main",
            "--json",
            "--path",
            str(hub_root),
        ],
    )
    assert show_result.exit_code == 0
    payload = json.loads(show_result.output)
    assert payload["readiness"]["status"] == "incompatible"
    assert payload["readiness"]["version"] == "0.2.0"


def test_hub_agent_workspace_create_fails_on_incompatible_preflight(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    monkeypatch.setattr(
        "codex_autorunner.core.hub.probe_agent_workspace_runtime",
        lambda _config, _workspace: {
            "status": "incompatible",
            "message": "ZeroClaw CLI is incompatible",
            "fix": "Install a compatible ZeroClaw build.",
        },
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "agent-workspace",
            "create",
            "zc-main",
            "--runtime",
            "zeroclaw",
            "--path",
            str(hub_root),
        ],
    )
    assert result.exit_code != 0
    assert "ZeroClaw CLI is incompatible" in result.output

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["agent_workspaces"] == []


def test_hub_agent_workspace_destination_set_updates_manifest(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    runner.invoke(
        app,
        [
            "hub",
            "agent-workspace",
            "create",
            "zc-main",
            "--runtime",
            "zeroclaw",
            "--disabled",
            "--path",
            str(hub_root),
        ],
        catch_exceptions=False,
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "agent-workspace",
            "destination",
            "set",
            "zc-main",
            "docker",
            "--image",
            "ghcr.io/acme/zeroclaw:latest",
            "--json",
            "--path",
            str(hub_root),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["effective_destination"] == {
        "kind": "docker",
        "image": "ghcr.io/acme/zeroclaw:latest",
    }
