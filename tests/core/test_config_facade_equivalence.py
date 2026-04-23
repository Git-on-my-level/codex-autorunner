"""Facade-equivalence and regression tests for the extracted config modules.

These tests verify that ``core.config`` correctly delegates to the extracted
config modules and that any remaining compatibility exceptions are explicitly
documented.  If a future refactor accidentally re-introduces a divergent copy,
these tests will fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

import codex_autorunner.core.config as facade
from codex_autorunner.core import (
    agent_config,
    config_env,
    config_layering,
    config_parsers,
    config_types,
    config_validation,
)


class TestFacadeReexportIdentity:
    """Verify that backward-compat re-exports from core.config are the *same*
    Python objects as their canonical owners in the extracted modules."""

    def test_agent_types_identity(self) -> None:
        assert facade.AgentConfig is agent_config.AgentConfig
        assert facade.AgentProfileConfig is agent_config.AgentProfileConfig
        assert facade.ResolvedAgentTarget is agent_config.ResolvedAgentTarget

    def test_agent_functions_identity(self) -> None:
        assert facade.parse_agents_config is agent_config.parse_agents_config
        assert (
            facade.resolve_agent_target_from_agents
            is agent_config.resolve_agent_target_from_agents
        )

    def test_env_functions_identity(self) -> None:
        assert facade.resolve_env_for_root is config_env.resolve_env_for_root
        assert facade.load_dotenv_for_root is config_env.load_dotenv_for_root
        assert facade.collect_env_overrides is config_env.collect_env_overrides

    def test_layering_constants_identity(self) -> None:
        assert facade.DEFAULT_HUB_CONFIG is config_layering.DEFAULT_HUB_CONFIG
        assert facade.DEFAULT_REPO_CONFIG is config_layering.DEFAULT_REPO_CONFIG
        assert facade.GENERATED_CONFIG_HEADER is config_layering.GENERATED_CONFIG_HEADER
        assert facade.REPO_OVERRIDE_FILENAME is config_layering.REPO_OVERRIDE_FILENAME
        assert facade.CONFIG_FILENAME is config_layering.CONFIG_FILENAME
        assert facade.ROOT_CONFIG_FILENAME is config_layering.ROOT_CONFIG_FILENAME
        assert facade.ROOT_OVERRIDE_FILENAME is config_layering.ROOT_OVERRIDE_FILENAME

    def test_layering_functions_identity(self) -> None:
        assert facade.load_root_defaults is config_layering.load_root_defaults

    def test_validation_helpers_identity(self) -> None:
        assert facade.is_loopback_host is config_validation.is_loopback_host

    def test_parser_helpers_identity(self) -> None:
        assert (
            facade._APP_SERVER_OUTPUT_POLICIES
            is config_parsers._APP_SERVER_OUTPUT_POLICIES
        )
        assert facade._parse_optional_int is config_parsers._parse_optional_int
        assert facade._parse_prompt_int is config_parsers._parse_prompt_int
        assert (
            facade._parse_security_config_section
            is config_parsers._parse_security_config_section
        )
        assert (
            facade._parse_notification_target_section
            is config_parsers._parse_notification_target_section
        )
        assert (
            facade._parse_notifications_config_section
            is config_parsers._parse_notifications_config_section
        )
        assert (
            facade._parse_voice_config_section
            is config_parsers._parse_voice_config_section
        )
        assert (
            facade._parse_destination_config_section
            is config_parsers._parse_destination_config_section
        )
        assert (
            facade._parse_ticket_flow_config is config_parsers._parse_ticket_flow_config
        )
        assert (
            facade._parse_app_server_output_config
            is config_parsers._parse_app_server_output_config
        )
        assert (
            facade._parse_app_server_config is config_parsers._parse_app_server_config
        )
        assert facade._parse_opencode_config is config_parsers._parse_opencode_config
        assert facade._parse_pma_config is config_parsers._parse_pma_config
        assert facade._parse_usage_config is config_parsers._parse_usage_config
        assert facade._parse_templates_config is config_parsers._parse_templates_config
        assert (
            facade._parse_static_assets_config
            is config_parsers._parse_static_assets_config
        )
        assert facade._parse_update_backend is config_parsers._parse_update_backend
        assert (
            facade._parse_update_linux_service_names
            is config_parsers._parse_update_linux_service_names
        )
        assert facade.normalize_base_path is config_parsers.normalize_base_path
        assert (
            facade.parse_flow_retention_config
            is config_parsers.parse_flow_retention_config
        )

    def test_config_types_identity(self) -> None:
        assert facade.FlowRetentionConfig is config_types.FlowRetentionConfig
        assert facade.LogConfig is config_types.LogConfig
        assert facade.HubConfig is config_types.HubConfig
        assert facade.RepoConfig is config_types.RepoConfig
        assert facade.OpenCodeConfig is config_types.OpenCodeConfig
        assert facade.PmaConfig is config_types.PmaConfig
        assert facade.TicketFlowConfig is config_types.TicketFlowConfig
        assert facade.UsageConfig is config_types.UsageConfig
        assert facade.AppServerConfig is config_types.AppServerConfig
        assert facade.StaticAssetsConfig is config_types.StaticAssetsConfig
        assert facade.TemplatesConfig is config_types.TemplatesConfig


class TestConstantEquivalence:
    """Verify that independently-defined constants in core.config match their
    canonical values in the extracted modules.  These are NOT identity tests
    because the constants are duplicated rather than re-exported."""

    def test_twelve_hour_seconds(self) -> None:
        assert facade.TWELVE_HOUR_SECONDS == config_layering.TWELVE_HOUR_SECONDS

    def test_pma_default_turn_timeout_seconds(self) -> None:
        assert (
            facade.PMA_DEFAULT_TURN_TIMEOUT_SECONDS
            == config_layering.PMA_DEFAULT_TURN_TIMEOUT_SECONDS
        )

    def test_pma_default_turn_idle_timeout_seconds(self) -> None:
        assert (
            facade.PMA_DEFAULT_TURN_IDLE_TIMEOUT_SECONDS
            == config_layering.PMA_DEFAULT_TURN_IDLE_TIMEOUT_SECONDS
        )

    def test_pma_default_max_text_chars(self) -> None:
        assert (
            facade.PMA_DEFAULT_MAX_TEXT_CHARS
            == config_layering.PMA_DEFAULT_MAX_TEXT_CHARS
        )

    def test_app_server_output_policies(self) -> None:
        assert (
            facade._APP_SERVER_OUTPUT_POLICIES
            == config_parsers._APP_SERVER_OUTPUT_POLICIES
        )


class TestParserOutputEquivalence:
    """Verify that duplicate parser functions in core.config produce the same
    results as their canonical implementations in config_parsers."""

    def test_parse_security_config_section(self) -> None:
        inputs: list[object] = [
            None,
            {},
            {"redact_run_logs": True},
            {"redact_run_logs": False, "redact_patterns": ["s3://.*"]},
        ]
        for raw in inputs:
            assert facade._parse_security_config_section(
                raw
            ) == config_parsers._parse_security_config_section(
                raw
            ), f"diverged for input {raw!r}"

    def test_parse_notification_target_section(self) -> None:
        inputs: list[object] = [
            None,
            {},
            {"enabled": True, "webhook_url_env": "MY_WEBHOOK"},
            {"enabled": "not-bool", "bot_token_env": "  "},
        ]
        for raw in inputs:
            assert facade._parse_notification_target_section(
                raw
            ) == config_parsers._parse_notification_target_section(
                raw
            ), f"diverged for input {raw!r}"

    def test_parse_notifications_config_section(self) -> None:
        inputs: list[object] = [
            None,
            {},
            {"enabled": True},
            {"enabled": "auto"},
            {"events": ["run_start", "run_end"], "tui_idle_seconds": 30},
            {"timeout_seconds": 10.5},
        ]
        for raw in inputs:
            assert facade._parse_notifications_config_section(
                raw
            ) == config_parsers._parse_notifications_config_section(
                raw
            ), f"diverged for input {raw!r}"

    def test_parse_voice_config_section(self) -> None:
        inputs: list[object] = [
            None,
            {},
            {"enabled": True, "provider": "local_whisper", "latency_mode": "low"},
            {"chunk_ms": 500, "sample_rate": 16000, "warn_on_remote_api": False},
            {
                "push_to_talk": {"key": "space"},
                "providers": {"local_whisper": {"model": "base"}},
            },
        ]
        for raw in inputs:
            assert facade._parse_voice_config_section(
                raw
            ) == config_parsers._parse_voice_config_section(
                raw
            ), f"diverged for input {raw!r}"

    def test_parse_ticket_flow_config(self) -> None:
        pairs: list[tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = [
            (None, None),
            ({}, {}),
            ({"approval_mode": "yolo"}, {}),
            ({"approval_mode": "safe"}, {}),
            ({"approval_mode": "review", "default_approval_decision": "cancel"}, {}),
            (
                {"approval_mode": "yolo", "auto_resume": True, "max_total_turns": 10},
                {"include_previous_ticket_context": True},
            ),
        ]
        for cfg, defaults in pairs:
            assert facade._parse_ticket_flow_config(
                cfg, defaults
            ) == config_parsers._parse_ticket_flow_config(
                cfg, defaults
            ), f"diverged for cfg={cfg!r} defaults={defaults!r}"

    def test_parse_optional_int(self) -> None:
        for value in [None, 0, 42, 3.14]:
            assert facade._parse_optional_int(
                value
            ) == config_parsers._parse_optional_int(value)

    def test_parse_prompt_int(self) -> None:
        for cfg, defaults, key in [
            ({}, {}, "x"),
            ({"x": 5}, {}, "x"),
            ({}, {"x": 10}, "x"),
            ({"x": None}, {"x": 10}, "x"),
        ]:
            assert facade._parse_prompt_int(
                cfg, defaults, key
            ) == config_parsers._parse_prompt_int(cfg, defaults, key)

    def test_parse_update_backend(self) -> None:
        for update_cfg in [
            {},
            {"backend": None},
            {"backend": "auto"},
            {"backend": "  "},
            {"backend": "pip"},
        ]:
            assert facade._parse_update_backend(
                update_cfg
            ) == config_parsers._parse_update_backend(update_cfg)

    def test_parse_opencode_config(self, tmp_path: Path) -> None:
        pairs: list[tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = [
            (None, None),
            ({}, {}),
            ({"server_scope": "workspace"}, {}),
            ({"server_scope": "global"}, {}),
            ({"max_text_chars": 5000, "max_handles": 8}, {}),
            ({"idle_ttl_seconds": 600, "session_stall_timeout_seconds": 30}, {}),
        ]
        for cfg, defaults in pairs:
            assert facade._parse_opencode_config(
                cfg, tmp_path, defaults
            ) == config_parsers._parse_opencode_config(
                cfg, tmp_path, defaults
            ), f"diverged for cfg={cfg!r}"

    def test_parse_app_server_output_config_with_defaults(self) -> None:
        defaults = {"policy": "final_only"}
        facade_result = facade._parse_app_server_output_config(None, defaults)
        canon_result = config_parsers._parse_app_server_output_config(None, defaults)
        assert facade_result == canon_result

    def test_parse_app_server_output_config_explicit_policy(self) -> None:
        for policy in ["final_only", "all_agent_messages"]:
            facade_result = facade._parse_app_server_output_config(
                {"policy": policy}, None
            )
            canon_result = config_parsers._parse_app_server_output_config(
                {"policy": policy}, None
            )
            assert facade_result == canon_result


class TestFacadeParserDelegation:
    def test_parse_app_server_output_config_defaults_through_canonical_owner(
        self,
    ) -> None:
        result = facade._parse_app_server_output_config(None, None)
        assert result.policy == "final_only"

    def test_parse_app_server_config_preserves_canonical_path_errors(
        self, tmp_path: Path
    ) -> None:
        from codex_autorunner.core.config_contract import ConfigError

        with pytest.raises(ConfigError, match="app_server.state_root"):
            facade._parse_app_server_config(
                {"state_root": "../outside"},
                tmp_path,
                {},
            )


class TestPrivateHelperAliases:
    """Verify that private helper aliases in core.config point to their
    canonical implementations."""

    def test_normalize_base_path_alias(self) -> None:
        assert facade.normalize_base_path is config_parsers.normalize_base_path

    def test_is_loopback_host_alias(self) -> None:
        assert facade.is_loopback_host is config_validation.is_loopback_host


class TestBuilderDelegation:
    """Verify that the facade delegates load functions to config_builders."""

    def test_load_hub_config_comes_from_builders(self) -> None:
        from codex_autorunner.core.config_builders import load_hub_config

        assert facade.load_hub_config is load_hub_config

    def test_load_repo_config_comes_from_builders(self) -> None:
        from codex_autorunner.core.config_builders import load_repo_config

        assert facade.load_repo_config is load_repo_config

    def test_derive_repo_config_comes_from_builders(self) -> None:
        from codex_autorunner.core.config_builders import derive_repo_config

        assert facade.derive_repo_config is derive_repo_config

    def test_builders_import_parsers_not_facade(self) -> None:
        import codex_autorunner.core.config_builders as builders

        assert (
            builders._parse_security_config_section
            is config_parsers._parse_security_config_section
        )
        assert (
            builders._parse_app_server_config is config_parsers._parse_app_server_config
        )
        assert builders._parse_opencode_config is config_parsers._parse_opencode_config
        assert builders._parse_pma_config is config_parsers._parse_pma_config
        assert builders._parse_usage_config is config_parsers._parse_usage_config
