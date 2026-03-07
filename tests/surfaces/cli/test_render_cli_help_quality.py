from __future__ import annotations

import re

from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_render_screenshot_help_mentions_mode_readiness_and_cleanup() -> None:
    result = runner.invoke(app, ["render", "screenshot", "--help"])
    output = _plain(result.stdout)

    assert result.exit_code == 0
    assert "--url" in output
    assert "--serve-cmd" in output
    assert "--ready-url" in output
    assert "--project-root" in output
    assert "--project-context" in output
    assert "no-project-conte" in output
    assert "tears it down on" in output
    assert "every exit path." in output


def test_render_demo_help_mentions_manifest_and_artifacts_options() -> None:
    result = runner.invoke(app, ["render", "demo", "--help"])
    output = _plain(result.stdout)

    assert result.exit_code == 0
    assert "--script" in output
    assert "Locator priority" in output
    assert "--record-video" in output
    assert "--trace" in output
    assert "--preflight" in output
    assert "--preflight-only" in output
    assert "--preflight-report" in output
    assert "--session-id" in output
    assert "--keep-session" in output
    assert "--attach-session" in output
    assert "--full-artifacts" in output
    assert "--project-root" in output
    assert "--project-context" in output
    assert "no-project-conte" in output


def test_render_observe_help_mentions_serve_mode_readiness_and_cleanup() -> None:
    result = runner.invoke(app, ["render", "observe", "--help"])
    output = _plain(result.stdout)

    assert result.exit_code == 0
    assert "--serve-cmd" in output
    assert "--ready-url" in output
    assert "--project-root" in output
    assert "--project-context" in output
    assert "no-project-conte" in output
    assert "tears it down on" in output
    assert "every exit path." in output
