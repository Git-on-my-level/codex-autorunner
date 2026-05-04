from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from codex_autorunner.surfaces.cli.output import echo_json, exit_with_error


def test_echo_json_uses_pretty_cli_default() -> None:
    app = typer.Typer(add_completion=False)

    @app.command()
    def dump() -> None:
        echo_json({"items": [{"name": "alpha"}]})

    result = CliRunner().invoke(app)

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"items": [{"name": "alpha"}]}
    assert result.stdout.startswith('{\n  "items": [')


def test_exit_with_error_writes_stderr_and_exits_code_1(monkeypatch) -> None:
    emitted: list[tuple[str, bool]] = []

    def fake_echo(message: str, *, err: bool = False) -> None:
        emitted.append((message, err))

    monkeypatch.setattr(typer, "echo", fake_echo)

    with pytest.raises(typer.Exit) as exc_info:
        exit_with_error("Error: boom")

    assert exc_info.value.exit_code == 1
    assert emitted == [("Error: boom", True)]
