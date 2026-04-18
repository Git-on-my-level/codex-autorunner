import logging
import os
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

import yaml

from ..housekeeping import (
    HousekeepingConfig,
    HousekeepingRule,
    parse_housekeeping_config,
)
from ..manifest import ManifestError, load_manifest
from .agent_config import (  # noqa: F401 — backward-compat re-exports
    AgentConfig,
    AgentProfileConfig,
    ResolvedAgentTarget,
    parse_agents_config,
    resolve_agent_target_from_agents,
)
from .config_contract import CONFIG_VERSION, ConfigError
from .destinations import default_local_destination, resolve_effective_repo_destination
from .path_utils import ConfigPathError
from .utils import atomic_write

logger = logging.getLogger("codex_autorunner.core.config")

ACTIVE_HUB_ROOT_ENV = "CAR_HUB_ROOT"
TWELVE_HOUR_SECONDS = 12 * 60 * 60
PMA_DEFAULT_TURN_TIMEOUT_SECONDS = 7200

from .config_builders import (  # noqa: E402
    build_hub_config as _build_hub_config_impl,
)
from .config_builders import (  # noqa: E402
    build_repo_config as _build_repo_config_impl,
)
from .config_env import (  # noqa: E402
    collect_env_overrides,  # noqa: F401 — backward-compat re-export
    load_dotenv_for_root,  # noqa: F401 — backward-compat re-export
    resolve_env_for_root,  # noqa: F401 — backward-compat re-export
)
from .config_layering import (  # noqa: E402
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,  # noqa: F401 — backward-compat re-export
    DEFAULT_REPO_CONFIG,  # noqa: F401 — backward-compat re-export
    GENERATED_CONFIG_HEADER,  # noqa: F401 — backward-compat re-export
    PMA_DEFAULT_MAX_TEXT_CHARS,  # noqa: F401 — backward-compat re-export
    REPO_OVERRIDE_FILENAME,  # noqa: F401 — backward-compat re-export
    ROOT_CONFIG_FILENAME,  # noqa: F401 — backward-compat re-export
    ROOT_OVERRIDE_FILENAME,
    _default_housekeeping_section,
    _load_yaml_dict,
    derive_repo_config_data,
    find_nearest_hub_config_path,
    load_root_defaults,  # noqa: F401 — backward-compat re-export
    resolve_hub_config_data,
)
from .generated_hub_config import normalize_generated_hub_config  # noqa: E402


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


# Import validation helpers after approval-mode constants are defined.
from .config_validation import (  # noqa: E402,I001
    _validate_hub_config,
    _validate_repo_config,
    is_loopback_host,  # noqa: F401 — backward-compat re-export
)

__all__ = [
    "ConfigError",
    "ConfigPathError",
]


_is_loopback_host = is_loopback_host


from .config_parsers import (  # noqa: E402
    _APP_SERVER_OUTPUT_POLICIES,  # noqa: F401 — backward-compat re-export
    _parse_app_server_config,  # noqa: F401 — backward-compat re-export
    _parse_app_server_output_config,  # noqa: F401 — backward-compat re-export
    _parse_destination_config_section,  # noqa: F401 — backward-compat re-export
    _parse_notification_target_section,  # noqa: F401 — backward-compat re-export
    _parse_notifications_config_section,  # noqa: F401 — backward-compat re-export
    _parse_opencode_config,  # noqa: F401 — backward-compat re-export
    _parse_optional_int,  # noqa: F401 — backward-compat re-export
    _parse_pma_config,  # noqa: F401 — backward-compat re-export
    _parse_prompt_int,  # noqa: F401 — backward-compat re-export
    _parse_security_config_section,  # noqa: F401 — backward-compat re-export
    _parse_static_assets_config,  # noqa: F401 — backward-compat re-export
    _parse_templates_config,  # noqa: F401 — backward-compat re-export
    _parse_ticket_flow_config,  # noqa: F401 — backward-compat re-export
    _parse_update_backend,  # noqa: F401 — backward-compat re-export
    _parse_update_linux_service_names,  # noqa: F401 — backward-compat re-export
    _parse_usage_config,  # noqa: F401 — backward-compat re-export
    _parse_voice_config_section,  # noqa: F401 — backward-compat re-export
    normalize_base_path,  # noqa: F401 — backward-compat re-export
    parse_flow_retention_config,  # noqa: F401 — backward-compat re-export
)
from .config_types import (  # noqa: E402
    AppServerAutorunnerPromptConfig,  # noqa: F401 — backward-compat re-export
    AppServerClientConfig,  # noqa: F401 — backward-compat re-export
    AppServerConfig,  # noqa: F401 — backward-compat re-export
    AppServerDocChatPromptConfig,  # noqa: F401 — backward-compat re-export
    AppServerOutputConfig,  # noqa: F401 — backward-compat re-export
    AppServerPromptsConfig,  # noqa: F401 — backward-compat re-export
    AppServerSpecIngestPromptConfig,  # noqa: F401 — backward-compat re-export
    DestinationConfigSection,  # noqa: F401 — backward-compat re-export
    FlowRetentionConfig,  # noqa: F401 — backward-compat re-export
    HubConfig,
    LogConfig,  # noqa: F401 — backward-compat re-export
    NotificationsConfigSection,  # noqa: F401 — backward-compat re-export
    NotificationTargetSection,  # noqa: F401 — backward-compat re-export
    OpenCodeConfig,  # noqa: F401 — backward-compat re-export
    PmaConfig,  # noqa: F401 — backward-compat re-export
    RepoConfig,
    SecurityConfigSection,  # noqa: F401 — backward-compat re-export
    StaticAssetsConfig,  # noqa: F401 — backward-compat re-export
    TemplateRepoConfig,  # noqa: F401 — backward-compat re-export
    TemplatesConfig,  # noqa: F401 — backward-compat re-export
    TicketFlowConfig,  # noqa: F401 — backward-compat re-export
    UsageConfig,  # noqa: F401 — backward-compat re-export
    VoiceConfigSection,  # noqa: F401 — backward-compat re-export
)

# Alias used by existing code paths that only support repo mode
Config = RepoConfig
_parse_agents_config = parse_agents_config


def update_override_templates(repo_root: Path, repos: List[Dict[str, Any]]) -> None:
    """
    Update templates.repos in the root override file, preserving other settings.

    This writes to `codex-autorunner.override.yml` (gitignored) at the provided repo_root.
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


_normalize_base_path = normalize_base_path


def load_hub_config_data(config_path: Path) -> Dict[str, Any]:
    """Load, merge, and return a raw hub config dict for the given config path."""
    load_dotenv_for_root(config_path.parent.parent.resolve())
    data = normalize_generated_hub_config(config_path)
    mode = data.get("mode")
    if mode not in (None, "hub"):
        raise ConfigError(f"Invalid mode '{mode}'; expected 'hub'")
    root = config_path.parent.parent.resolve()
    return resolve_hub_config_data(root, data)


def _resolve_hub_config_path(start: Path) -> Path:
    config_path = find_nearest_hub_config_path(start)
    if not config_path:
        raise ConfigError(
            f"Missing hub config file; expected to find {CONFIG_FILENAME} in {start} or parents "
            "(pass --path on most commands, --hub-root on car render, or run 'car init' to initialize)"
        )
    return config_path


def ensure_hub_config_at(start: Path) -> tuple[Path, bool]:
    """
    Ensure a hub config exists at or above the given start path.

    Returns a tuple of (config_path, did_initialize) where:
    - config_path is the path to the hub config file
    - did_initialize is True if we created a new config, False if it already existed
    """
    existing = find_nearest_hub_config_path(start)
    if existing:
        return (existing, False)

    try:
        from .utils import find_repo_root

        target_root = find_repo_root(start)
    except Exception:  # intentional: find_repo_root may raise undocumented exceptions
        target_root = start

    from ..bootstrap import seed_hub_files

    seed_hub_files(target_root)
    new_path = find_nearest_hub_config_path(target_root)
    if not new_path:
        raise ConfigError(f"Failed to initialize hub config at {target_root}")
    return (new_path, True)


def load_hub_config(start: Path) -> HubConfig:
    """Load the nearest hub config walking upward from the provided path."""
    config_path = _resolve_hub_config_path(start)
    merged = load_hub_config_data(config_path)
    _validate_hub_config(merged, root=config_path.parent.parent.resolve())
    return _build_hub_config(config_path, merged)


def _resolve_hub_path_for_repo(repo_root: Path, hub_path: Optional[Path]) -> Path:
    if hub_path:
        candidate = hub_path
        if candidate.is_dir():
            candidate = candidate / CONFIG_FILENAME
        if not candidate.exists():
            raise ConfigError(f"Hub config not found at {candidate}")
        data = _load_yaml_dict(candidate)
        mode = data.get("mode")
        if mode not in (None, "hub"):
            raise ConfigError(f"Invalid hub config mode '{mode}'; expected 'hub'")
        return candidate
    local_candidate = find_nearest_hub_config_path(repo_root)
    if local_candidate:
        return local_candidate
    env_hub_root = os.environ.get(ACTIVE_HUB_ROOT_ENV, "").strip()
    if env_hub_root:
        candidate = Path(env_hub_root).expanduser()
        if candidate.is_dir():
            candidate = candidate / CONFIG_FILENAME
        if candidate.exists():
            data = _load_yaml_dict(candidate)
            mode = data.get("mode")
            if mode not in (None, "hub"):
                raise ConfigError(
                    f"Invalid hub config mode '{mode}' from {ACTIVE_HUB_ROOT_ENV}; expected 'hub'"
                )
            return candidate
    return _resolve_hub_config_path(repo_root)


def derive_repo_config(
    hub: HubConfig, repo_root: Path, *, load_env: bool = True
) -> RepoConfig:
    if load_env:
        load_dotenv_for_root(repo_root)
    merged = derive_repo_config_data(hub.raw, repo_root)
    merged["mode"] = "repo"
    merged["version"] = CONFIG_VERSION
    _validate_repo_config(merged, root=repo_root)
    repo_config = _build_repo_config(repo_root / CONFIG_FILENAME, merged)
    repo_config.effective_destination = _resolve_repo_effective_destination(
        hub, repo_root
    )
    return repo_config


def _resolve_repo_effective_destination(
    hub: HubConfig, repo_root: Path
) -> DestinationConfigSection:
    try:
        manifest = load_manifest(hub.manifest_path, hub.root)
    except ManifestError as exc:
        raise ConfigError(
            "Failed to resolve effective destination from hub manifest: "
            f"{hub.manifest_path}: {exc}"
        ) from exc
    except Exception as exc:  # intentional: defensive guard beyond known ManifestError
        raise ConfigError(
            "Failed to resolve effective destination from hub manifest: "
            f"{hub.manifest_path}: {exc}"
        ) from exc
    repo = manifest.get_by_path(hub.root, repo_root)
    if repo is None:
        return _parse_destination_config_section(default_local_destination())
    repos_by_id = {entry.id: entry for entry in manifest.repos}
    resolution = resolve_effective_repo_destination(repo, repos_by_id)
    return _parse_destination_config_section(resolution.to_dict())


def _resolve_repo_root(start: Path) -> Path:
    search_dir = start.resolve() if start.is_dir() else start.resolve().parent
    for current in [search_dir] + list(search_dir.parents):
        if (current / ".codex-autorunner" / "state.sqlite3").exists():
            return current
        if (current / ".git").exists():
            return current
    return search_dir


def load_repo_config(start: Path, hub_path: Optional[Path] = None) -> RepoConfig:
    """Load a repo config by deriving it from the nearest hub config."""
    repo_root = _resolve_repo_root(start)
    hub_config_path = _resolve_hub_path_for_repo(repo_root, hub_path)
    hub_config = load_hub_config_data(hub_config_path)
    _validate_hub_config(hub_config, root=hub_config_path.parent.parent.resolve())
    hub = _build_hub_config(hub_config_path, hub_config)
    return derive_repo_config(hub, repo_root)


def _build_repo_config(config_path: Path, cfg: Dict[str, Any]) -> RepoConfig:
    return _build_repo_config_impl(config_path, cfg)


def _build_hub_config(config_path: Path, cfg: Dict[str, Any]) -> HubConfig:
    return _build_hub_config_impl(config_path, cfg)
