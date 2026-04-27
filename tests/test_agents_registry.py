from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.agents.registry import (
    CAR_PLUGIN_API_VERSION,
    AgentDescriptor,
    _check_hermes_health,
    get_registered_agents,
    has_capability,
    reload_agents,
    resolve_agent_runtime,
    validate_agent_id,
    wrap_requested_agent_context,
)
from codex_autorunner.core.config import AgentConfig, ResolvedAgentTarget
from codex_autorunner.core.config_contract import ConfigError


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
        assert has_capability("hermes", "approvals") is True
        assert has_capability("hermes", "review") is False
        assert has_capability("hermes", "model_listing") is False
        assert has_capability("hermes", "transcript_history") is False

    def test_capability_checks_use_canonical_names(self):
        assert has_capability("codex", "durable_threads") is True
        assert has_capability("codex", "message_turns") is True

    def test_capability_checks_preserve_v1_alias_compatibility(self):
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

    def test_hermes_make_harness_reuses_bound_supervisor(self):
        from codex_autorunner.agents.hermes.harness import HermesHarness

        supervisor = object()

        class MockContext:
            hermes_supervisor = supervisor

        harness = get_registered_agents()["hermes"].make_harness(MockContext())

        assert isinstance(harness, HermesHarness)
        assert harness._supervisor is supervisor

    def test_hermes_make_harness_loads_hub_config_from_context_root(self, monkeypatch):
        from codex_autorunner.agents.hermes.harness import HermesHarness

        class HubLikeConfig:
            def agent_binary(self, _agent_id: str) -> str:
                return "hermes"

        sentinel_supervisor = object()
        observed = {}

        def _fake_load_hub_config(root: Path):
            observed["root"] = root
            return HubLikeConfig()

        def _fake_build_supervisor(config, **kwargs):
            observed["config"] = config
            observed["kwargs"] = kwargs
            return sentinel_supervisor

        monkeypatch.setattr(
            "codex_autorunner.agents.registry.load_hub_config",
            _fake_load_hub_config,
        )
        monkeypatch.setattr(
            "codex_autorunner.agents.registry.build_hermes_supervisor_from_config",
            _fake_build_supervisor,
        )

        ctx = SimpleNamespace(
            _config=SimpleNamespace(root=Path("/tmp/car-hub")),
            logger=None,
        )

        harness = get_registered_agents()["hermes"].make_harness(ctx)

        assert isinstance(harness, HermesHarness)
        assert harness._supervisor is sentinel_supervisor
        assert observed["root"] == Path("/tmp/car-hub")
        assert isinstance(observed["config"], HubLikeConfig)

    def test_hermes_health_check_loads_hub_config_from_context_root(self, monkeypatch):
        class HubLikeConfig:
            def agent_binary(self, _agent_id: str) -> str:
                return "hermes"

        observed = {}

        def _fake_load_hub_config(root: Path):
            observed["root"] = root
            return HubLikeConfig()

        monkeypatch.setattr(
            "codex_autorunner.agents.registry.load_hub_config",
            _fake_load_hub_config,
        )
        monkeypatch.setattr(
            "codex_autorunner.agents.registry.hermes_runtime_preflight",
            lambda config: type(
                "Result",
                (),
                {"status": "ready", "version": "hermes 1.0.0", "message": str(config)},
            )(),
        )

        ctx = SimpleNamespace(_config=SimpleNamespace(root=Path("/tmp/car-hub")))

        assert _check_hermes_health(ctx) is True
        assert observed["root"] == Path("/tmp/car-hub")

    def test_hermes_health_check_missing_binary(self, monkeypatch):
        monkeypatch.setattr(
            "codex_autorunner.agents.registry.hermes_runtime_preflight",
            lambda _config: type(
                "Result",
                (),
                {
                    "status": "missing_binary",
                    "version": None,
                    "launch_mode": None,
                    "message": "missing",
                    "fix": None,
                },
            )(),
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

    def test_configured_hermes_alias_registers_and_builds_distinct_harness(
        self, monkeypatch
    ):
        sentinel_supervisors: dict[tuple[str, str | None], object] = {}

        class MockConfig:
            agents = {
                "hermes": AgentConfig(
                    backend=None,
                    binary="hermes",
                    serve_command=None,
                    base_url=None,
                    subagent_models=None,
                ),
                "hermes-m4-pma": AgentConfig(
                    backend="hermes",
                    binary="hermes-m4-pma",
                    serve_command=None,
                    base_url=None,
                    subagent_models=None,
                ),
            }

            @staticmethod
            def agent_binary(agent_id: str) -> str:
                return MockConfig.agents[agent_id].binary

            @staticmethod
            def agent_backend(agent_id: str) -> str:
                agent = MockConfig.agents[agent_id]
                return str(agent.backend or agent_id)

        def _fake_build_supervisor(
            config,
            *,
            agent_id="hermes",
            profile=None,
            **_kwargs,
        ):
            assert config is MockConfig
            supervisor = sentinel_supervisors.setdefault((agent_id, profile), object())
            return supervisor

        monkeypatch.setattr(
            "codex_autorunner.agents.registry.build_hermes_supervisor_from_config",
            _fake_build_supervisor,
        )

        ctx = SimpleNamespace(config=MockConfig, logger=None)

        agents = get_registered_agents(MockConfig)
        assert validate_agent_id("hermes-m4-pma", MockConfig) == "hermes-m4-pma"
        assert agents["hermes-m4-pma"].runtime_kind == "hermes"

        harness = agents["hermes-m4-pma"].make_harness(ctx)

        assert str(harness.agent_id) == "hermes-m4-pma"
        assert harness.display_name == "Hermes (hermes-m4-pma)"
        assert harness._supervisor is sentinel_supervisors[("hermes-m4-pma", None)]
        assert ("hermes", "hermes-m4-pma", "") in ctx._agent_runtime_supervisors

    def test_hermes_alias_without_explicit_backend_infers_hermes(self, monkeypatch):
        sentinel_supervisors: dict[tuple[str, str | None], object] = {}

        class MockConfig:
            agents = {
                "hermes": AgentConfig(
                    backend=None,
                    binary="hermes",
                    serve_command=None,
                    base_url=None,
                    subagent_models=None,
                ),
                "hermes-m4-pma": AgentConfig(
                    backend=None,
                    binary="hermes-m4-pma",
                    serve_command=None,
                    base_url=None,
                    subagent_models=None,
                ),
            }

            @staticmethod
            def agent_binary(agent_id: str) -> str:
                return MockConfig.agents[agent_id].binary

            @staticmethod
            def agent_backend(agent_id: str) -> str:
                agent = MockConfig.agents[agent_id]
                return str(agent.backend or agent_id)

        def _fake_build_supervisor(
            config,
            *,
            agent_id="hermes",
            profile=None,
            **_kwargs,
        ):
            assert config is MockConfig
            supervisor = sentinel_supervisors.setdefault((agent_id, profile), object())
            return supervisor

        monkeypatch.setattr(
            "codex_autorunner.agents.registry.build_hermes_supervisor_from_config",
            _fake_build_supervisor,
        )

        ctx = SimpleNamespace(config=MockConfig, logger=None)

        agents = get_registered_agents(MockConfig)
        assert validate_agent_id("hermes-m4-pma", MockConfig) == "hermes-m4-pma"
        assert agents["hermes-m4-pma"].runtime_kind == "hermes"

        harness = agents["hermes-m4-pma"].make_harness(ctx)

        assert str(harness.agent_id) == "hermes-m4-pma"
        assert harness.display_name == "Hermes (hermes-m4-pma)"
        assert harness._supervisor is sentinel_supervisors[("hermes-m4-pma", None)]
        assert ("hermes", "hermes-m4-pma", "") in ctx._agent_runtime_supervisors

    def test_validate_agent_id_prefers_repo_config_for_path_context(self, monkeypatch):
        calls: list[str] = []

        class HubConfig:
            agents = {
                "hermes": AgentConfig(
                    backend=None,
                    binary="hermes",
                    serve_command=None,
                    base_url=None,
                    subagent_models=None,
                )
            }

            @staticmethod
            def agent_binary(agent_id: str) -> str:
                return HubConfig.agents[agent_id].binary

        class RepoConfig(HubConfig):
            agents = {
                **HubConfig.agents,
                "hermes-m4-pma": AgentConfig(
                    backend="hermes",
                    binary="hermes-m4-pma",
                    serve_command=None,
                    base_url=None,
                    subagent_models=None,
                ),
            }

            @staticmethod
            def agent_binary(agent_id: str) -> str:
                return RepoConfig.agents[agent_id].binary

            @staticmethod
            def agent_backend(agent_id: str) -> str:
                agent = RepoConfig.agents[agent_id]
                return str(agent.backend or agent_id)

        monkeypatch.setattr(
            "codex_autorunner.agents.registry.load_repo_config",
            lambda _root: calls.append("repo") or RepoConfig,
        )
        monkeypatch.setattr(
            "codex_autorunner.agents.registry.load_hub_config",
            lambda _root: calls.append("hub") or HubConfig,
        )

        assert validate_agent_id("hermes-m4-pma", Path("/tmp/repo")) == "hermes-m4-pma"
        assert calls == ["repo"]

    def test_get_registered_agents_skips_warning_for_known_profile_style_id(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        class MockConfig:
            agents = {
                "hermes": AgentConfig(
                    backend=None,
                    binary="hermes",
                    serve_command=None,
                    base_url=None,
                    subagent_models=None,
                    profiles={"m4-pma": object()},
                ),
                "hermes-m4-pma": AgentConfig(
                    backend=None,
                    binary=None,
                    serve_command=None,
                    base_url=None,
                    subagent_models=None,
                ),
            }

            @staticmethod
            def agent_binary(agent_id: str) -> str:
                agent = MockConfig.agents[agent_id]
                if not agent.binary:
                    raise ConfigError(f"agents.{agent_id}.binary is required")
                return agent.binary

            @staticmethod
            def agent_backend(agent_id: str) -> str:
                if agent_id == "hermes-m4-pma":
                    raise ConfigError(f"agents.{agent_id}.binary is required")
                agent = MockConfig.agents[agent_id]
                return str(agent.backend or agent_id)

            @staticmethod
            def agent_profiles(agent_id: str):
                agent = MockConfig.agents.get(agent_id)
                return dict(agent.profiles or {}) if agent is not None else {}

        caplog.set_level("WARNING")

        agents = get_registered_agents(MockConfig)

        assert "hermes-m4-pma" not in agents
        assert (
            "Configured agent hermes-m4-pma references unknown backend"
            not in caplog.text
        )

    def test_wrap_requested_agent_context_preserves_profile(self):
        ctx = SimpleNamespace()

        wrapped = wrap_requested_agent_context(ctx, agent_id="hermes", profile="m4")

        assert wrapped._requested_agent_id == "hermes"
        assert wrapped._requested_agent_profile == "m4"

    def test_resolve_agent_runtime_maps_alias_input_to_logical_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "codex_autorunner.agents.registry.get_registered_agents",
            lambda context=None: {
                "hermes": AgentDescriptor(
                    id="hermes",
                    name="Hermes",
                    capabilities=frozenset(),
                    make_harness=lambda _ctx: None,  # type: ignore[return-value]
                    runtime_kind="hermes",
                ),
                "hermes-m4-pma": AgentDescriptor(
                    id="hermes-m4-pma",
                    name="Hermes Alias",
                    capabilities=frozenset(),
                    make_harness=lambda _ctx: None,  # type: ignore[return-value]
                    runtime_kind="hermes",
                ),
            },
        )
        monkeypatch.setattr(
            "codex_autorunner.agents.registry._resolve_runtime_agent_config",
            lambda _ctx: SimpleNamespace(
                resolve_runtime_agent_target=lambda agent_id, profile=None: (
                    ResolvedAgentTarget(
                        logical_agent_id=agent_id,
                        logical_profile=profile,
                        runtime_agent_id="hermes-m4-pma",
                        runtime_profile=None,
                        resolution_kind="alias_profile",
                    )
                    if agent_id == "hermes" and profile == "m4-pma"
                    else None
                )
            ),
        )

        resolved = resolve_agent_runtime("hermes-m4-pma", context="ctx")

        assert resolved.logical_agent_id == "hermes"
        assert resolved.logical_profile == "m4-pma"
        assert resolved.runtime_agent_id == "hermes-m4-pma"
        assert resolved.runtime_profile is None
        assert resolved.resolution_kind == "alias_profile"
