from __future__ import annotations

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core import optional_dependencies


def test_render_missing_playwright_shows_optional_dependency_hint(
    monkeypatch,
) -> None:
    original_find_spec = optional_dependencies.importlib.util.find_spec

    def fake_find_spec(name: str):  # type: ignore[no-untyped-def]
        if name == "playwright":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(
        optional_dependencies.importlib.util, "find_spec", fake_find_spec
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["render", "screenshot", "--url", "http://127.0.0.1:3000"],
    )

    assert result.exit_code == 1
    assert "render requires optional dependencies (playwright)." in result.output
    assert "pip install codex-autorunner[browser]" in result.output
