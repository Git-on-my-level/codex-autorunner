from __future__ import annotations

import json

from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()


def test_doctor_versions_json_output(hub_root_only) -> None:
    result = runner.invoke(
        app,
        ["doctor", "versions", "--repo", str(hub_root_only), "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "cli" in payload
    assert "python" in payload
    assert "package" in payload
    assert "hub" in payload
    assert "mismatch" in payload


def test_doctor_help_lists_versions_subcommand() -> None:
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "versions" in result.stdout
