from __future__ import annotations

from types import SimpleNamespace

import pytest

from codex_autorunner.agents.hermes_identity import (
    CanonicalHermesIdentity,
    canonicalize_hermes_identity,
    is_hermes_alias_agent,
)


class TestIsHermesAliasAgent:
    def test_plain_hermes_is_not_alias(self) -> None:
        assert is_hermes_alias_agent("hermes") is False

    def test_hermes_uppercase_is_not_alias(self) -> None:
        assert is_hermes_alias_agent("HERMES") is False

    def test_dash_alias_is_detected(self) -> None:
        assert is_hermes_alias_agent("hermes-m4-pma") is True

    def test_underscore_alias_is_detected(self) -> None:
        assert is_hermes_alias_agent("hermes_m4_pma") is True

    def test_non_hermes_agent_is_not_alias(self) -> None:
        assert is_hermes_alias_agent("codex") is False
        assert is_hermes_alias_agent("opencode") is False

    def test_whitespace_handling(self) -> None:
        assert is_hermes_alias_agent("  hermes-m4-pma  ") is True

    def test_empty_string_is_not_alias(self) -> None:
        assert is_hermes_alias_agent("") is False


class TestCanonicalizeHermesIdentity:
    def test_plain_hermes_with_no_profile_passes_through(self) -> None:
        result = canonicalize_hermes_identity("hermes")
        assert result == CanonicalHermesIdentity(agent="hermes", profile=None)

    def test_plain_hermes_with_profile_passes_through(self) -> None:
        result = canonicalize_hermes_identity("hermes", "m4-pma")
        assert result == CanonicalHermesIdentity(agent="hermes", profile="m4-pma")

    def test_alias_agent_resolved_via_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codex_autorunner.agents import hermes_identity as mod

        monkeypatch.setattr(
            mod,
            "resolve_agent_runtime",
            lambda agent_id, profile=None, context=None: SimpleNamespace(
                logical_agent_id="hermes",
                logical_profile="m4-pma",
                runtime_agent_id="hermes-m4-pma",
                runtime_profile=None,
                resolution_kind="alias_profile",
            ),
        )

        result = canonicalize_hermes_identity("hermes-m4-pma")
        assert result.agent == "hermes"
        assert result.profile == "m4-pma"

    def test_alias_agent_with_explicit_profile_uses_runtime_resolution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codex_autorunner.agents import hermes_identity as mod

        monkeypatch.setattr(
            mod,
            "resolve_agent_runtime",
            lambda agent_id, profile=None, context=None: SimpleNamespace(
                logical_agent_id="hermes",
                logical_profile="other",
                runtime_agent_id="hermes-other",
                runtime_profile=None,
                resolution_kind="alias_profile",
            ),
        )

        result = canonicalize_hermes_identity("hermes-m4-pma", "other")
        assert result.agent == "hermes"
        assert result.profile == "other"

    def test_non_hermes_agent_passes_through(self) -> None:
        result = canonicalize_hermes_identity("codex")
        assert result.agent == "codex"
        assert result.profile is None

    def test_empty_agent_id_returns_as_is(self) -> None:
        result = canonicalize_hermes_identity("")
        assert result.agent == ""

    def test_resolution_failure_falls_back_to_syntactic_alias_parsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codex_autorunner.agents import hermes_identity as mod

        def _fail(*args, **kwargs):
            raise ValueError("no registry")

        monkeypatch.setattr(mod, "resolve_agent_runtime", _fail)

        result = canonicalize_hermes_identity("hermes-m4-pma")
        assert result.agent == "hermes"
        assert result.profile == "m4-pma"
