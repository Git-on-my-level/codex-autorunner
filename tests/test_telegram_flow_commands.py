from codex_autorunner.core.flows import (
    FLOW_ACTION_TOKENS,
    FLOW_ACTIONS_WITH_RUN_PICKER,
    normalize_flow_action,
)
from codex_autorunner.integrations.telegram.handlers.commands.flows import (
    _split_flow_action,
)


def test_split_flow_action_empty() -> None:
    assert _split_flow_action("") == ("", "")


def test_split_flow_action_returns_remainder() -> None:
    action, remainder = _split_flow_action("reply hello world")
    assert action == "reply"
    assert remainder == "hello world"


def test_normalize_flow_action_defaults_to_help() -> None:
    assert normalize_flow_action("") == "help"


def test_normalize_flow_action_canonicalizes_bootstrap_alias() -> None:
    assert normalize_flow_action("bootstrap") == "start"


def test_flow_action_catalog_exposes_picker_and_tokens() -> None:
    assert FLOW_ACTIONS_WITH_RUN_PICKER == frozenset(
        {"restart", "resume", "stop", "archive", "recover", "reply"}
    )
    assert "help" in FLOW_ACTION_TOKENS
    assert "bootstrap" in FLOW_ACTION_TOKENS
