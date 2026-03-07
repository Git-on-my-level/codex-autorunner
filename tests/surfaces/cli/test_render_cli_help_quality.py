from __future__ import annotations

from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()


def test_render_screenshot_help_mentions_mode_readiness_and_cleanup() -> None:
    result = runner.invoke(app, ["render", "screenshot", "--help"])

    assert result.exit_code == 0
    assert "--url" in result.stdout
    assert "--serve-cmd" in result.stdout
    assert "--ready-url" in result.stdout
    assert "CAR tears it down on" in result.stdout
    assert "every exit path." in result.stdout


def test_render_demo_help_mentions_manifest_and_artifacts_options() -> None:
    result = runner.invoke(app, ["render", "demo", "--help"])

    assert result.exit_code == 0
    assert "--script" in result.stdout
    assert "Locator priority" in result.stdout
    assert "--record-video" in result.stdout
    assert "--trace" in result.stdout


def test_render_observe_help_mentions_serve_mode_readiness_and_cleanup() -> None:
    result = runner.invoke(app, ["render", "observe", "--help"])

    assert result.exit_code == 0
    assert "--serve-cmd" in result.stdout
    assert "--ready-url" in result.stdout
    assert "CAR tears it down on" in result.stdout
    assert "every exit path." in result.stdout
