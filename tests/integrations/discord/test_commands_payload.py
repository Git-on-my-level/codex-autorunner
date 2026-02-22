from __future__ import annotations

from codex_autorunner.integrations.discord.commands import build_application_commands


def _find_option(options: list[dict], name: str) -> dict:
    for option in options:
        if option.get("name") == name:
            return option
    raise AssertionError(f"Option not found: {name}")


def test_build_application_commands_structure_is_stable() -> None:
    commands = build_application_commands()
    assert len(commands) == 1

    car = commands[0]
    assert car["name"] == "car"
    assert car["type"] == 1

    options = car["options"]
    assert [opt["name"] for opt in options] == ["bind", "status", "flow"]

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


def test_required_options_are_marked_required() -> None:
    commands = build_application_commands()
    car_options = commands[0]["options"]

    bind = _find_option(car_options, "bind")
    bind_path = _find_option(bind["options"], "path")
    assert bind_path["required"] is True

    flow = _find_option(car_options, "flow")
    flow_reply = _find_option(flow["options"], "reply")
    text_option = _find_option(flow_reply["options"], "text")
    run_id_option = _find_option(flow_reply["options"], "run_id")

    assert text_option["required"] is True
    assert run_id_option["required"] is False
