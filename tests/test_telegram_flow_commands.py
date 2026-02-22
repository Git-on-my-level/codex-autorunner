from codex_autorunner.integrations.telegram.handlers.commands.flows import (
    _normalize_flow_action,
    _split_flow_action,
)


def test_split_flow_action_empty() -> None:
    assert _split_flow_action("") == ("", "")


def test_split_flow_action_returns_remainder() -> None:
    action, remainder = _split_flow_action("reply hello world")
    assert action == "reply"
    assert remainder == "hello world"


def test_normalize_flow_action_defaults_to_help() -> None:
    assert _normalize_flow_action("") == "help"


def test_normalize_flow_action_returns_action_as_is() -> None:
    assert _normalize_flow_action("start") == "start"
