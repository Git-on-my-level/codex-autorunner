from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.config_contract import CONFIG_VERSION, ConfigError
from codex_autorunner.core.config_validation import (
    _is_strict_int,
    _normalize_ticket_flow_approval_mode,
    _validate_agents_config,
    _validate_app_server_config,
    _validate_collaboration_policy_config,
    _validate_discord_bot_config,
    _validate_housekeeping_config,
    _validate_opencode_config,
    _validate_repo_config,
    _validate_server_security,
    _validate_static_assets_config,
    _validate_telegram_bot_config,
    _validate_update_config,
    _validate_usage_config,
    _validate_version,
    is_loopback_host,
)


def _minimal_repo_config_for_flow_retention() -> dict[str, object]:
    return {
        "version": CONFIG_VERSION,
        "mode": "repo",
        "docs": {
            "active_context": ".codex-autorunner/contextspace/active_context.md",
            "decisions": ".codex-autorunner/contextspace/decisions.md",
            "spec": ".codex-autorunner/contextspace/spec.md",
        },
        "codex": {"binary": "codex"},
        "prompt": {"prev_run_max_chars": 10},
        "runner": {"sleep_seconds": 1, "stop_after_runs": None},
        "git": {"auto_commit": False},
        "server": {"host": "127.0.0.1", "port": 4173},
        "log": {
            "path": ".codex-autorunner/codex-autorunner.log",
            "max_bytes": 1,
            "backup_count": 1,
        },
        "server_log": {
            "path": ".codex-autorunner/codex-server.log",
            "max_bytes": 1,
            "backup_count": 1,
        },
    }


class TestNormalizeTicketFlowApprovalMode:
    def test_yolo(self) -> None:
        assert _normalize_ticket_flow_approval_mode("yolo", scope="test") == "yolo"

    def test_review(self) -> None:
        assert _normalize_ticket_flow_approval_mode("review", scope="test") == "review"

    def test_safe_alias_maps_to_review(self) -> None:
        assert _normalize_ticket_flow_approval_mode("safe", scope="test") == "review"

    def test_case_insensitive(self) -> None:
        assert _normalize_ticket_flow_approval_mode("YOLO", scope="test") == "yolo"
        assert (
            _normalize_ticket_flow_approval_mode(" Review ", scope="test") == "review"
        )

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be one of"):
            _normalize_ticket_flow_approval_mode("invalid", scope="test")

    def test_non_string_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a string"):
            _normalize_ticket_flow_approval_mode(123, scope="test")


class TestValidateVersion:
    def test_valid_version(self) -> None:
        _validate_version({"version": CONFIG_VERSION})

    def test_wrong_version_raises(self) -> None:
        with pytest.raises(ConfigError, match="Unsupported config version"):
            _validate_version({"version": 99})

    def test_missing_version_raises(self) -> None:
        with pytest.raises(ConfigError, match="Unsupported config version"):
            _validate_version({})


class TestIsLoopbackHost:
    @pytest.mark.parametrize(
        "host",
        ["localhost", "127.0.0.1", "::1"],
    )
    def test_loopback_hosts(self, host: str) -> None:
        assert is_loopback_host(host) is True

    def test_unspecified_is_not_loopback(self) -> None:
        assert is_loopback_host("0.0.0.0") is False

    def test_non_loopback_ip(self) -> None:
        assert is_loopback_host("192.168.1.1") is False

    def test_non_ip_string(self) -> None:
        assert is_loopback_host("example.com") is False


class TestIsStrictInt:
    def test_int_passes(self) -> None:
        assert _is_strict_int(42) is True

    def test_bool_fails(self) -> None:
        assert _is_strict_int(True) is False
        assert _is_strict_int(False) is False

    def test_float_fails(self) -> None:
        assert _is_strict_int(3.14) is False


class TestValidateServerSecurity:
    def test_loopback_without_allowed_hosts_ok(self) -> None:
        _validate_server_security({"host": "127.0.0.1"})

    def test_localhost_without_allowed_hosts_ok(self) -> None:
        _validate_server_security({"host": "localhost"})

    def test_non_loopback_without_allowed_hosts_raises(self) -> None:
        with pytest.raises(ConfigError, match="allowed_hosts must be set"):
            _validate_server_security({"host": "0.0.0.0"})

    def test_non_loopback_with_allowed_hosts_ok(self) -> None:
        _validate_server_security({"host": "0.0.0.0", "allowed_hosts": ["example.com"]})

    def test_allowed_hosts_must_be_list(self) -> None:
        with pytest.raises(ConfigError, match="must be a list of strings"):
            _validate_server_security({"allowed_hosts": "not-a-list"})

    def test_allowed_hosts_entries_must_be_strings(self) -> None:
        with pytest.raises(ConfigError, match="must be a list of strings"):
            _validate_server_security({"allowed_hosts": [123]})

    def test_allowed_origins_must_be_list(self) -> None:
        with pytest.raises(ConfigError, match="must be a list of strings"):
            _validate_server_security({"allowed_origins": "not-a-list"})

    def test_allowed_origins_entries_must_be_strings(self) -> None:
        with pytest.raises(ConfigError, match="must be a list of strings"):
            _validate_server_security({"allowed_origins": [123]})


class TestValidateAppServerConfig:
    def test_none_is_ok(self) -> None:
        _validate_app_server_config({})

    def test_valid_config(self) -> None:
        _validate_app_server_config({"app_server": {"command": ["echo"]}})

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_app_server_config({"app_server": "bad"})

    def test_command_must_be_list_or_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a list or string"):
            _validate_app_server_config({"app_server": {"command": 42}})

    def test_command_may_be_null(self) -> None:
        _validate_app_server_config({"app_server": {"command": None}})

    def test_state_root_must_be_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a string path"):
            _validate_app_server_config({"app_server": {"state_root": 42}})

    def test_auto_restart_must_be_bool_or_null(self) -> None:
        _validate_app_server_config({"app_server": {"auto_restart": True}})
        _validate_app_server_config({"app_server": {"auto_restart": None}})
        with pytest.raises(ConfigError, match="must be boolean or null"):
            _validate_app_server_config({"app_server": {"auto_restart": "yes"}})

    def test_max_handles_must_be_int_or_null(self) -> None:
        _validate_app_server_config({"app_server": {"max_handles": 5}})
        _validate_app_server_config({"app_server": {"max_handles": None}})
        with pytest.raises(ConfigError, match="must be an integer or null"):
            _validate_app_server_config({"app_server": {"max_handles": "5"}})

    def test_turn_timeout_seconds_must_be_number_or_null(self) -> None:
        _validate_app_server_config({"app_server": {"turn_timeout_seconds": 30}})
        _validate_app_server_config({"app_server": {"turn_timeout_seconds": None}})
        with pytest.raises(ConfigError, match="must be a number or null"):
            _validate_app_server_config(
                {"app_server": {"turn_timeout_seconds": "slow"}}
            )

    def test_client_max_message_bytes_must_be_positive_int(self) -> None:
        _validate_app_server_config(
            {"app_server": {"client": {"max_message_bytes": 100}}}
        )
        with pytest.raises(ConfigError, match="must be > 0"):
            _validate_app_server_config(
                {"app_server": {"client": {"max_message_bytes": 0}}}
            )
        with pytest.raises(ConfigError, match="must be an integer"):
            _validate_app_server_config(
                {"app_server": {"client": {"max_message_bytes": "big"}}}
            )

    def test_client_backoff_jitter_ratio_must_be_gte_zero(self) -> None:
        _validate_app_server_config(
            {"app_server": {"client": {"restart_backoff_jitter_ratio": 0}}}
        )
        with pytest.raises(ConfigError, match="must be >= 0"):
            _validate_app_server_config(
                {"app_server": {"client": {"restart_backoff_jitter_ratio": -1}}}
            )

    def test_prompts_section_valid(self) -> None:
        _validate_app_server_config(
            {
                "app_server": {
                    "prompts": {
                        "doc_chat": {"max_chars": 100},
                    }
                }
            }
        )

    def test_prompts_max_chars_must_be_positive_int(self) -> None:
        with pytest.raises(ConfigError, match="must be >= 1"):
            _validate_app_server_config(
                {
                    "app_server": {
                        "prompts": {
                            "doc_chat": {"max_chars": 0},
                        }
                    }
                }
            )

    def test_prompts_must_be_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_app_server_config({"app_server": {"prompts": "bad"}})

    def test_prompts_section_must_be_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_app_server_config(
                {"app_server": {"prompts": {"doc_chat": "bad"}}}
            )

    def test_prompts_value_must_be_int(self) -> None:
        with pytest.raises(ConfigError, match="must be an integer"):
            _validate_app_server_config(
                {
                    "app_server": {
                        "prompts": {"doc_chat": {"max_chars": "big"}},
                    }
                }
            )


class TestValidateRepoConfigFlowRetention:
    def test_rejects_non_mapping(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config_for_flow_retention()
        cfg["flow_retention"] = "bad"

        with pytest.raises(ConfigError, match="flow_retention must be a mapping"):
            _validate_repo_config(cfg, root=tmp_path)

    @pytest.mark.parametrize(
        ("key", "value", "message"),
        (
            ("retention_days", "7", "flow_retention.retention_days must be an integer"),
            ("retention_days", 0, "flow_retention.retention_days must be > 0"),
            (
                "sweep_interval_seconds",
                "60",
                "flow_retention.sweep_interval_seconds must be an integer",
            ),
            (
                "sweep_interval_seconds",
                0,
                "flow_retention.sweep_interval_seconds must be > 0",
            ),
        ),
    )
    def test_rejects_invalid_rule_value(
        self, tmp_path: Path, key: str, value: object, message: str
    ) -> None:
        cfg = _minimal_repo_config_for_flow_retention()
        cfg["flow_retention"] = {
            "retention_days": 7,
            "sweep_interval_seconds": 60,
        }
        cfg["flow_retention"][key] = value

        with pytest.raises(ConfigError, match=message):
            _validate_repo_config(cfg, root=tmp_path)


class TestValidateAgentsConfig:
    def test_none_is_ok(self) -> None:
        _validate_agents_config({})

    def test_valid_agent(self) -> None:
        _validate_agents_config(
            {
                "agents": {
                    "opencode": {
                        "binary": "opencode",
                    }
                }
            }
        )

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_agents_config({"agents": "bad"})

    def test_agent_must_be_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_agents_config({"agents": {"opencode": "bad"}})

    def test_binary_required(self) -> None:
        with pytest.raises(ConfigError, match="binary is required"):
            _validate_agents_config({"agents": {"opencode": {}}})

    def test_binary_must_be_nonempty_str(self) -> None:
        with pytest.raises(ConfigError, match="binary is required"):
            _validate_agents_config({"agents": {"opencode": {"binary": ""}}})

    def test_backend_must_be_nonempty_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a non-empty string"):
            _validate_agents_config(
                {"agents": {"opencode": {"binary": "opencode", "backend": ""}}}
            )

    def test_serve_command_must_be_list_or_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a list or str"):
            _validate_agents_config(
                {
                    "agents": {
                        "opencode": {
                            "binary": "opencode",
                            "serve_command": 42,
                        }
                    }
                }
            )

    def test_profiles_must_be_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_agents_config(
                {
                    "agents": {
                        "opencode": {
                            "binary": "opencode",
                            "profiles": "bad",
                        }
                    }
                }
            )

    def test_valid_profiles(self) -> None:
        _validate_agents_config(
            {
                "agents": {
                    "opencode": {
                        "binary": "opencode",
                        "profiles": {
                            "default": {"binary": "opencode"},
                        },
                        "default_profile": "default",
                    }
                }
            }
        )

    def test_default_profile_must_reference_configured_profile(self) -> None:
        with pytest.raises(ConfigError, match="must reference a configured profile"):
            _validate_agents_config(
                {
                    "agents": {
                        "opencode": {
                            "binary": "opencode",
                            "profiles": {
                                "default": {"binary": "opencode"},
                            },
                            "default_profile": "missing",
                        }
                    }
                }
            )

    def test_profile_key_must_be_nonempty(self) -> None:
        with pytest.raises(ConfigError, match="must be non-empty strings"):
            _validate_agents_config(
                {
                    "agents": {
                        "opencode": {
                            "binary": "opencode",
                            "profiles": {"": {"binary": "opencode"}},
                        }
                    }
                }
            )

    def test_profile_must_be_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_agents_config(
                {
                    "agents": {
                        "opencode": {
                            "binary": "opencode",
                            "profiles": {"my-profile": "bad"},
                        }
                    }
                }
            )


class TestValidateUpdateConfig:
    def test_none_is_ok(self) -> None:
        _validate_update_config({})

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_update_config({"update": "bad"})

    def test_valid_auto_backend(self) -> None:
        _validate_update_config({"update": {"backend": "auto"}})

    def test_invalid_backend_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be one of"):
            _validate_update_config({"update": {"backend": "invalid"}})

    def test_backend_must_be_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a string"):
            _validate_update_config({"update": {"backend": 42}})

    def test_skip_checks_must_be_bool(self) -> None:
        _validate_update_config({"update": {"skip_checks": True}})
        with pytest.raises(ConfigError, match="must be boolean or null"):
            _validate_update_config({"update": {"skip_checks": "yes"}})

    def test_linux_service_names_must_be_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_update_config({"update": {"linux_service_names": "bad"}})

    def test_linux_service_names_valid(self) -> None:
        _validate_update_config({"update": {"linux_service_names": {"hub": "car-hub"}}})

    def test_linux_service_names_must_be_nonempty_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a non-empty string"):
            _validate_update_config({"update": {"linux_service_names": {"hub": ""}}})


class TestValidateOpencodeConfig:
    def test_none_is_ok(self) -> None:
        _validate_opencode_config({})

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_opencode_config({"opencode": "bad"})

    def test_valid_server_scope_workspace(self) -> None:
        _validate_opencode_config({"opencode": {"server_scope": "workspace"}})

    def test_valid_server_scope_global(self) -> None:
        _validate_opencode_config({"opencode": {"server_scope": "global"}})

    def test_invalid_server_scope_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be 'workspace' or 'global'"):
            _validate_opencode_config({"opencode": {"server_scope": "invalid"}})

    def test_server_scope_must_be_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a string or null"):
            _validate_opencode_config({"opencode": {"server_scope": 42}})

    def test_session_stall_timeout_must_be_number(self) -> None:
        _validate_opencode_config({"opencode": {"session_stall_timeout_seconds": 30}})
        with pytest.raises(ConfigError, match="must be a number or null"):
            _validate_opencode_config(
                {"opencode": {"session_stall_timeout_seconds": "slow"}}
            )

    def test_max_text_chars_must_be_int(self) -> None:
        _validate_opencode_config({"opencode": {"max_text_chars": 1000}})
        with pytest.raises(ConfigError, match="must be an integer or null"):
            _validate_opencode_config({"opencode": {"max_text_chars": "big"}})

    def test_max_handles_must_be_int(self) -> None:
        _validate_opencode_config({"opencode": {"max_handles": 5}})
        with pytest.raises(ConfigError, match="must be an integer or null"):
            _validate_opencode_config({"opencode": {"max_handles": "5"}})

    def test_idle_ttl_must_be_int(self) -> None:
        _validate_opencode_config({"opencode": {"idle_ttl_seconds": 300}})
        with pytest.raises(ConfigError, match="must be an integer or null"):
            _validate_opencode_config({"opencode": {"idle_ttl_seconds": 3.14}})


class TestValidateUsageConfig:
    def test_none_is_ok(self) -> None:
        _validate_usage_config({}, root=Path("/tmp"))

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_usage_config({"usage": "bad"}, root=Path("/tmp"))

    def test_cache_scope_valid_global(self) -> None:
        _validate_usage_config({"usage": {"cache_scope": "global"}}, root=Path("/tmp"))

    def test_cache_scope_valid_repo(self) -> None:
        _validate_usage_config({"usage": {"cache_scope": "repo"}}, root=Path("/tmp"))

    def test_cache_scope_invalid(self) -> None:
        with pytest.raises(ConfigError, match="must be 'global' or 'repo'"):
            _validate_usage_config(
                {"usage": {"cache_scope": "invalid"}}, root=Path("/tmp")
            )

    def test_cache_scope_must_be_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a string"):
            _validate_usage_config({"usage": {"cache_scope": 42}}, root=Path("/tmp"))

    def test_global_cache_root_valid(self) -> None:
        _validate_usage_config(
            {"usage": {"global_cache_root": "~/.cache"}}, root=Path("/tmp")
        )

    def test_global_cache_root_must_be_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a string or null"):
            _validate_usage_config(
                {"usage": {"global_cache_root": 42}}, root=Path("/tmp")
            )

    def test_repo_cache_path_valid(self, tmp_path: Path) -> None:
        _validate_usage_config({"usage": {"repo_cache_path": ".cache"}}, root=tmp_path)

    def test_repo_cache_path_must_be_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a string or null"):
            _validate_usage_config(
                {"usage": {"repo_cache_path": 42}}, root=Path("/tmp")
            )


class TestValidateTelegramBotConfig:
    def test_none_is_ok(self) -> None:
        _validate_telegram_bot_config({})

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_telegram_bot_config({"telegram_bot": "bad"})

    def test_enabled_must_be_bool(self) -> None:
        with pytest.raises(ConfigError, match="must be boolean"):
            _validate_telegram_bot_config({"telegram_bot": {"enabled": "yes"}})

    def test_mode_must_be_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a string"):
            _validate_telegram_bot_config({"telegram_bot": {"mode": 42}})

    def test_parse_mode_valid_html(self) -> None:
        _validate_telegram_bot_config({"telegram_bot": {"parse_mode": "HTML"}})

    def test_parse_mode_valid_markdown(self) -> None:
        _validate_telegram_bot_config({"telegram_bot": {"parse_mode": "Markdown"}})

    def test_parse_mode_valid_markdownv2(self) -> None:
        _validate_telegram_bot_config({"telegram_bot": {"parse_mode": "MarkdownV2"}})

    def test_parse_mode_null_ok(self) -> None:
        _validate_telegram_bot_config({"telegram_bot": {"parse_mode": None}})

    def test_parse_mode_invalid_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be HTML, Markdown"):
            _validate_telegram_bot_config({"telegram_bot": {"parse_mode": "bbcode"}})

    def test_allowed_chat_ids_must_be_list(self) -> None:
        with pytest.raises(ConfigError, match="must be a list"):
            _validate_telegram_bot_config(
                {"telegram_bot": {"allowed_chat_ids": "not-list"}}
            )

    def test_media_enabled_must_be_bool(self) -> None:
        with pytest.raises(ConfigError, match="must be boolean"):
            _validate_telegram_bot_config(
                {"telegram_bot": {"media": {"enabled": "yes"}}}
            )

    def test_media_max_bytes_must_be_positive(self) -> None:
        with pytest.raises(ConfigError, match="must be greater than 0"):
            _validate_telegram_bot_config(
                {"telegram_bot": {"media": {"max_image_bytes": 0}}}
            )

    def test_shell_enabled_must_be_bool(self) -> None:
        with pytest.raises(ConfigError, match="must be boolean"):
            _validate_telegram_bot_config({"telegram_bot": {"shell": {"enabled": 1}}})

    def test_shell_timeout_must_be_positive(self) -> None:
        with pytest.raises(ConfigError, match="must be greater than 0"):
            _validate_telegram_bot_config(
                {"telegram_bot": {"shell": {"timeout_ms": 0}}}
            )

    def test_cache_ttl_must_be_positive(self) -> None:
        with pytest.raises(ConfigError, match="must be > 0"):
            _validate_telegram_bot_config(
                {"telegram_bot": {"cache": {"cleanup_interval_seconds": 0}}}
            )

    def test_command_registration_scopes_valid(self) -> None:
        _validate_telegram_bot_config(
            {"telegram_bot": {"command_registration": {"scopes": ["all"]}}}
        )

    def test_polling_timeout_must_be_positive(self) -> None:
        with pytest.raises(ConfigError, match="must be greater than 0"):
            _validate_telegram_bot_config(
                {"telegram_bot": {"polling": {"timeout_seconds": 0}}}
            )

    def test_defaults_approval_policy_must_be_str_or_null(self) -> None:
        _validate_telegram_bot_config(
            {"telegram_bot": {"defaults": {"approval_policy": None}}}
        )
        with pytest.raises(ConfigError, match="must be a string or null"):
            _validate_telegram_bot_config(
                {"telegram_bot": {"defaults": {"approval_policy": 42}}}
            )

    def test_agent_timeouts_must_be_number_or_null(self) -> None:
        _validate_telegram_bot_config(
            {"telegram_bot": {"agent_timeouts": {"default": 30, "slow": None}}}
        )
        with pytest.raises(ConfigError, match="must be numbers or null"):
            _validate_telegram_bot_config(
                {"telegram_bot": {"agent_timeouts": {"default": "slow"}}}
            )


class TestValidateDiscordBotConfig:
    def test_none_is_ok(self) -> None:
        _validate_discord_bot_config({})

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_discord_bot_config({"discord_bot": "bad"})

    def test_enabled_must_be_bool(self) -> None:
        with pytest.raises(ConfigError, match="must be boolean"):
            _validate_discord_bot_config({"discord_bot": {"enabled": 1}})

    def test_allowed_guild_ids_must_be_list(self) -> None:
        with pytest.raises(ConfigError, match="must be a list"):
            _validate_discord_bot_config(
                {"discord_bot": {"allowed_guild_ids": "not-list"}}
            )

    def test_allowed_guild_ids_must_contain_str_or_int(self) -> None:
        with pytest.raises(ConfigError, match="must contain only string/int"):
            _validate_discord_bot_config(
                {"discord_bot": {"allowed_guild_ids": [[1, 2]]}}
            )

    def test_intents_must_be_int(self) -> None:
        with pytest.raises(ConfigError, match="must be an integer"):
            _validate_discord_bot_config({"discord_bot": {"intents": "bad"}})

    def test_max_message_length_must_be_int(self) -> None:
        with pytest.raises(ConfigError, match="must be an integer"):
            _validate_discord_bot_config({"discord_bot": {"max_message_length": "big"}})

    def test_command_registration_must_be_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_discord_bot_config(
                {"discord_bot": {"command_registration": "bad"}}
            )

    def test_command_registration_scope_valid(self) -> None:
        _validate_discord_bot_config(
            {"discord_bot": {"command_registration": {"scope": "global"}}}
        )
        _validate_discord_bot_config(
            {"discord_bot": {"command_registration": {"scope": "guild"}}}
        )

    def test_command_registration_scope_invalid(self) -> None:
        with pytest.raises(ConfigError, match="must be 'global' or 'guild'"):
            _validate_discord_bot_config(
                {"discord_bot": {"command_registration": {"scope": "bad"}}}
            )

    def test_media_max_voice_bytes_must_be_positive(self) -> None:
        with pytest.raises(ConfigError, match="must be greater than 0"):
            _validate_discord_bot_config(
                {"discord_bot": {"media": {"max_voice_bytes": 0}}}
            )


class TestValidateHousekeepingConfig:
    def test_none_is_ok(self) -> None:
        _validate_housekeeping_config({})

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_housekeeping_config({"housekeeping": "bad"})

    def test_valid_config(self) -> None:
        _validate_housekeeping_config(
            {
                "housekeeping": {
                    "enabled": True,
                    "interval_seconds": 60,
                    "rules": [
                        {
                            "name": "clean-logs",
                            "kind": "directory",
                            "path": "logs",
                            "max_age_days": 7,
                        }
                    ],
                }
            }
        )

    @pytest.mark.parametrize(
        ("payload", "message"),
        (
            ({"interval_seconds": "60"}, "housekeeping.interval_seconds must be int"),
            ({"interval_seconds": 0}, "housekeeping.interval_seconds must be > 0"),
            (
                {"min_file_age_seconds": -1},
                "housekeeping.min_file_age_seconds must be >= 0",
            ),
            ({"dry_run": "yes"}, "housekeeping.dry_run must be bool"),
        ),
    )
    def test_registered_scalar_rules_reject_invalid_values(
        self, payload: dict[str, object], message: str
    ) -> None:
        with pytest.raises(ConfigError, match=message):
            _validate_housekeeping_config({"housekeeping": payload})

    def test_rules_must_be_list(self) -> None:
        with pytest.raises(ConfigError, match="must be a list"):
            _validate_housekeeping_config({"housekeeping": {"rules": "bad"}})

    def test_rule_must_be_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_housekeeping_config({"housekeeping": {"rules": ["bad"]}})

    def test_rule_kind_must_be_valid(self) -> None:
        with pytest.raises(ConfigError, match="must be 'directory' or 'file'"):
            _validate_housekeeping_config(
                {"housekeeping": {"rules": [{"kind": "bad"}]}}
            )

    def test_rule_path_must_be_relative(self) -> None:
        with pytest.raises(ConfigError, match="must be relative"):
            _validate_housekeeping_config(
                {"housekeeping": {"rules": [{"path": "/absolute"}]}}
            )

    def test_rule_path_must_not_contain_dots(self) -> None:
        with pytest.raises(ConfigError, match="must not contain"):
            _validate_housekeeping_config(
                {"housekeeping": {"rules": [{"path": "../escape"}]}}
            )

    @pytest.mark.parametrize(
        ("key", "value", "message"),
        (
            ("max_files", "10", r"housekeeping.rules\[0\].max_files must be int"),
            (
                "max_total_bytes",
                -1,
                r"housekeeping.rules\[0\].max_total_bytes must be >= 0",
            ),
            ("max_age_days", -1, r"housekeeping.rules\[0\].max_age_days must be >= 0"),
            ("max_bytes", -1, r"housekeeping.rules\[0\].max_bytes must be >= 0"),
            ("max_lines", -1, r"housekeeping.rules\[0\].max_lines must be >= 0"),
        ),
    )
    def test_registered_rule_field_rules_reject_invalid_values(
        self, key: str, value: object, message: str
    ) -> None:
        with pytest.raises(ConfigError, match=message):
            _validate_housekeeping_config({"housekeeping": {"rules": [{key: value}]}})


class TestValidateStaticAssetsConfig:
    def test_none_is_ok(self) -> None:
        _validate_static_assets_config({}, scope="hub")

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_static_assets_config({"static_assets": "bad"}, scope="hub")

    @pytest.mark.parametrize(
        ("key", "value", "message"),
        (
            (
                "cache_root",
                42,
                "hub.static_assets.cache_root must be str if provided",
            ),
            (
                "max_cache_entries",
                "10",
                "hub.static_assets.max_cache_entries must be int if provided",
            ),
            (
                "max_cache_entries",
                -1,
                "hub.static_assets.max_cache_entries must be >= 0",
            ),
            (
                "max_cache_age_days",
                "30",
                "hub.static_assets.max_cache_age_days must be int if provided",
            ),
            (
                "max_cache_age_days",
                -1,
                "hub.static_assets.max_cache_age_days must be >= 0",
            ),
        ),
    )
    def test_registered_rules_reject_invalid_values(
        self, key: str, value: object, message: str
    ) -> None:
        with pytest.raises(ConfigError, match=message):
            _validate_static_assets_config({"static_assets": {key: value}}, scope="hub")

    @pytest.mark.parametrize("key", ("cache_root", "max_cache_age_days"))
    def test_registered_rules_allow_null_for_nullable_fields(self, key: str) -> None:
        _validate_static_assets_config({"static_assets": {key: None}}, scope="hub")


class TestValidateCollaborationPolicyConfig:
    def test_none_is_ok(self) -> None:
        _validate_collaboration_policy_config({})

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_collaboration_policy_config({"collaboration_policy": "bad"})

    def test_actors_must_be_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_collaboration_policy_config(
                {"collaboration_policy": {"actors": "bad"}}
            )

    def test_valid_telegram_surface(self) -> None:
        _validate_collaboration_policy_config(
            {
                "collaboration_policy": {
                    "telegram": {
                        "default_mode": "active",
                        "destinations": [
                            {"chat_id": 123, "mode": "active"},
                        ],
                    }
                }
            }
        )

    def test_telegram_invalid_default_mode_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be one of"):
            _validate_collaboration_policy_config(
                {
                    "collaboration_policy": {
                        "telegram": {"default_mode": "invalid"},
                    }
                }
            )

    def test_telegram_destinations_must_be_list(self) -> None:
        with pytest.raises(ConfigError, match="must be a list"):
            _validate_collaboration_policy_config(
                {
                    "collaboration_policy": {
                        "telegram": {"destinations": "bad"},
                    }
                }
            )

    def test_telegram_destination_must_be_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            _validate_collaboration_policy_config(
                {
                    "collaboration_policy": {
                        "telegram": {"destinations": ["bad"]},
                    }
                }
            )

    def test_telegram_destination_chat_id_required(self) -> None:
        with pytest.raises(ConfigError, match="must be a string/int ID"):
            _validate_collaboration_policy_config(
                {
                    "collaboration_policy": {
                        "telegram": {"destinations": [{}]},
                    }
                }
            )

    def test_discord_valid_surface(self) -> None:
        _validate_collaboration_policy_config(
            {
                "collaboration_policy": {
                    "discord": {
                        "default_mode": "silent",
                        "destinations": [
                            {"channel_id": 456, "guild_id": 789},
                        ],
                    }
                }
            }
        )

    def test_id_list_must_be_list(self) -> None:
        with pytest.raises(ConfigError, match="must be a list"):
            _validate_collaboration_policy_config(
                {
                    "collaboration_policy": {
                        "telegram": {"allowed_chat_ids": "not-list"},
                    }
                }
            )

    def test_id_list_must_contain_str_or_int(self) -> None:
        with pytest.raises(ConfigError, match="must contain only string/int"):
            _validate_collaboration_policy_config(
                {
                    "collaboration_policy": {
                        "telegram": {"allowed_chat_ids": [[1]]},
                    }
                }
            )

    def test_telegram_require_topics_must_be_bool(self) -> None:
        with pytest.raises(ConfigError, match="must be boolean"):
            _validate_collaboration_policy_config(
                {
                    "collaboration_policy": {
                        "telegram": {"require_topics": "yes"},
                    }
                }
            )

    def test_telegram_destination_thread_id_can_be_none(self) -> None:
        _validate_collaboration_policy_config(
            {
                "collaboration_policy": {
                    "telegram": {
                        "destinations": [{"chat_id": 123, "thread_id": None}],
                    }
                }
            }
        )

    def test_telegram_destination_name_must_be_str(self) -> None:
        with pytest.raises(ConfigError, match="must be a string"):
            _validate_collaboration_policy_config(
                {
                    "collaboration_policy": {
                        "telegram": {
                            "destinations": [
                                {"chat_id": 123, "name": 42},
                            ],
                        }
                    }
                }
            )

    def test_trigger_mode_must_be_valid(self) -> None:
        with pytest.raises(ConfigError, match="must be one of"):
            _validate_collaboration_policy_config(
                {
                    "collaboration_policy": {
                        "telegram": {"trigger_mode": "invalid"},
                    }
                }
            )


def _minimal_repo_config(root: Path) -> dict:
    return {
        "version": CONFIG_VERSION,
        "mode": "repo",
        "docs": {
            "active_context": "active_context.md",
            "decisions": "decisions.md",
            "spec": "spec.md",
        },
        "codex": {
            "binary": "codex",
            "args": [],
        },
        "prompt": {"prev_run_max_chars": 10000},
        "runner": {"sleep_seconds": 5},
        "git": {"auto_commit": False},
        "server": {"host": "127.0.0.1", "port": 8080},
        "log": {"path": "test.log", "max_bytes": 1000, "backup_count": 3},
    }


class TestValidateRepoConfig:
    def test_valid_minimal_config(self, tmp_path: Path) -> None:
        _validate_repo_config(_minimal_repo_config(tmp_path), root=tmp_path)

    def test_wrong_mode_raises(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["mode"] = "hub"
        with pytest.raises(ConfigError, match="must set mode: repo"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_wrong_version_raises(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["version"] = 99
        with pytest.raises(ConfigError, match="Unsupported config version"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_docs_must_be_mapping(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["docs"] = "bad"
        with pytest.raises(ConfigError, match="docs must be a mapping"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_docs_values_must_be_nonempty_str(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["docs"]["active_context"] = ""
        with pytest.raises(ConfigError, match="must be a non-empty string"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_codex_binary_required(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        del cfg["codex"]["binary"]
        with pytest.raises(ConfigError, match="codex.binary is required"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_codex_args_must_be_list(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["codex"]["args"] = "bad"
        with pytest.raises(ConfigError, match="codex.args must be a list"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_codex_model_must_be_str_or_null(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["codex"]["model"] = 42
        with pytest.raises(ConfigError, match="must be a string or null"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_server_host_must_be_str(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["server"]["host"] = 42
        with pytest.raises(ConfigError, match="server.host must be a string"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_server_port_must_be_int(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["server"]["port"] = "8080"
        with pytest.raises(ConfigError, match="server.port must be an integer"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_git_auto_commit_must_be_bool(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["git"]["auto_commit"] = "yes"
        with pytest.raises(ConfigError, match="must be boolean"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_github_enabled_must_be_bool(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["github"] = {"enabled": "yes"}
        with pytest.raises(ConfigError, match="must be boolean"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_notifications_enabled_must_be_bool_or_auto(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["notifications"] = {"enabled": "auto"}
        _validate_repo_config(cfg, root=tmp_path)

        with pytest.raises(ConfigError, match="must be boolean, null, or 'auto'"):
            cfg = _minimal_repo_config(tmp_path)
            cfg["notifications"] = {"enabled": "yes"}
            _validate_repo_config(cfg, root=tmp_path)

    def test_notifications_events_must_be_list_of_str(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["notifications"] = {"events": [123]}
        with pytest.raises(ConfigError, match="must be a list of strings"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_terminal_idle_timeout_negative_raises(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["terminal"] = {"idle_timeout_seconds": -1}
        with pytest.raises(ConfigError, match="must be >= 0"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_ticket_flow_approval_mode_valid(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["ticket_flow"] = {"approval_mode": "yolo"}
        _validate_repo_config(cfg, root=tmp_path)

    def test_ticket_flow_approval_mode_invalid(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["ticket_flow"] = {"approval_mode": "bad"}
        with pytest.raises(ConfigError, match="must be one of"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_ui_editor_must_be_str(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["ui"] = {"editor": 42}
        with pytest.raises(ConfigError, match="must be a string"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_autorunner_reuse_session_must_be_bool(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["autorunner"] = {"reuse_session": "yes"}
        with pytest.raises(ConfigError, match="must be boolean or null"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_github_automation_policy_valid(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["github"] = {
            "enabled": True,
            "automation": {
                "enabled": True,
                "policy": {"merge_pr": "deny"},
            },
        }
        _validate_repo_config(cfg, root=tmp_path)

    def test_github_automation_policy_invalid_action(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["github"] = {
            "enabled": True,
            "automation": {
                "enabled": True,
                "policy": {"bogus_action": True},
            },
        }
        with pytest.raises(ConfigError, match="is not supported"):
            _validate_repo_config(cfg, root=tmp_path)

    def test_static_assets_max_cache_entries_negative(self, tmp_path: Path) -> None:
        cfg = _minimal_repo_config(tmp_path)
        cfg["static_assets"] = {"max_cache_entries": -1}
        with pytest.raises(ConfigError, match="must be >= 0"):
            _validate_repo_config(cfg, root=tmp_path)


class TestValidatorOwnsRejectionNotParser:
    """
    These tests verify that config_validation.py is the authority for
    rejecting invalid authored config values.  The parser layer should
    never see these values in the canonical load path.
    """

    def test_app_server_output_policy_rejected_by_validator(self) -> None:
        with pytest.raises(ConfigError, match="must be one of"):
            _validate_app_server_config(
                {"app_server": {"output": {"policy": "garbage"}}}
            )

    def test_opencode_server_scope_rejected_by_validator(self) -> None:
        with pytest.raises(ConfigError, match="must be 'workspace' or 'global'"):
            _validate_opencode_config({"opencode": {"server_scope": "everywhere"}})
