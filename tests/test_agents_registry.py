from __future__ import annotations

import pytest

from codex_autorunner.agents.registry import (
    CAR_PLUGIN_API_VERSION,
    AgentDescriptor,
    _check_codex_health,
    _check_opencode_health,
    _make_codex_harness,
    _make_opencode_harness,
    get_available_agents,
    get_registered_agents,
    has_capability,
    validate_agent_id,
)


@pytest.fixture
def app_ctx():
    class MockContext:
        app_server_supervisor = object()
        app_server_events = object()
        opencode_supervisor = object()

    return MockContext()


@pytest.fixture
def app_ctx_codex_only():
    class MockContext:
        app_server_supervisor = object()
        app_server_events = object()
        opencode_supervisor = None

    return MockContext()


@pytest.fixture
def app_ctx_opencode_only():
    class MockContext:
        app_server_supervisor = None
        app_server_events = None
        opencode_supervisor = object()

    return MockContext()


@pytest.fixture
def app_ctx_missing_supervisors():
    class MockContext:
        app_server_supervisor = None
        app_server_events = None
        opencode_supervisor = None

    return MockContext()


class TestValidateAgentId:
    def test_valid_agent_ids(self):
        assert validate_agent_id("codex") == "codex"
        assert validate_agent_id("opencode") == "opencode"

    def test_case_insensitive(self):
        assert validate_agent_id("CODEX") == "codex"
        assert validate_agent_id("OPENCODE") == "opencode"
        assert validate_agent_id("CoDeX") == "codex"
        assert validate_agent_id("OpEnCoDe") == "opencode"

    def test_whitespace_trimming(self):
        assert validate_agent_id("  codex  ") == "codex"
        assert validate_agent_id("\topencode\n") == "opencode"

    def test_none_input(self):
        with pytest.raises(ValueError, match="Unknown agent"):
            validate_agent_id(None)

    def test_whitespace_only(self):
        with pytest.raises(ValueError, match="Unknown agent"):
            validate_agent_id("   ")
        with pytest.raises(ValueError, match="Unknown agent"):
            validate_agent_id("\t\n")

    def test_invalid_agent_id(self):
        with pytest.raises(ValueError, match="Unknown agent.*'invalid'"):
            validate_agent_id("invalid")
        with pytest.raises(ValueError, match="Unknown agent.*'foo'"):
            validate_agent_id("foo")


class TestHasCapability:
    def test_valid_capabilities_codex(self):
        assert has_capability("codex", "threads") is True
        assert has_capability("codex", "turns") is True
        assert has_capability("codex", "review") is True
        assert has_capability("codex", "model_listing") is True
        assert has_capability("codex", "event_streaming") is True
        assert has_capability("codex", "approvals") is True

    def test_valid_capabilities_opencode(self):
        assert has_capability("opencode", "threads") is True
        assert has_capability("opencode", "turns") is True
        assert has_capability("opencode", "review") is True
        assert has_capability("opencode", "model_listing") is True
        assert has_capability("opencode", "event_streaming") is True

    def test_nonexistent_capability(self):
        assert has_capability("codex", "invalid_capability") is False

    def test_nonexistent_agent(self):
        assert has_capability("invalid_agent", "threads") is False
        assert has_capability("invalid_agent", "invalid_capability") is False

    def test_opencode_doesnt_have_approvals(self):
        assert has_capability("opencode", "approvals") is False


class _StubEntryPoint:
    def __init__(self, obj):
        self._obj = obj
        self.group = "codex_autorunner.agent_backends"
        self.name = "stub"

    def load(self):
        return self._obj


def _run_with_entrypoints(monkeypatch, entrypoints):
    """Reload registry with supplied entry points for plugin tests."""

    def _select(_group):
        return entrypoints

    import codex_autorunner.agents.registry as registry

    monkeypatch.setattr(registry, "_select_entry_points", _select)
    registry.reload_agents()
    return registry


class TestGetRegisteredAgents:
    def test_returns_dict(self):
        agents = get_registered_agents()
        assert isinstance(agents, dict)

    def test_returns_copy(self):
        agents1 = get_registered_agents()
        agents2 = get_registered_agents()
        assert agents1 is not agents2

    def test_contains_codex_and_opencode(self):
        agents = get_registered_agents()
        assert "codex" in agents
        assert "opencode" in agents

    def test_agent_descriptor_structure(self):
        agents = get_registered_agents()
        assert isinstance(agents["codex"], AgentDescriptor)
        assert isinstance(agents["opencode"], AgentDescriptor)

    def test_agent_properties(self):
        agents = get_registered_agents()
        assert agents["codex"].id == "codex"
        assert agents["codex"].name == "Codex"
        assert agents["opencode"].id == "opencode"
        assert agents["opencode"].name == "OpenCode"


class TestGetAvailableAgents:
    def test_both_agents_available(self, app_ctx):
        available = get_available_agents(app_ctx)
        assert "codex" in available
        assert "opencode" in available
        assert len(available) == 2

    def test_codex_only_available(self, app_ctx_codex_only):
        available = get_available_agents(app_ctx_codex_only)
        assert "codex" in available
        assert "opencode" not in available
        assert len(available) == 1

    def test_opencode_only_available(self, app_ctx_opencode_only):
        available = get_available_agents(app_ctx_opencode_only)
        assert "codex" not in available
        assert "opencode" in available
        assert len(available) == 1

    def test_no_agents_available(self, app_ctx_missing_supervisors):
        available = get_available_agents(app_ctx_missing_supervisors)
        assert "codex" not in available
        assert "opencode" not in available
        assert len(available) == 0

    def test_none_context_raises_attribute_error(self):
        with pytest.raises(AttributeError):
            get_available_agents(None)

    def test_malformed_context_missing_attributes_raises(self):
        class BadContext:
            pass

        with pytest.raises(AttributeError):
            get_available_agents(BadContext())


class TestCheckCodexHealth:
    def test_healthy_context(self, app_ctx):
        assert _check_codex_health(app_ctx) is True

    def test_missing_supervisor(self, app_ctx_missing_supervisors):
        assert _check_codex_health(app_ctx_missing_supervisors) is False

    def test_none_supervisor(self):
        class NoneSupervisorContext:
            app_server_supervisor = None

        assert _check_codex_health(NoneSupervisorContext()) is False


class TestCheckOpenCodeHealth:
    def test_healthy_context(self, app_ctx):
        assert _check_opencode_health(app_ctx) is True

    def test_missing_supervisor(self, app_ctx_missing_supervisors):
        assert _check_opencode_health(app_ctx_missing_supervisors) is False

    def test_none_supervisor(self):
        class NoneSupervisorContext:
            opencode_supervisor = None

        assert _check_opencode_health(NoneSupervisorContext()) is False


class TestMakeCodexHarness:
    def test_valid_context(self, app_ctx):
        from codex_autorunner.agents.codex.harness import CodexHarness

        harness = _make_codex_harness(app_ctx)
        assert isinstance(harness, CodexHarness)

    def test_missing_supervisor_raises(self, app_ctx_missing_supervisors):
        with pytest.raises(RuntimeError, match="supervisor or events missing"):
            _make_codex_harness(app_ctx_missing_supervisors)

    def test_missing_events_raises(self):
        class ContextWithSupervisorOnly:
            app_server_supervisor = object()
            app_server_events = None

        with pytest.raises(RuntimeError, match="supervisor or events missing"):
            _make_codex_harness(ContextWithSupervisorOnly())


class TestMakeOpenCodeHarness:
    def test_valid_context(self, app_ctx):
        from codex_autorunner.agents.opencode.harness import OpenCodeHarness

        harness = _make_opencode_harness(app_ctx)
        assert isinstance(harness, OpenCodeHarness)

    def test_missing_supervisor_raises(self, app_ctx_missing_supervisors):
        with pytest.raises(RuntimeError, match="supervisor missing"):
            _make_opencode_harness(app_ctx_missing_supervisors)


class TestPluginApiCompatibility:
    def test_accepts_older_api_version(self, monkeypatch):
        older = AgentDescriptor(
            id="older",
            name="Older",
            capabilities=frozenset(),
            make_harness=lambda ctx: None,
            plugin_api_version=CAR_PLUGIN_API_VERSION - 1,
        )
        registry = _run_with_entrypoints(monkeypatch, [_StubEntryPoint(older)])
        agents = registry.get_registered_agents()
        assert "older" in agents

    def test_rejects_newer_api_version(self, monkeypatch):
        newer = AgentDescriptor(
            id="newer",
            name="Newer",
            capabilities=frozenset(),
            make_harness=lambda ctx: None,
            plugin_api_version=CAR_PLUGIN_API_VERSION + 1,
        )
        registry = _run_with_entrypoints(monkeypatch, [_StubEntryPoint(newer)])
        agents = registry.get_registered_agents()
        assert "newer" not in agents
        # built-ins remain
        assert "codex" in agents
        assert "opencode" in agents
