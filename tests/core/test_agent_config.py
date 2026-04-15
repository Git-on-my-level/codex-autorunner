import pytest

from codex_autorunner.core.agent_config import (
    AgentConfig,
    AgentProfileConfig,
    ResolvedAgentTarget,
    _parse_command,
    parse_agents_config,
    resolve_agent_target_from_agents,
)
from codex_autorunner.core.config_contract import ConfigError


class TestParseCommand:
    def test_list_input(self) -> None:
        assert _parse_command(["a", "b", "c"]) == ["a", "b", "c"]

    def test_string_input(self) -> None:
        assert _parse_command("a b c") == ["a", "b", "c"]

    def test_none_input(self) -> None:
        assert _parse_command(None) == []

    def test_int_input(self) -> None:
        assert _parse_command(42) == []

    def test_list_filters_falsy(self) -> None:
        assert _parse_command(["a", "", None, "b"]) == ["a", "b"]

    def test_string_with_extra_whitespace(self) -> None:
        assert _parse_command("  a   b  ") == ["a", "b"]


class TestParseAgentsConfig:
    def test_returns_defaults_when_cfg_has_no_agents(self) -> None:
        defaults = {
            "agents": {
                "codex": {
                    "backend": "codex",
                    "binary": "codex-binary",
                }
            }
        }
        result = parse_agents_config({}, defaults)
        assert "codex" in result
        assert result["codex"].binary == "codex-binary"

    def test_returns_defaults_when_cfg_is_none(self) -> None:
        defaults = {
            "agents": {
                "codex": {"backend": "codex", "binary": "codex-binary"},
            }
        }
        result = parse_agents_config(None, defaults)
        assert "codex" in result

    def test_non_dict_agent_entry_raises(self) -> None:
        cfg = {"agents": {"codex": "not-a-dict"}}
        with pytest.raises(ConfigError, match="must be a mapping"):
            parse_agents_config(cfg, {})

    def test_agent_with_missing_binary_raises(self) -> None:
        cfg = {"agents": {"codex": {"backend": "codex"}}}
        with pytest.raises(ConfigError, match="binary is required"):
            parse_agents_config(cfg, {})

    def test_agent_with_empty_binary_raises(self) -> None:
        cfg = {"agents": {"codex": {"backend": "codex", "binary": "  "}}}
        with pytest.raises(ConfigError, match="binary is required"):
            parse_agents_config(cfg, {})

    def test_parses_basic_agent(self) -> None:
        cfg = {
            "agents": {
                "opencode": {
                    "backend": "opencode",
                    "binary": "/usr/bin/opencode",
                }
            }
        }
        result = parse_agents_config(cfg, {})
        agent = result["opencode"]
        assert agent.backend == "opencode"
        assert agent.binary == "/usr/bin/opencode"
        assert agent.serve_command is None
        assert agent.base_url is None
        assert agent.subagent_models is None
        assert agent.default_profile is None
        assert agent.profiles is None

    def test_backend_none_when_empty(self) -> None:
        cfg = {"agents": {"x": {"backend": "  ", "binary": "bin"}}}
        result = parse_agents_config(cfg, {})
        assert result["x"].backend is None

    def test_parses_serve_command_list(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "serve_command": ["bin", "--serve"],
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].serve_command == ["bin", "--serve"]

    def test_parses_serve_command_string(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "serve_command": "bin --serve --port 8080",
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].serve_command == ["bin", "--serve", "--port", "8080"]

    def test_serve_command_absent_means_none(self) -> None:
        cfg = {"agents": {"x": {"binary": "bin"}}}
        result = parse_agents_config(cfg, {})
        assert result["x"].serve_command is None

    def test_parses_subagent_models(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "subagent_models": {"reviewer": "model-a"},
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].subagent_models == {"reviewer": "model-a"}

    def test_subagent_models_non_dict_means_none(self) -> None:
        cfg = {"agents": {"x": {"binary": "bin", "subagent_models": "bad"}}}
        result = parse_agents_config(cfg, {})
        assert result["x"].subagent_models is None

    def test_default_profile_normalized(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "default_profile": "  My-Profile  ",
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].default_profile == "my-profile"

    def test_default_profile_empty_means_none(self) -> None:
        cfg = {"agents": {"x": {"binary": "bin", "default_profile": "  "}}}
        result = parse_agents_config(cfg, {})
        assert result["x"].default_profile is None

    def test_default_profile_non_string_means_none(self) -> None:
        cfg = {"agents": {"x": {"binary": "bin", "default_profile": 42}}}
        result = parse_agents_config(cfg, {})
        assert result["x"].default_profile is None

    def test_parses_profiles(self) -> None:
        cfg = {
            "agents": {
                "hermes": {
                    "binary": "hermes",
                    "profiles": {
                        "m4-pma": {
                            "binary": "hermes-m4",
                            "backend": "hermes",
                            "display_name": "M4 PMA",
                        },
                    },
                }
            }
        }
        result = parse_agents_config(cfg, {})
        agent = result["hermes"]
        assert agent.profiles is not None
        profile = agent.profiles["m4-pma"]
        assert profile.binary == "hermes-m4"
        assert profile.backend == "hermes"
        assert profile.display_name == "M4 PMA"

    def test_profile_id_normalized_to_lowercase(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {"UPPER": {"binary": "bin-upper"}},
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert "upper" in result["x"].profiles

    def test_empty_profiles_dict_means_none(self) -> None:
        cfg = {"agents": {"x": {"binary": "bin", "profiles": {}}}}
        result = parse_agents_config(cfg, {})
        assert result["x"].profiles is None

    def test_profile_with_empty_id_raises(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {"": {"binary": "bin-other"}},
                }
            }
        }
        with pytest.raises(ConfigError, match="must be non-empty strings"):
            parse_agents_config(cfg, {})

    def test_profile_with_non_dict_value_raises(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {"p1": "not-a-dict"},
                }
            }
        }
        with pytest.raises(ConfigError, match="must be a mapping"):
            parse_agents_config(cfg, {})

    def test_profile_backend_none_when_empty(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {"p1": {"backend": "  "}},
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].profiles["p1"].backend is None

    def test_profile_display_name_none_when_empty(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {"p1": {"display_name": "  "}},
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].profiles["p1"].display_name is None

    def test_profile_binary_none_when_empty(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {"p1": {"binary": "  "}},
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].profiles["p1"].binary is None

    def test_profile_serve_command_parsed(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {
                        "p1": {"serve_command": "bin --serve --profile p1"},
                    },
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].profiles["p1"].serve_command == [
            "bin",
            "--serve",
            "--profile",
            "p1",
        ]

    def test_profile_serve_command_absent_means_none(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {"p1": {}},
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].profiles["p1"].serve_command is None

    def test_profile_subagent_models_non_dict_means_none(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {"p1": {"subagent_models": "bad"}},
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].profiles["p1"].subagent_models is None


class TestBackwardCompatImports:
    def test_agent_config_reexported_from_config(self) -> None:
        from codex_autorunner.core.config import (
            AgentConfig,
            AgentProfileConfig,
            ResolvedAgentTarget,
        )

        assert AgentConfig is not None
        assert AgentProfileConfig is not None
        assert ResolvedAgentTarget is not None

    def test_same_class_identity(self) -> None:
        from codex_autorunner.core.config import (
            AgentConfig as ConfigAgentConfig,
        )
        from codex_autorunner.core.config import (
            AgentProfileConfig as ConfigAgentProfileConfig,
        )
        from codex_autorunner.core.config import (
            ResolvedAgentTarget as ConfigResolvedAgentTarget,
        )

        assert ConfigAgentConfig is AgentConfig
        assert ConfigAgentProfileConfig is AgentProfileConfig
        assert ConfigResolvedAgentTarget is ResolvedAgentTarget


class TestResolveAgentTargetFromAgents:
    def test_configured_alias_id_normalizes_without_registry_help(self) -> None:
        result = resolve_agent_target_from_agents(
            {
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
            },
            "hermes-m4-pma",
        )

        assert result.logical_agent_id == "hermes"
        assert result.logical_profile == "m4-pma"
        assert result.runtime_agent_id == "hermes-m4-pma"
        assert result.runtime_profile is None
        assert result.resolution_kind == "alias_profile"

    def test_runtime_alias_fallback_can_resolve_profile_without_config(self) -> None:
        result = resolve_agent_target_from_agents(
            {},
            "hermes",
            profile="m4-pma",
            runtime_alias_kinds={"hermes-m4-pma": "hermes"},
            allow_runtime_alias_fallback=True,
        )

        assert result.logical_agent_id == "hermes"
        assert result.logical_profile == "m4-pma"
        assert result.runtime_agent_id == "hermes-m4-pma"
        assert result.runtime_profile is None
        assert result.resolution_kind == "alias_profile"
