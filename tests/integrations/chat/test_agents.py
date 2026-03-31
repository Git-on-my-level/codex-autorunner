from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.chat.agents import (
    DEFAULT_CHAT_AGENT,
    build_agent_switch_state,
    chat_agent_command_choices,
    chat_agent_definitions,
    chat_agent_description,
    chat_agent_supports_effort,
    chat_hermes_profile_options,
    default_chat_model_for_agent,
    normalize_chat_agent,
    resolve_chat_agent_and_profile,
    resolve_chat_runtime_agent,
    valid_chat_agent_values,
)


def test_normalize_chat_agent_accepts_registered_agents() -> None:
    assert normalize_chat_agent(" OpenCode ") == "opencode"
    assert normalize_chat_agent("Open Code") == "opencode"
    assert normalize_chat_agent("codex") == "codex"
    assert normalize_chat_agent("hermes") == "hermes"
    assert normalize_chat_agent(" Hermes ") == "hermes"


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


def test_chat_agent_supports_effort_for_review_capable_agents() -> None:
    assert chat_agent_supports_effort("codex") is True
    assert chat_agent_supports_effort("opencode") is True
    assert chat_agent_supports_effort("hermes") is False
    assert chat_agent_supports_effort("zeroclaw") is False


def test_chat_agent_command_choices_is_registry_driven() -> None:
    choices = chat_agent_command_choices()
    values = {c["value"] for c in choices}
    assert "codex" in values
    assert "opencode" in values
    assert "hermes" in values
    assert all("name" in c and "value" in c for c in choices)


def test_chat_agent_description_includes_registered_agents() -> None:
    description = chat_agent_description()
    assert "codex" in description
    assert "opencode" in description
    assert "hermes" in description


def test_hermes_is_accepted_by_normalize_chat_agent() -> None:
    assert normalize_chat_agent("hermes") == "hermes"
    assert normalize_chat_agent("HERMES") == "hermes"
    assert normalize_chat_agent(" Hermes ") == "hermes"


def test_normalize_chat_agent_accepts_registered_command_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        lambda context=None: {
            "plugin-agent": SimpleNamespace(name="Plugin Agent"),
        },
    )

    assert normalize_chat_agent("plugin-agent") == "plugin-agent"
    choices = chat_agent_command_choices()
    assert {"name": "plugin-agent", "value": "plugin-agent"} in choices


def test_chat_agent_definitions_keep_builtins_first_and_append_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        lambda context=None: {
            "plugin-agent": SimpleNamespace(name="Plugin Agent"),
        },
    )

    definitions = chat_agent_definitions()

    assert [definition.value for definition in definitions[:4]] == [
        "codex",
        "opencode",
        "hermes",
        "zeroclaw",
    ]
    assert definitions[-1].value == "plugin-agent"
    assert definitions[-1].description == "Plugin Agent"
    assert valid_chat_agent_values()[-1] == "plugin-agent"


def test_resolve_chat_agent_and_profile_uses_context_for_config_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _registered(context=None):
        if context != "repo-root":
            return {}
        return {
            "hermes-m4-pma": SimpleNamespace(name="Hermes (hermes-m4-pma)"),
        }

    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        _registered,
    )

    assert normalize_chat_agent("hermes-m4-pma") is None
    assert chat_hermes_profile_options("repo-root")[0].profile == "m4-pma"
    assert resolve_chat_agent_and_profile("hermes-m4-pma", context="repo-root") == (
        "hermes",
        "m4-pma",
    )
    assert (
        resolve_chat_runtime_agent(
            "hermes",
            "m4-pma",
            context="repo-root",
        )
        == "hermes-m4-pma"
    )
