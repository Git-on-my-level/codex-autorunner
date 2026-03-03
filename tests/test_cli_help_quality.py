from __future__ import annotations

from collections.abc import Iterator

import click
from typer.main import get_command
from typer.testing import CliRunner

from codex_autorunner.cli import app


def _iter_cli_commands() -> Iterator[tuple[tuple[str, ...], click.Command]]:
    root = get_command(app)

    def _walk(
        command: click.Command, path: tuple[str, ...]
    ) -> Iterator[tuple[tuple[str, ...], click.Command]]:
        yield path, command
        if isinstance(command, click.MultiCommand):
            ctx = click.Context(command)
            for name in command.list_commands(ctx):
                child = command.get_command(ctx, name)
                if child is None:
                    continue
                yield from _walk(child, path + (name,))

    yield from _walk(root, tuple())


def test_cli_all_commands_have_help_summary() -> None:
    missing: list[str] = []
    for path, command in _iter_cli_commands():
        if not path:
            continue
        help_text = (command.help or "").strip()
        short_help = (command.short_help or "").strip()
        if not help_text and not short_help:
            missing.append("car " + " ".join(path))

    assert not missing, "Missing help summary for:\n" + "\n".join(sorted(missing))


def test_cli_all_options_have_help_text() -> None:
    missing: list[str] = []
    for path, command in _iter_cli_commands():
        for param in command.params:
            if not isinstance(param, click.Option):
                continue
            if param.hidden or param.name == "help":
                continue
            if (param.help or "").strip():
                continue
            option_names = ", ".join(param.opts) if param.opts else f"<{param.name}>"
            cmd = "car" if not path else "car " + " ".join(path)
            missing.append(f"{cmd}: {option_names}")

    assert not missing, "Missing option help text for:\n" + "\n".join(sorted(missing))


def test_cli_destructive_commands_include_safety_notes() -> None:
    runner = CliRunner()
    destructive_paths = [
        ("kill",),
        ("hub", "worktree", "cleanup"),
        ("hub", "worktree", "archive"),
        ("hub", "inbox", "clear"),
        ("hub", "runs", "cleanup"),
        ("ticket-flow", "archive"),
    ]

    for path in destructive_paths:
        result = runner.invoke(app, [*path, "--help"])
        assert result.exit_code == 0
        assert (
            "Safety:" in result.stdout
        ), f"Expected safety note in: car {' '.join(path)} --help"


def test_cli_help_shows_examples_on_key_commands() -> None:
    runner = CliRunner()
    with_examples = [
        ("hub", "dispatch", "reply"),
        ("templates", "fetch"),
        ("templates", "apply"),
        ("hub", "destination", "set"),
    ]

    for path in with_examples:
        result = runner.invoke(app, [*path, "--help"])
        assert result.exit_code == 0
        assert (
            "Example" in result.stdout
        ), f"Expected an example in: car {' '.join(path)} --help"


def test_cli_alias_help_points_to_canonical_commands() -> None:
    runner = CliRunner()

    template_help = runner.invoke(app, ["template", "--help"])
    assert template_help.exit_code == 0
    assert "canonical form: `car templates ...`" in template_help.stdout

    ticket_flow_alias_help = runner.invoke(app, ["flow", "ticket_flow", "--help"])
    assert ticket_flow_alias_help.exit_code == 0
    assert "canonical: `car ticket-flow ...`" in ticket_flow_alias_help.stdout
