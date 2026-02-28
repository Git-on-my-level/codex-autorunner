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
        "new",
        "newt",
        "debug",
        "agent",
        "model",
        "update",
        "help",
        "ids",
        "diff",
        "skills",
        "mcp",
        "init",
        "repos",
        "review",
        "approvals",
        "mention",
        "experimental",
        "rollout",
        "feedback",
        "session",
        "files",
        "flow",
    ]
    assert [opt["name"] for opt in options] == expected_subcommands

    session = _find_option(options, "session")
    session_options = session["options"]
    assert [opt["name"] for opt in session_options] == [
        "resume",
        "reset",
        "compact",
        "interrupt",
        "logout",
    ]

    flow = _find_option(options, "flow")
    flow_options = flow["options"]
    assert [opt["name"] for opt in flow_options] == [
        "status",
        "runs",
        "issue",
        "plan",
        "resume",
        "stop",
        "archive",
        "recover",
        "reply",
    ]

    pma = next(cmd for cmd in commands if cmd["name"] == "pma")
    assert pma["type"] == 1
    pma_options = pma["options"]
    assert [opt["name"] for opt in pma_options] == [
        "on",
        "off",
        "status",
        "targets",
        "target",
        "thread",
    ]

    pma_target = _find_option(pma_options, "target")
    assert [opt["name"] for opt in pma_target["options"]] == ["add", "rm", "clear"]
    pma_thread = _find_option(pma_options, "thread")
    assert [opt["name"] for opt in pma_thread["options"]] == [
        "list",
        "info",
        "archive",
        "resume",
    ]


def test_required_options_are_marked_required() -> None:
    commands = build_application_commands()
    car_options = commands[0]["options"]

    bind = _find_option(car_options, "bind")
    bind_workspace = _find_option(bind["options"], "workspace")
    assert bind_workspace["required"] is False
    update = _find_option(car_options, "update")
    update_target = _find_option(update["options"], "target")
    assert update_target["required"] is False

    flow = _find_option(car_options, "flow")
    flow_issue = _find_option(flow["options"], "issue")
    flow_issue_ref = _find_option(flow_issue["options"], "issue_ref")
    assert flow_issue_ref["required"] is True

    flow_plan = _find_option(flow["options"], "plan")
    flow_plan_text = _find_option(flow_plan["options"], "text")
    assert flow_plan_text["required"] is True

    flow_recover = _find_option(flow["options"], "recover")
    flow_recover_run_id = _find_option(flow_recover["options"], "run_id")
    assert flow_recover_run_id["required"] is False
    flow_reply = _find_option(flow["options"], "reply")
    text_option = _find_option(flow_reply["options"], "text")
    run_id_option = _find_option(flow_reply["options"], "run_id")

    assert text_option["required"] is True
    assert run_id_option["required"] is False

    pma_options = commands[1]["options"]
    target_group = _find_option(pma_options, "target")
    add_ref = _find_option(
        _find_option(target_group["options"], "add")["options"], "ref"
    )
    rm_ref = _find_option(_find_option(target_group["options"], "rm")["options"], "ref")
    assert add_ref["required"] is True
    assert rm_ref["required"] is True

    thread_group = _find_option(pma_options, "thread")
    info_id = _find_option(
        _find_option(thread_group["options"], "info")["options"], "id"
    )
    archive_id = _find_option(
        _find_option(thread_group["options"], "archive")["options"], "id"
    )
    resume_options = _find_option(thread_group["options"], "resume")["options"]
    resume_id = _find_option(resume_options, "id")
    resume_backend_id = _find_option(resume_options, "backend_id")
    assert info_id["required"] is True
    assert archive_id["required"] is True
    assert resume_id["required"] is True
    assert resume_backend_id["required"] is True
