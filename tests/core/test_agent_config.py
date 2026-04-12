from codex_autorunner.core.agent_config import (
    AgentConfig,
    AgentProfileConfig,
    ResolvedAgentTarget,
    _parse_command,
    parse_agents_config,
)


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

    def test_skips_non_dict_agent_entries(self) -> None:
        cfg = {"agents": {"codex": "not-a-dict"}}
        result = parse_agents_config(cfg, {})
        assert "codex" not in result

    def test_skips_agent_with_missing_binary(self) -> None:
        cfg = {"agents": {"codex": {"backend": "codex"}}}
        result = parse_agents_config(cfg, {})
        assert "codex" not in result

    def test_skips_agent_with_empty_binary(self) -> None:
        cfg = {"agents": {"codex": {"backend": "codex", "binary": "  "}}}
        result = parse_agents_config(cfg, {})
        assert "codex" not in result

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

    def test_skips_profile_with_empty_id(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {"": {"binary": "bin-other"}},
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].profiles is None

    def test_skips_profile_with_non_dict_value(self) -> None:
        cfg = {
            "agents": {
                "x": {
                    "binary": "bin",
                    "profiles": {"p1": "not-a-dict"},
                }
            }
        }
        result = parse_agents_config(cfg, {})
        assert result["x"].profiles is None

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
