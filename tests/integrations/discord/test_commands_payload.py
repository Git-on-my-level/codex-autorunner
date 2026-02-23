from __future__ import annotations

from codex_autorunner.integrations.discord.commands import build_application_commands


def _find_option(options: list[dict], name: str) -> dict:
    for option in options:
        if option.get("name") == name:
            return option
    raise AssertionError(f"Option not found: {name}")


def test_build_application_commands_structure_is_stable() -> None:
    commands = build_application_commands()
    assert len(commands) == 2
    command_names = {cmd["name"] for cmd in commands}
    assert command_names == {"car", "pma"}

    car = next(cmd for cmd in commands if cmd["name"] == "car")
    assert car["type"] == 1

    options = car["options"]
    expected_subcommands = [
        "bind",
        "status",
        "debug",
        "agent",
        "model",
        "help",
        "ids",
        "diff",
        "skills",
        "mcp",
        "init",
        "repos",
        "files",
        "flow",
    ]
    assert [opt["name"] for opt in options] == expected_subcommands

    flow = _find_option(options, "flow")
    flow_options = flow["options"]
    assert [opt["name"] for opt in flow_options] == [
        "status",
        "runs",
        "resume",
        "stop",
        "archive",
        "reply",
    ]

    pma = next(cmd for cmd in commands if cmd["name"] == "pma")
    assert pma["type"] == 1
    pma_options = pma["options"]
    assert [opt["name"] for opt in pma_options] == ["on", "off", "status"]


def test_required_options_are_marked_required() -> None:
    commands = build_application_commands()
    car_options = commands[0]["options"]

    bind = _find_option(car_options, "bind")
    bind_workspace = _find_option(bind["options"], "workspace")
    assert bind_workspace["required"] is False

    flow = _find_option(car_options, "flow")
    flow_reply = _find_option(flow["options"], "reply")
    text_option = _find_option(flow_reply["options"], "text")
    run_id_option = _find_option(flow_reply["options"], "run_id")

    assert text_option["required"] is True
    assert run_id_option["required"] is False
