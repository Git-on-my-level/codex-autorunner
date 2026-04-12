import dataclasses
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import yaml

from ..housekeeping import (
    HousekeepingConfig,
    HousekeepingRule,
    parse_housekeeping_config,
)
from ..manifest import ManifestError, load_manifest
from .agent_config import (
    AgentConfig,
    AgentProfileConfig,
    ResolvedAgentTarget,
    resolve_agent_target_from_agents,
)
from .agent_config import (
    parse_agents_config as _parse_agents_config,
)
from .config_contract import CONFIG_VERSION, ConfigError
from .config_env import (
    collect_env_overrides,
    load_dotenv_for_root,
    resolve_env_for_root,
)
from .config_layering import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    DEFAULT_REPO_CONFIG,
    PMA_DEFAULT_MAX_TEXT_CHARS,
    REPO_OVERRIDE_FILENAME,
    REPO_SHARED_KEYS,
    ROOT_CONFIG_FILENAME,
    ROOT_OVERRIDE_FILENAME,
    _default_housekeeping_section,
    _load_yaml_dict,
    derive_repo_config_data,
    find_nearest_hub_config_path,
    load_root_defaults,
    resolve_hub_config_data,
)
from .config_parsers import (
    _normalize_base_path,
    _parse_app_server_config,
    _parse_destination_config_section,
    _parse_log_config,
    _parse_notifications_config_section,
    _parse_opencode_config,
    _parse_pma_config,
    _parse_security_config_section,
    _parse_static_assets_config,
    _parse_templates_config,
    _parse_ticket_flow_config,
    _parse_update_backend,
    _parse_update_linux_service_names,
    _parse_usage_config,
    _parse_voice_config_section,
    parse_flow_retention_config,
)
from .config_types import (
    AppServerAutorunnerPromptConfig,
    AppServerConfig,
    DestinationConfigSection,
    FlowRetentionConfig,
    LogConfig,
    NotificationsConfigSection,
    OpenCodeConfig,
    PmaConfig,
    SecurityConfigSection,
    StaticAssetsConfig,
    TemplateRepoConfig,
    TemplatesConfig,
    TicketFlowConfig,
    UsageConfig,
    VoiceConfigSection,
)
from .destinations import default_local_destination, resolve_effective_repo_destination
from .generated_hub_config import normalize_generated_hub_config
from .path_utils import ConfigPathError
from .utils import atomic_write

logger = logging.getLogger("codex_autorunner.core.config")

# Re-export validation helpers for backward compatibility
from .config_validation import (  # noqa: E402,I001
    _is_loopback_host as _is_loopback_host_impl,
    _validate_hub_config,
    _validate_repo_config,
)

__all__ = [
    "AppServerAutorunnerPromptConfig",
    "ConfigError",
    "ConfigPathError",
    "FlowRetentionConfig",
    "PMA_DEFAULT_MAX_TEXT_CHARS",
    "REPO_OVERRIDE_FILENAME",
    "REPO_SHARED_KEYS",
    "ROOT_CONFIG_FILENAME",
    "TemplateRepoConfig",
    "collect_env_overrides",
    "find_nearest_hub_config_path",
    "load_root_defaults",
    "parse_flow_retention_config",
    "resolve_env_for_root",
]


def _is_loopback_host(host: str) -> bool:
    return _is_loopback_host_impl(host)


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


class AgentConfigMixin:
    agents: Dict[str, AgentConfig]

    def resolve_runtime_agent_target(
        self, agent_id: str, *, profile: Optional[str] = None
    ) -> ResolvedAgentTarget:
        return resolve_agent_target_from_agents(self.agents, agent_id, profile=profile)

    def resolved_agent_config(
        self, agent_id: str, *, profile: Optional[str] = None
    ) -> AgentConfig:
        resolved_target = self.resolve_runtime_agent_target(agent_id, profile=profile)
        agent = self.agents.get(resolved_target.runtime_agent_id)
        if agent is None:
            raise ConfigError(f"agents.{agent_id}.binary is required")
        normalized_profile = str(resolved_target.runtime_profile or "").strip().lower()
        if not normalized_profile:
            return agent
        profile_config = (agent.profiles or {}).get(normalized_profile)
        if profile_config is None:
            raise ConfigError(
                f"agents.{resolved_target.runtime_agent_id}.profiles.{normalized_profile} is not configured"
            )
        return AgentConfig(
            backend=profile_config.backend or agent.backend,
            binary=profile_config.binary or agent.binary,
            serve_command=(
                list(profile_config.serve_command)
                if profile_config.serve_command
                else (list(agent.serve_command) if agent.serve_command else None)
            ),
            base_url=profile_config.base_url or agent.base_url,
            subagent_models=(
                dict(profile_config.subagent_models)
                if profile_config.subagent_models
                else (
                    dict(agent.subagent_models)
                    if agent.subagent_models is not None
                    else None
                )
            ),
            default_profile=agent.default_profile,
            profiles=agent.profiles,
        )

    def agent_binary(self, agent_id: str, *, profile: Optional[str] = None) -> str:
        agent = self.resolved_agent_config(agent_id, profile=profile)
        if agent and agent.binary:
            return agent.binary
        raise ConfigError(f"agents.{agent_id}.binary is required")

    def agent_backend(self, agent_id: str, *, profile: Optional[str] = None) -> str:
        agent = self.resolved_agent_config(agent_id, profile=profile)
        backend = getattr(agent, "backend", None) if agent is not None else None
        if isinstance(backend, str) and backend.strip():
            return backend.strip().lower()
        return str(agent_id or "").strip().lower()

    def agent_serve_command(
        self, agent_id: str, *, profile: Optional[str] = None
    ) -> Optional[List[str]]:
        agent = self.resolved_agent_config(agent_id, profile=profile)
        if agent:
            return list(agent.serve_command) if agent.serve_command else None
        return None

    def agent_profiles(self, agent_id: str) -> Dict[str, AgentProfileConfig]:
        agent = self.agents.get(agent_id)
        if agent is None or not isinstance(agent.profiles, dict):
            return {}
        return dict(agent.profiles)

    def agent_default_profile(self, agent_id: str) -> Optional[str]:
        agent = self.agents.get(agent_id)
        if agent is None:
            return None
        value = str(agent.default_profile or "").strip().lower()
        return value or None


@dataclasses.dataclass
class RepoConfig(AgentConfigMixin):
    raw: Dict[str, Any]
    root: Path
    version: int
    mode: str
    security: SecurityConfigSection
    docs: Dict[str, Path]
    codex_binary: str
    codex_args: List[str]
    codex_terminal_args: List[str]
    codex_model: Optional[str]
    codex_reasoning: Optional[str]
    agents: Dict[str, AgentConfig]
    prompt_prev_run_max_chars: int
    prompt_template: Optional[Path]
    runner_sleep_seconds: int
    runner_stop_after_runs: Optional[int]
    runner_max_wallclock_seconds: Optional[int]
    runner_no_progress_threshold: int
    autorunner_reuse_session: bool
    ticket_flow: TicketFlowConfig
    git_auto_commit: bool
    git_commit_message_template: str
    update_skip_checks: bool
    update_backend: str
    update_linux_service_names: Dict[str, str]
    app_server: AppServerConfig
    opencode: OpenCodeConfig
    pma: PmaConfig
    usage: UsageConfig
    server_host: str
    server_port: int
    server_base_path: str
    server_access_log: bool
    server_auth_token_env: str
    server_allowed_hosts: List[str]
    server_allowed_origins: List[str]
    notifications: NotificationsConfigSection
    terminal_idle_timeout_seconds: Optional[int]
    log: LogConfig
    server_log: LogConfig
    voice: VoiceConfigSection
    static_assets: StaticAssetsConfig
    housekeeping: HousekeepingConfig
    flow_retention: FlowRetentionConfig
    durable_writes: bool
    templates: TemplatesConfig
    effective_destination: DestinationConfigSection = dataclasses.field(
        default_factory=lambda: _parse_destination_config_section(
            default_local_destination()
        )
    )

    def doc_path(self, key: str) -> Path:
        return self.root / self.docs[key]


@dataclasses.dataclass
class HubConfig(AgentConfigMixin):
    raw: Dict[str, Any]
    root: Path
    version: int
    mode: str
    repo_defaults: Dict[str, Any]
    agents: Dict[str, AgentConfig]
    templates: TemplatesConfig
    repos_root: Path
    worktrees_root: Path
    manifest_path: Path
    discover_depth: int
    auto_init_missing: bool
    include_root_repo: bool
    repo_server_inherit: bool
    update_repo_url: str
    update_repo_ref: str
    update_skip_checks: bool
    update_backend: str
    update_linux_service_names: Dict[str, str]
    app_server: AppServerConfig
    opencode: OpenCodeConfig
    pma: PmaConfig
    usage: UsageConfig
    server_host: str
    server_port: int
    server_base_path: str
    server_access_log: bool
    server_auth_token_env: str
    server_allowed_hosts: List[str]
    server_allowed_origins: List[str]
    log: LogConfig
    server_log: LogConfig
    static_assets: StaticAssetsConfig
    housekeeping: HousekeepingConfig
    durable_writes: bool


Config = RepoConfig


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
            f"Missing hub config file; expected to find {CONFIG_FILENAME} in {start} or parents (use --hub to specify, or run 'car init' to initialize)"
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
    root = config_path.parent.parent.resolve()
    docs = {
        "active_context": Path(cfg["docs"]["active_context"]),
        "decisions": Path(cfg["docs"]["decisions"]),
        "spec": Path(cfg["docs"]["spec"]),
    }
    voice_cfg = _parse_voice_config_section(cfg.get("voice"))
    template_val = cfg["prompt"].get("template")
    template = root / template_val if template_val else None
    term_args = cfg["codex"].get("terminal_args") or []
    terminal_cfg = cfg.get("terminal") if isinstance(cfg.get("terminal"), dict) else {}
    terminal_cfg = cast(Dict[str, Any], terminal_cfg)
    idle_timeout_value = terminal_cfg.get("idle_timeout_seconds")
    idle_timeout_seconds: Optional[int]
    if idle_timeout_value is None:
        idle_timeout_seconds = None
    else:
        idle_timeout_seconds = int(idle_timeout_value)
        if idle_timeout_seconds <= 0:
            idle_timeout_seconds = None
    notifications_cfg = _parse_notifications_config_section(cfg.get("notifications"))
    security_cfg = _parse_security_config_section(cfg.get("security"))
    update_cfg = cfg.get("update")
    update_cfg = cast(
        Dict[str, Any], update_cfg if isinstance(update_cfg, dict) else {}
    )
    update_skip_checks = bool(update_cfg.get("skip_checks", False))
    update_backend = _parse_update_backend(update_cfg)
    update_linux_service_names = _parse_update_linux_service_names(update_cfg)
    autorunner_cfg = cfg.get("autorunner")
    autorunner_cfg = cast(
        Dict[str, Any], autorunner_cfg if isinstance(autorunner_cfg, dict) else {}
    )
    reuse_session_value = autorunner_cfg.get("reuse_session")
    autorunner_reuse_session = (
        bool(reuse_session_value) if reuse_session_value is not None else False
    )
    storage_cfg = cfg.get("storage")
    storage_cfg = cast(
        Dict[str, Any], storage_cfg if isinstance(storage_cfg, dict) else {}
    )
    durable_writes = bool(storage_cfg.get("durable_writes", False))
    return RepoConfig(
        raw=cfg,
        root=root,
        version=int(cfg["version"]),
        mode="repo",
        docs=docs,
        codex_binary=cfg["codex"]["binary"],
        codex_args=list(cfg["codex"].get("args", [])),
        codex_terminal_args=list(term_args) if isinstance(term_args, list) else [],
        codex_model=cfg["codex"].get("model"),
        codex_reasoning=cfg["codex"].get("reasoning"),
        agents=_parse_agents_config(cfg, DEFAULT_REPO_CONFIG),
        prompt_prev_run_max_chars=int(cfg["prompt"]["prev_run_max_chars"]),
        prompt_template=template,
        runner_sleep_seconds=int(cfg["runner"]["sleep_seconds"]),
        runner_stop_after_runs=cfg["runner"].get("stop_after_runs"),
        runner_max_wallclock_seconds=cfg["runner"].get("max_wallclock_seconds"),
        runner_no_progress_threshold=int(cfg["runner"].get("no_progress_threshold", 3)),
        autorunner_reuse_session=autorunner_reuse_session,
        git_auto_commit=bool(cfg["git"].get("auto_commit", False)),
        git_commit_message_template=str(cfg["git"].get("commit_message_template")),
        update_skip_checks=update_skip_checks,
        update_backend=update_backend,
        update_linux_service_names=update_linux_service_names,
        ticket_flow=_parse_ticket_flow_config(
            cfg.get("ticket_flow"),
            cast(Dict[str, Any], DEFAULT_REPO_CONFIG.get("ticket_flow")),
        ),
        app_server=_parse_app_server_config(
            cfg.get("app_server"),
            root,
            DEFAULT_REPO_CONFIG["app_server"],
        ),
        opencode=_parse_opencode_config(
            cfg.get("opencode"), root, DEFAULT_REPO_CONFIG.get("opencode")
        ),
        pma=_parse_pma_config(cfg.get("pma"), root, DEFAULT_HUB_CONFIG.get("pma")),
        usage=_parse_usage_config(
            cfg.get("usage"), root, DEFAULT_REPO_CONFIG.get("usage")
        ),
        security=security_cfg,
        server_host=str(cfg["server"].get("host")),
        server_port=int(cfg["server"].get("port")),
        server_base_path=_normalize_base_path(cfg["server"].get("base_path", "")),
        server_access_log=bool(cfg["server"].get("access_log", False)),
        server_auth_token_env=str(cfg["server"].get("auth_token_env", "")),
        server_allowed_hosts=list(cfg["server"].get("allowed_hosts") or []),
        server_allowed_origins=list(cfg["server"].get("allowed_origins") or []),
        notifications=notifications_cfg,
        terminal_idle_timeout_seconds=idle_timeout_seconds,
        log=_parse_log_config(cfg.get("log"), root, DEFAULT_REPO_CONFIG["log"]),
        server_log=_parse_log_config(
            cfg.get("server_log"), root, DEFAULT_REPO_CONFIG["server_log"]
        ),
        voice=voice_cfg,
        static_assets=_parse_static_assets_config(
            cfg.get("static_assets"), root, DEFAULT_REPO_CONFIG["static_assets"]
        ),
        housekeeping=parse_housekeeping_config(cfg.get("housekeeping")),
        flow_retention=parse_flow_retention_config(cfg.get("flow_retention")),
        durable_writes=durable_writes,
        templates=_parse_templates_config(
            cfg.get("templates"), DEFAULT_HUB_CONFIG.get("templates")
        ),
    )


def _build_hub_config(config_path: Path, cfg: Dict[str, Any]) -> HubConfig:
    root = config_path.parent.parent.resolve()
    hub_cfg = cfg["hub"]
    log_cfg_raw = hub_cfg["log"]
    server_log_cfg_raw = cfg.get("server_log")
    if not isinstance(server_log_cfg_raw, dict):
        server_log_cfg_raw = {
            "path": log_cfg_raw["path"],
            "max_bytes": log_cfg_raw["max_bytes"],
            "backup_count": log_cfg_raw["backup_count"],
        }

    log = _parse_log_config(log_cfg_raw, root, log_cfg_raw, scope="log.path")
    server_log = _parse_log_config(
        server_log_cfg_raw,
        root,
        server_log_cfg_raw,
        scope="server_log.path",
    )

    update_cfg = cfg.get("update")
    update_cfg = cast(
        Dict[str, Any], update_cfg if isinstance(update_cfg, dict) else {}
    )
    update_skip_checks = bool(update_cfg.get("skip_checks", False))
    update_backend = _parse_update_backend(update_cfg)
    update_linux_service_names = _parse_update_linux_service_names(update_cfg)
    storage_cfg = cfg.get("storage")
    storage_cfg = cast(
        Dict[str, Any], storage_cfg if isinstance(storage_cfg, dict) else {}
    )
    durable_writes = bool(storage_cfg.get("durable_writes", False))

    return HubConfig(
        raw=cfg,
        root=root,
        version=int(cfg["version"]),
        mode="hub",
        repo_defaults=cast(Dict[str, Any], cfg.get("repo_defaults") or {}),
        agents=_parse_agents_config(cfg, DEFAULT_HUB_CONFIG),
        templates=_parse_templates_config(
            cfg.get("templates"), DEFAULT_HUB_CONFIG.get("templates")
        ),
        repos_root=(root / hub_cfg["repos_root"]).resolve(),
        worktrees_root=(root / hub_cfg["worktrees_root"]).resolve(),
        manifest_path=root / hub_cfg["manifest"],
        discover_depth=int(hub_cfg["discover_depth"]),
        auto_init_missing=bool(hub_cfg["auto_init_missing"]),
        include_root_repo=bool(hub_cfg.get("include_root_repo", False)),
        repo_server_inherit=bool(hub_cfg.get("repo_server_inherit", True)),
        update_repo_url=str(hub_cfg.get("update_repo_url", "")),
        update_repo_ref=str(hub_cfg.get("update_repo_ref", "main")),
        update_skip_checks=update_skip_checks,
        update_backend=update_backend,
        update_linux_service_names=update_linux_service_names,
        durable_writes=durable_writes,
        app_server=_parse_app_server_config(
            cfg.get("app_server"),
            root,
            DEFAULT_HUB_CONFIG["app_server"],
        ),
        opencode=_parse_opencode_config(
            cfg.get("opencode"), root, DEFAULT_HUB_CONFIG.get("opencode")
        ),
        pma=_parse_pma_config(cfg.get("pma"), root, DEFAULT_HUB_CONFIG.get("pma")),
        usage=_parse_usage_config(
            cfg.get("usage"), root, DEFAULT_HUB_CONFIG.get("usage")
        ),
        server_host=str(cfg["server"]["host"]),
        server_port=int(cfg["server"]["port"]),
        server_base_path=_normalize_base_path(cfg["server"].get("base_path", "")),
        server_access_log=bool(cfg["server"].get("access_log", False)),
        server_auth_token_env=str(cfg["server"].get("auth_token_env", "")),
        server_allowed_hosts=list(cfg["server"].get("allowed_hosts") or []),
        server_allowed_origins=list(cfg["server"].get("allowed_origins") or []),
        log=log,
        server_log=server_log,
        static_assets=_parse_static_assets_config(
            cfg.get("static_assets"), root, DEFAULT_HUB_CONFIG["static_assets"]
        ),
        housekeeping=parse_housekeeping_config(cfg.get("housekeeping")),
    )
