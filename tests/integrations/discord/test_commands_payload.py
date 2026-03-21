from __future__ import annotations

from codex_autorunner.core.flows import FLOW_ACTION_NAMES, FLOW_ACTION_SPECS
from codex_autorunner.core.update_targets import update_target_command_choices
from codex_autorunner.integrations.chat.agents import (
    chat_agent_command_choices,
    chat_agent_description,
)
from codex_autorunner.integrations.chat.model_selection import (
    reasoning_effort_command_choices,
    reasoning_effort_description,
)
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
        "tickets",
        "mcp",
        "init",
        "repos",
        "review",
        "approvals",
        "mention",
        "experimental",
        "rollout",
        "feedback",
        "archive",
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
    assert [opt["name"] for opt in flow_options] == list(FLOW_ACTION_NAMES)
    assert [(opt["name"], opt["description"]) for opt in flow_options] == [
        (spec.name, spec.description) for spec in FLOW_ACTION_SPECS
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

    skills = _find_option(car_options, "skills")
    skills_search = _find_option(skills["options"], "search")
    assert skills_search["required"] is False
    assert skills_search["autocomplete"] is True

    tickets = _find_option(car_options, "tickets")
    tickets_search = _find_option(tickets["options"], "search")
    assert tickets_search["required"] is False
    assert tickets_search["autocomplete"] is True

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
    assert agent_name["description"] == f"Agent name: {chat_agent_description()}"
    assert agent_name.get("choices", []) == list(chat_agent_command_choices())

    model = _find_option(car_options, "model")
    model_effort = _find_option(model["options"], "effort")
    assert model_effort["description"] == (
        f"Reasoning effort (codex only): {reasoning_effort_description()}"
    )
    assert model_effort.get("choices", []) == list(reasoning_effort_command_choices())

    update = _find_option(car_options, "update")
    update_target = _find_option(update["options"], "target")
    assert update_target["description"] == "Target service group or status"
    assert update_target.get("choices", []) == list(
        update_target_command_choices(include_status=True)
    )

    experimental = _find_option(car_options, "experimental")
    experimental_action = _find_option(experimental["options"], "action")
    action_choices = {
        choice["value"] for choice in experimental_action.get("choices", [])
    }
    assert action_choices == {"list", "enable", "disable"}
