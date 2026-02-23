from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.runtime import DoctorCheck, DoctorReport
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


def test_doctor_json_includes_chat_parity_contract_checks(
    monkeypatch, hub_root_only: Path
) -> None:
    from codex_autorunner.core.utils import RepoNotFoundError
    from codex_autorunner.surfaces.cli.commands import doctor as doctor_cmd

    class _StubHubConfig:
        def __init__(self) -> None:
            self.raw = {}

    def _raise_repo_not_found(_start: Path) -> Path:
        raise RepoNotFoundError("not found")

    monkeypatch.setattr(doctor_cmd, "doctor", lambda _start: DoctorReport(checks=[]))
    monkeypatch.setattr(doctor_cmd, "load_hub_config", lambda _start: _StubHubConfig())
    monkeypatch.setattr(doctor_cmd, "find_repo_root", _raise_repo_not_found)
    monkeypatch.setattr(doctor_cmd, "telegram_doctor_checks", lambda *_a, **_k: [])
    monkeypatch.setattr(doctor_cmd, "discord_doctor_checks", lambda *_a, **_k: [])
    monkeypatch.setattr(doctor_cmd, "pma_doctor_checks", lambda *_a, **_k: [])
    monkeypatch.setattr(doctor_cmd, "hub_worktree_doctor_checks", lambda *_a, **_k: [])
    monkeypatch.setattr(
        doctor_cmd,
        "chat_doctor_checks",
        lambda **_k: [
            DoctorCheck(
                name="Chat parity contract",
                passed=True,
                message="ok",
                severity="info",
                check_id="chat.parity_contract",
            )
        ],
    )

    result = runner.invoke(app, ["doctor", "--repo", str(hub_root_only), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    check_ids = [check.get("check_id") for check in payload.get("checks", [])]
    assert "chat.parity_contract" in check_ids
