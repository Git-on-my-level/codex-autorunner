from __future__ import annotations

from types import SimpleNamespace

from codex_autorunner.core.agent_model_defaults import resolve_model_for_agent


def test_settings_default_model_wins_over_config_and_builtin() -> None:
    state = SimpleNamespace(
        autorunner_model_overrides={"codex": "gpt-settings-default"}
    )
    config = SimpleNamespace(codex_model="gpt-config-default")

    assert (
        resolve_model_for_agent("codex", state=state, config=config)
        == "gpt-settings-default"
    )


def test_explicit_model_wins_over_settings_default() -> None:
    state = SimpleNamespace(
        autorunner_model_overrides={"opencode": "zai/settings-default"}
    )

    assert (
        resolve_model_for_agent("opencode", "zai/explicit", state=state)
        == "zai/explicit"
    )


def test_legacy_model_override_is_codex_only() -> None:
    state = SimpleNamespace(autorunner_model_override="gpt-legacy")

    assert resolve_model_for_agent("codex", state=state) == "gpt-legacy"
    assert resolve_model_for_agent("opencode", state=state) != "gpt-legacy"


def test_callers_can_avoid_builtin_when_blank_should_mean_runtime_default() -> None:
    assert resolve_model_for_agent("codex", include_builtin=False) is None
