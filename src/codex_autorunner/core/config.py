"""Public config facade for codex-autorunner.

This module re-exports the stable public API for configuration loading,
validation, and type access.  All loading and builder logic lives in
``config_builders``; sub-modules (``config_layering``, ``config_parsers``,
``config_types``, ``config_validation``, ``config_env``) own their own
concerns.
"""

from .config_builders import (
    ACTIVE_HUB_ROOT_ENV,
    derive_repo_config,
    ensure_hub_config_at,
    load_hub_config,
    load_hub_config_data,
    load_repo_config,
)
from .config_contract import (
    CONFIG_VERSION,
    ConfigError,
)
from .config_env import collect_env_overrides
from .config_layering import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    DEFAULT_REPO_CONFIG,
    GENERATED_CONFIG_HEADER,
    REPO_OVERRIDE_FILENAME,
    ROOT_CONFIG_FILENAME,
    ROOT_OVERRIDE_FILENAME,
    find_nearest_hub_config_path,
)
from .config_parsers import parse_flow_retention_config
from .config_types import (
    AppRepoConfig,
    AppsConfig,
    AppServerAutorunnerPromptConfig,
    AppServerClientConfig,
    AppServerConfig,
    AppServerDocChatPromptConfig,
    AppServerOutputConfig,
    AppServerPromptsConfig,
    AppServerSpecIngestPromptConfig,
    DestinationConfigSection,
    FlowRetentionConfig,
    HubConfig,
    LogConfig,
    NotificationsConfigSection,
    NotificationTargetSection,
    OpenCodeConfig,
    PmaConfig,
    RepoConfig,
    SecurityConfigSection,
    StaticAssetsConfig,
    TemplateRepoConfig,
    TemplatesConfig,
    TicketFlowConfig,
    UsageConfig,
    VoiceConfigSection,
)
from .path_utils import ConfigPathError

__all__ = [
    "ACTIVE_HUB_ROOT_ENV",
    "CONFIG_FILENAME",
    "CONFIG_VERSION",
    "ConfigError",
    "ConfigPathError",
    "DEFAULT_HUB_CONFIG",
    "DEFAULT_REPO_CONFIG",
    "GENERATED_CONFIG_HEADER",
    "REPO_OVERRIDE_FILENAME",
    "ROOT_CONFIG_FILENAME",
    "ROOT_OVERRIDE_FILENAME",
    "AppServerAutorunnerPromptConfig",
    "AppServerClientConfig",
    "AppServerConfig",
    "AppServerDocChatPromptConfig",
    "AppServerOutputConfig",
    "AppServerPromptsConfig",
    "AppServerSpecIngestPromptConfig",
    "AppRepoConfig",
    "AppsConfig",
    "DestinationConfigSection",
    "FlowRetentionConfig",
    "HubConfig",
    "LogConfig",
    "NotificationsConfigSection",
    "NotificationTargetSection",
    "OpenCodeConfig",
    "PmaConfig",
    "RepoConfig",
    "SecurityConfigSection",
    "StaticAssetsConfig",
    "TemplateRepoConfig",
    "TemplatesConfig",
    "TicketFlowConfig",
    "UsageConfig",
    "VoiceConfigSection",
    "collect_env_overrides",
    "derive_repo_config",
    "ensure_hub_config_at",
    "find_nearest_hub_config_path",
    "load_hub_config",
    "load_hub_config_data",
    "load_repo_config",
    "parse_flow_retention_config",
]
