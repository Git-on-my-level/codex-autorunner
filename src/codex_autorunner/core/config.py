"""Public config facade for codex-autorunner.

This module re-exports the stable public API for configuration loading,
validation, and type access.  All loading and builder logic lives in
``config_builders``; sub-modules (``config_layering``, ``config_parsers``,
``config_types``, ``config_validation``, ``config_env``) own their own
concerns.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..housekeeping import (
    HousekeepingConfig,
    HousekeepingRule,
    parse_housekeeping_config,
)
from .agent_config import (  # noqa: F401 — backward-compat re-exports
    AgentConfig,
    AgentProfileConfig,
    ResolvedAgentTarget,
    parse_agents_config,
    resolve_agent_target_from_agents,
)
from .config_builders import (  # noqa: F401 — backward-compat re-exports
    ACTIVE_HUB_ROOT_ENV,
    derive_repo_config,
    ensure_hub_config_at,
    load_hub_config,
    load_hub_config_data,
    load_repo_config,
)
from .config_contract import (  # noqa: F401 — backward-compat re-exports
    CONFIG_VERSION,
    ConfigError,
)
from .config_env import (  # noqa: F401 — backward-compat re-exports
    collect_env_overrides,
    load_dotenv_for_root,
    resolve_env_for_root,
)
from .config_layering import (  # noqa: F401 — backward-compat re-exports
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    DEFAULT_REPO_CONFIG,
    GENERATED_CONFIG_HEADER,
    PMA_DEFAULT_MAX_TEXT_CHARS,
    REPO_OVERRIDE_FILENAME,
    ROOT_CONFIG_FILENAME,
    ROOT_OVERRIDE_FILENAME,
    _default_housekeeping_section,
    _load_yaml_dict,
    derive_repo_config_data,
    find_nearest_hub_config_path,
    load_root_defaults,
    resolve_hub_config_data,
)
from .config_parsers import (  # noqa: F401 — backward-compat re-exports
    _APP_SERVER_OUTPUT_POLICIES,
    _parse_app_server_config,
    _parse_app_server_output_config,
    _parse_destination_config_section,
    _parse_notification_target_section,
    _parse_notifications_config_section,
    _parse_opencode_config,
    _parse_optional_int,
    _parse_pma_config,
    _parse_prompt_int,
    _parse_security_config_section,
    _parse_static_assets_config,
    _parse_templates_config,
    _parse_ticket_flow_config,
    _parse_update_backend,
    _parse_update_linux_service_names,
    _parse_usage_config,
    _parse_voice_config_section,
    normalize_base_path,
    parse_flow_retention_config,
)
from .config_types import (  # noqa: F401 — backward-compat re-exports
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
from .config_validation import (  # noqa: F401 — backward-compat re-exports
    is_loopback_host,
)
from .path_utils import ConfigPathError
from .utils import atomic_write

TWELVE_HOUR_SECONDS = 12 * 60 * 60
PMA_DEFAULT_TURN_IDLE_TIMEOUT_SECONDS = 1800
PMA_DEFAULT_TURN_TIMEOUT_SECONDS = PMA_DEFAULT_TURN_IDLE_TIMEOUT_SECONDS

Config = RepoConfig

__all__ = [
    "ConfigError",
    "ConfigPathError",
]


def resolve_housekeeping_rule(
    config: object,
    name: str,
) -> Optional[HousekeepingRule]:
    if not isinstance(config, HousekeepingConfig):
        return None
    wanted = name.strip().lower()
    if not wanted:
        return None
    for rule in config.rules:
        if rule.name.strip().lower() == wanted:
            return rule
    return None


def default_housekeeping_rule_named(
    name: str,
    *,
    include_repo_review_runs: bool = False,
    include_hub_update_rules: bool = False,
) -> Optional[HousekeepingRule]:
    default_config = parse_housekeeping_config(
        _default_housekeeping_section(
            include_repo_review_runs=include_repo_review_runs,
            include_hub_update_rules=include_hub_update_rules,
        )
    )
    return resolve_housekeeping_rule(default_config, name)


def update_override_templates(repo_root: Path, repos: List[Dict[str, Any]]) -> None:
    """
    Update templates.repos in the root override file, preserving other settings.

    This writes to ``codex-autorunner.override.yml`` (gitignored) at the provided repo_root.
    """
    override_path = repo_root / ROOT_OVERRIDE_FILENAME
    data = _load_yaml_dict(override_path)
    templates = data.get("templates")
    if templates is None or not isinstance(templates, dict):
        templates = {}
        data["templates"] = templates
    templates["repos"] = list(repos or [])
    rendered = yaml.safe_dump(data, sort_keys=False).rstrip() + "\n"
    atomic_write(override_path, rendered)
