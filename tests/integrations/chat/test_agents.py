from codex_autorunner.integrations.chat.agents import (
    DEFAULT_CHAT_AGENT,
    build_agent_switch_state,
    chat_agent_supports_effort,
    default_chat_model_for_agent,
    normalize_chat_agent,
)


def test_normalize_chat_agent_accepts_compact_values() -> None:
    assert normalize_chat_agent(" OpenCode ") == "opencode"
    assert normalize_chat_agent("Open Code") == "opencode"
    assert normalize_chat_agent("codex") == "codex"


def test_normalize_chat_agent_falls_back_to_default() -> None:
    assert normalize_chat_agent(None, default=DEFAULT_CHAT_AGENT) == "codex"
    assert normalize_chat_agent("unknown", default=DEFAULT_CHAT_AGENT) == "codex"


def test_build_agent_switch_state_clears_model_and_effort_for_override_surfaces() -> (
    None
):
    state = build_agent_switch_state("opencode", model_reset="clear")

    assert state.agent == "opencode"
    assert state.model is None
    assert state.effort is None


def test_build_agent_switch_state_applies_agent_default_model() -> None:
    state = build_agent_switch_state("codex", model_reset="agent_default")

    assert state.agent == "codex"
    assert state.model == default_chat_model_for_agent("codex")
    assert state.effort is None


def test_chat_agent_supports_effort_only_for_codex() -> None:
    assert chat_agent_supports_effort("codex") is True
    assert chat_agent_supports_effort("opencode") is False
