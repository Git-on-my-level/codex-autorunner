from __future__ import annotations

from typer.testing import CliRunner

from codex_autorunner.cli import app


def test_render_group_is_present_in_root_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "render" in result.stdout


def test_render_subcommands_are_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["render", "--help"])
    assert result.exit_code == 0
    assert "screenshot" in result.stdout
    assert "demo" in result.stdout
    assert "observe" in result.stdout


def test_render_demo_help_documents_manifest_steps() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["render", "demo", "--help"])
    assert result.exit_code == 0
    assert "version: 1" in result.stdout
    assert "snapshot_a11y" in result.stdout
