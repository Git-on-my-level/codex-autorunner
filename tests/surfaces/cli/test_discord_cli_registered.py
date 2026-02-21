from __future__ import annotations

from typer.testing import CliRunner

from codex_autorunner.cli import app


def test_discord_cli_help_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["discord", "--help"])
    assert result.exit_code == 0
    assert "start" in result.stdout
    assert "health" in result.stdout
    assert "register-commands" in result.stdout
