from __future__ import annotations

from typer.testing import CliRunner

from codex_autorunner.cli import app


def test_telegram_cli_chats_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["telegram", "--help"])
    assert result.exit_code == 0
    assert "chats" in result.stdout
