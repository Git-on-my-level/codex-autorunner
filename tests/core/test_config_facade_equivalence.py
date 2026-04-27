"""Contract and identity tests for the slimmed core.config facade.

These tests verify that ``core.config`` exports only the approved public
surface (as defined in TICKET-001) and that each re-export is identity-
equivalent to its canonical owner module.
"""

from __future__ import annotations

import pytest

import codex_autorunner.core.config as facade
from codex_autorunner.core import (
    config_builders,
    config_contract,
    config_env,
    config_layering,
    config_parsers,
    config_types,
)
from codex_autorunner.core.path_utils import ConfigPathError


class TestApprovedFacadeIdentity:
    """Every name in ``core.config.__all__`` must resolve to the same object
    in its canonical owner module."""

    def test_config_error_identity(self) -> None:
        assert facade.ConfigError is config_contract.ConfigError

    def test_config_path_error_identity(self) -> None:
        assert facade.ConfigPathError is ConfigPathError

    def test_config_version_identity(self) -> None:
        assert facade.CONFIG_VERSION is config_contract.CONFIG_VERSION

    def test_active_hub_root_env_identity(self) -> None:
        assert facade.ACTIVE_HUB_ROOT_ENV is config_builders.ACTIVE_HUB_ROOT_ENV

    def test_config_filename_identity(self) -> None:
        assert facade.CONFIG_FILENAME is config_layering.CONFIG_FILENAME

    def test_root_config_filename_identity(self) -> None:
        assert facade.ROOT_CONFIG_FILENAME is config_layering.ROOT_CONFIG_FILENAME

    def test_root_override_filename_identity(self) -> None:
        assert facade.ROOT_OVERRIDE_FILENAME is config_layering.ROOT_OVERRIDE_FILENAME

    def test_repo_override_filename_identity(self) -> None:
        assert facade.REPO_OVERRIDE_FILENAME is config_layering.REPO_OVERRIDE_FILENAME

    def test_generated_config_header_identity(self) -> None:
        assert facade.GENERATED_CONFIG_HEADER is config_layering.GENERATED_CONFIG_HEADER

    def test_default_hub_config_identity(self) -> None:
        assert facade.DEFAULT_HUB_CONFIG is config_layering.DEFAULT_HUB_CONFIG

    def test_default_repo_config_identity(self) -> None:
        assert facade.DEFAULT_REPO_CONFIG is config_layering.DEFAULT_REPO_CONFIG

    def test_load_hub_config_identity(self) -> None:
        assert facade.load_hub_config is config_builders.load_hub_config

    def test_load_hub_config_data_identity(self) -> None:
        assert facade.load_hub_config_data is config_builders.load_hub_config_data

    def test_load_repo_config_identity(self) -> None:
        assert facade.load_repo_config is config_builders.load_repo_config

    def test_derive_repo_config_identity(self) -> None:
        assert facade.derive_repo_config is config_builders.derive_repo_config

    def test_ensure_hub_config_at_identity(self) -> None:
        assert facade.ensure_hub_config_at is config_builders.ensure_hub_config_at

    def test_find_nearest_hub_config_path_identity(self) -> None:
        assert (
            facade.find_nearest_hub_config_path
            is config_layering.find_nearest_hub_config_path
        )

    def test_parse_flow_retention_config_identity(self) -> None:
        assert (
            facade.parse_flow_retention_config
            is config_parsers.parse_flow_retention_config
        )

    def test_collect_env_overrides_identity(self) -> None:
        assert facade.collect_env_overrides is config_env.collect_env_overrides


class TestConfigTypeIdentity:
    """Config dataclasses re-exported through the facade."""

    @pytest.mark.parametrize(
        "name",
        [
            "FlowRetentionConfig",
            "LogConfig",
            "HubConfig",
            "RepoConfig",
            "OpenCodeConfig",
            "PmaConfig",
            "TicketFlowConfig",
            "UsageConfig",
            "AppServerConfig",
            "AppServerClientConfig",
            "AppServerOutputConfig",
            "AppServerPromptsConfig",
            "AppServerAutorunnerPromptConfig",
            "AppServerDocChatPromptConfig",
            "AppServerSpecIngestPromptConfig",
            "StaticAssetsConfig",
            "TemplatesConfig",
            "TemplateRepoConfig",
            "SecurityConfigSection",
            "NotificationsConfigSection",
            "NotificationTargetSection",
            "DestinationConfigSection",
            "VoiceConfigSection",
        ],
    )
    def test_type_identity(self, name: str) -> None:
        facade_obj = getattr(facade, name)
        canonical_obj = getattr(config_types, name)
        assert facade_obj is canonical_obj


class TestFacadeExcludesRemovedNames:
    """Verify that names removed in the slimming are no longer present."""

    REMOVED_NAMES = [
        "_parse_app_server_config",
        "_parse_app_server_output_config",
        "_parse_destination_config_section",
        "_parse_notification_target_section",
        "_parse_notifications_config_section",
        "_parse_opencode_config",
        "_parse_optional_int",
        "_parse_pma_config",
        "_parse_prompt_int",
        "_parse_security_config_section",
        "_parse_static_assets_config",
        "_parse_templates_config",
        "_parse_ticket_flow_config",
        "_parse_update_backend",
        "_parse_update_linux_service_names",
        "_parse_usage_config",
        "_parse_voice_config_section",
        "_APP_SERVER_OUTPUT_POLICIES",
        "_default_housekeeping_section",
        "_load_yaml_dict",
        "derive_repo_config_data",
        "load_root_defaults",
        "resolve_hub_config_data",
        "PMA_DEFAULT_MAX_TEXT_CHARS",
        "normalize_base_path",
        "load_dotenv_for_root",
        "resolve_env_for_root",
        "is_loopback_host",
        "HousekeepingConfig",
        "HousekeepingRule",
        "parse_housekeeping_config",
        "AgentConfig",
        "AgentProfileConfig",
        "ResolvedAgentTarget",
        "parse_agents_config",
        "resolve_agent_target_from_agents",
        "Config",
        "TWELVE_HOUR_SECONDS",
        "PMA_DEFAULT_TURN_IDLE_TIMEOUT_SECONDS",
        "PMA_DEFAULT_TURN_TIMEOUT_SECONDS",
        "resolve_housekeeping_rule",
        "default_housekeeping_rule_named",
        "update_override_templates",
    ]

    @pytest.mark.parametrize("name", REMOVED_NAMES)
    def test_name_absent(self, name: str) -> None:
        assert not hasattr(
            facade, name
        ), f"{name!r} should have been removed from core.config"


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
