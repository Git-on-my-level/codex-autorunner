from __future__ import annotations

import threading

import pytest

from codex_autorunner.agents.registry import (
    CAR_PLUGIN_API_VERSION,
    AgentDescriptor,
    _check_hermes_health,
    get_registered_agents,
    has_capability,
    reload_agents,
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


@pytest.fixture
def app_ctx_zeroclaw_ready():
    class MockConfig:
        @staticmethod
        def agent_binary(_agent_id: str) -> str:
            return "zeroclaw"

    class MockContext:
        app_server_supervisor = None
        app_server_events = None
        opencode_supervisor = None
        config = MockConfig()

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
        assert has_capability("codex", "durable_threads") is True
        assert has_capability("codex", "message_turns") is True
        assert has_capability("codex", "interrupt") is True
        assert has_capability("codex", "active_thread_discovery") is True
        assert has_capability("codex", "review") is True
        assert has_capability("codex", "model_listing") is True
        assert has_capability("codex", "event_streaming") is True
        assert has_capability("codex", "approvals") is True

    def test_valid_capabilities_opencode(self):
        assert has_capability("opencode", "durable_threads") is True
        assert has_capability("opencode", "message_turns") is True
        assert has_capability("opencode", "interrupt") is True
        assert has_capability("opencode", "active_thread_discovery") is True
        assert has_capability("opencode", "review") is True
        assert has_capability("opencode", "model_listing") is True
        assert has_capability("opencode", "event_streaming") is True

    def test_nonexistent_capability(self):
        assert has_capability("codex", "invalid_capability") is False

    def test_nonexistent_agent(self):
        assert has_capability("invalid_agent", "durable_threads") is False
        assert has_capability("invalid_agent", "invalid_capability") is False

    def test_opencode_doesnt_have_approvals(self):
        assert has_capability("opencode", "approvals") is False

    def test_zeroclaw_advertises_only_durable_thread_capabilities_it_proves(self):
        assert has_capability("zeroclaw", "durable_threads") is True
        assert has_capability("zeroclaw", "message_turns") is True
        assert has_capability("zeroclaw", "active_thread_discovery") is True
        assert has_capability("zeroclaw", "event_streaming") is True
        assert has_capability("zeroclaw", "interrupt") is False

    def test_hermes_advertises_acp_capabilities(self):
        assert has_capability("hermes", "durable_threads") is True
        assert has_capability("hermes", "message_turns") is True
        assert has_capability("hermes", "active_thread_discovery") is True
        assert has_capability("hermes", "event_streaming") is True
        assert has_capability("hermes", "interrupt") is True
        assert has_capability("hermes", "review") is False
        assert has_capability("hermes", "model_listing") is False
        assert has_capability("hermes", "transcript_history") is False

    def test_legacy_capability_aliases_still_normalize(self):
        assert has_capability("codex", "threads") is True
        assert has_capability("codex", "turns") is True


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
        assert "hermes" in agents
        assert "zeroclaw" in agents
        assert len(agents) >= 4

    def test_concurrent_reload_and_access_no_exceptions(self):
        exceptions = []

        def reload_and_access():
            try:
                for _ in range(5):
                    get_registered_agents()
                    reload_agents()
            except Exception as e:
                exceptions.append(e)

        threads = [threading.Thread(target=reload_and_access) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert (
            not exceptions
        ), f"Exceptions occurred during concurrent reload/access: {exceptions}"


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


class TestHermesHarness:
    def test_hermes_in_registered_agents(self):
        agents = get_registered_agents()
        assert "hermes" in agents
        assert isinstance(agents["hermes"], AgentDescriptor)
        assert agents["hermes"].id == "hermes"

    def test_hermes_health_check_missing_binary(self, monkeypatch):
        monkeypatch.setattr(
            "codex_autorunner.agents.hermes.supervisor.hermes_binary_available",
            lambda _config: False,
        )

        class MockConfig:
            def agent_binary(self, _agent_id: str) -> str:
                return "hermes"

        class MockContext:
            config = MockConfig()

        result = _check_hermes_health(MockContext())
        assert result is False

    def test_hermes_health_check_available(self, monkeypatch):
        monkeypatch.setattr(
            "codex_autorunner.agents.registry.hermes_runtime_preflight",
            lambda _config: type(
                "Result",
                (),
                {"status": "ready", "version": "hermes 1.0.0", "message": "Ready"},
            )(),
        )

        class MockConfig:
            def agent_binary(self, _agent_id: str) -> str:
                return "hermes"

        class MockContext:
            config = MockConfig()

        result = _check_hermes_health(MockContext())
        assert result is True

    def test_validate_agent_id_accepts_hermes(self):
        assert validate_agent_id("hermes") == "hermes"
        assert validate_agent_id("HERMES") == "hermes"
        assert validate_agent_id("  hermes  ") == "hermes"
