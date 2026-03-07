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
        "start",
        "restart",
        "resume",
        "stop",
        "archive",
        "recover",
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
    assert bind_workspace["autocomplete"] is True

    model = _find_option(car_options, "model")
    model_name = _find_option(model["options"], "name")
    assert model_name["required"] is False
    assert model_name["autocomplete"] is True

    session = _find_option(car_options, "session")
    session_resume = _find_option(session["options"], "resume")
    session_thread_id = _find_option(session_resume["options"], "thread_id")
    assert session_thread_id["required"] is False
    assert session_thread_id["autocomplete"] is True

    update = _find_option(car_options, "update")
    update_target = _find_option(update["options"], "target")
    assert update_target["required"] is False

    flow = _find_option(car_options, "flow")
    for flow_name in ("status", "restart", "resume", "stop", "archive", "recover"):
        flow_command = _find_option(flow["options"], flow_name)
        flow_run_id = _find_option(flow_command["options"], "run_id")
        assert flow_run_id["required"] is False
        assert flow_run_id["autocomplete"] is True

    flow_issue = _find_option(flow["options"], "issue")
    flow_issue_ref = _find_option(flow_issue["options"], "issue_ref")
    assert flow_issue_ref["required"] is True

    flow_plan = _find_option(flow["options"], "plan")
    flow_plan_text = _find_option(flow_plan["options"], "text")
    assert flow_plan_text["required"] is True

    flow_start = _find_option(flow["options"], "start")
    flow_start_force_new = _find_option(flow_start["options"], "force_new")
    assert flow_start_force_new["required"] is False

    flow_restart = _find_option(flow["options"], "restart")
    flow_restart_run_id = _find_option(flow_restart["options"], "run_id")
    assert flow_restart_run_id["required"] is False

    flow_recover = _find_option(flow["options"], "recover")
    flow_recover_run_id = _find_option(flow_recover["options"], "run_id")
    assert flow_recover_run_id["required"] is False
    flow_reply = _find_option(flow["options"], "reply")
    text_option = _find_option(flow_reply["options"], "text")
    run_id_option = _find_option(flow_reply["options"], "run_id")

    assert text_option["required"] is True
    assert run_id_option["required"] is False
    assert run_id_option["autocomplete"] is True

    pma_options = commands[1]["options"]
    assert [opt["name"] for opt in pma_options] == ["on", "off", "status"]


def test_agent_and_effort_options_include_choices() -> None:
    commands = build_application_commands()
    car_options = commands[0]["options"]

    agent = _find_option(car_options, "agent")
    agent_name = _find_option(agent["options"], "name")
    agent_choices = {choice["value"] for choice in agent_name.get("choices", [])}
    assert agent_choices == {"codex", "opencode"}

    model = _find_option(car_options, "model")
    model_effort = _find_option(model["options"], "effort")
    effort_choices = {choice["value"] for choice in model_effort.get("choices", [])}
    assert effort_choices == {"none", "minimal", "low", "medium", "high", "xhigh"}

    update = _find_option(car_options, "update")
    update_target = _find_option(update["options"], "target")
    target_choices = {choice["value"] for choice in update_target.get("choices", [])}
    assert target_choices == {"both", "web", "chat", "telegram", "discord", "status"}

    experimental = _find_option(car_options, "experimental")
    experimental_action = _find_option(experimental["options"], "action")
    action_choices = {
        choice["value"] for choice in experimental_action.get("choices", [])
    }
    assert action_choices == {"list", "enable", "disable"}
