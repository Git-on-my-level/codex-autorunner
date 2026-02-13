from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.surfaces.cli.cli import _find_hub_server_process

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


def test_find_hub_server_process_matches_root_serve_without_explicit_port() -> None:
    ps_output = "1234 car serve --path /tmp/hub\n"
    with patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["ps"], returncode=0, stdout=ps_output, stderr=""
        ),
    ):
        detected = _find_hub_server_process(port=4517)
    assert detected is not None
    assert detected["pid"] == 1234
    assert "car serve" in detected["command"]
