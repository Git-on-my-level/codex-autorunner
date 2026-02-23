from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
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
    assert "--dev" in result.stdout


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


def _stub_doctor_checks(monkeypatch):
    from codex_autorunner.core.utils import RepoNotFoundError
    from codex_autorunner.surfaces.cli.commands import doctor as doctor_cmd

    class _StubHubConfig:
        def __init__(self) -> None:
            self.raw = {}

    def _raise_repo_not_found(_start: Path) -> Path:
        raise RepoNotFoundError("not found")

    chat_calls: list[dict[str, object]] = []

    def _chat_doctor_checks(**kwargs):
        chat_calls.append(kwargs)
        return [
            DoctorCheck(
                name="Chat parity contract",
                passed=True,
                message="Chat parity contract ok",
                severity="info",
                check_id="chat.parity_contract",
            )
        ]

    monkeypatch.setattr(doctor_cmd, "doctor", lambda _start: DoctorReport(checks=[]))
    monkeypatch.setattr(doctor_cmd, "load_hub_config", lambda _start: _StubHubConfig())
    monkeypatch.setattr(doctor_cmd, "find_repo_root", _raise_repo_not_found)
    monkeypatch.setattr(doctor_cmd, "telegram_doctor_checks", lambda *_a, **_k: [])
    monkeypatch.setattr(doctor_cmd, "discord_doctor_checks", lambda *_a, **_k: [])
    monkeypatch.setattr(doctor_cmd, "pma_doctor_checks", lambda *_a, **_k: [])
    monkeypatch.setattr(doctor_cmd, "hub_worktree_doctor_checks", lambda *_a, **_k: [])
    monkeypatch.setattr(doctor_cmd, "chat_doctor_checks", _chat_doctor_checks)
    return chat_calls


def test_doctor_default_output_excludes_chat_parity_contract_checks(
    monkeypatch, hub_root_only: Path
) -> None:
    chat_calls = _stub_doctor_checks(monkeypatch)
    result = runner.invoke(app, ["doctor", "--repo", str(hub_root_only)])
    assert result.exit_code == 0, result.output
    assert "Chat parity contract ok" not in result.output
    assert chat_calls == []


@pytest.mark.parametrize(
    ("extra_args", "expect_chat_check", "expected_calls"),
    [
        ([], False, 0),
        (["--dev"], True, 1),
    ],
)
def test_doctor_json_chat_parity_contract_checks_require_dev_flag(
    monkeypatch,
    hub_root_only: Path,
    extra_args: list[str],
    expect_chat_check: bool,
    expected_calls: int,
) -> None:
    chat_calls = _stub_doctor_checks(monkeypatch)
    result = runner.invoke(
        app,
        ["doctor", "--repo", str(hub_root_only), "--json", *extra_args],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    check_ids = [check.get("check_id") for check in payload.get("checks", [])]
    assert ("chat.parity_contract" in check_ids) is expect_chat_check
    assert len(chat_calls) == expected_calls
