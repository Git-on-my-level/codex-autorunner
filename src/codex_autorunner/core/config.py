import dataclasses
import logging
import os
from os import PathLike
from pathlib import Path
from typing import (
    IO,
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    TypedDict,
    Union,
    cast,
)

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
from .app_server_command import (
    GLOBAL_APP_SERVER_COMMAND_ENV,
    LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV,
    resolve_app_server_command,
)
from .config_contract import CONFIG_VERSION, ConfigError
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
    _default_update_linux_service_names,
    _load_yaml_dict,
    derive_repo_config_data,
    find_nearest_hub_config_path,
    load_root_defaults,
    resolve_hub_config_data,
)
from .destinations import default_local_destination, resolve_effective_repo_destination
from .generated_hub_config import normalize_generated_hub_config
from .path_utils import ConfigPathError, resolve_config_path
from .report_retention import (
    DEFAULT_REPORT_MAX_HISTORY_FILES,
    DEFAULT_REPORT_MAX_TOTAL_BYTES,
)
from .utils import atomic_write

logger = logging.getLogger("codex_autorunner.core.config")

_DEFAULT_FLOW_RETENTION_DAYS = 7
_DEFAULT_FLOW_SWEEP_INTERVAL_SECONDS = 24 * 60 * 60


@dataclasses.dataclass(frozen=True)
class FlowRetentionConfig:
    retention_days: int = _DEFAULT_FLOW_RETENTION_DAYS
    sweep_interval_seconds: int = _DEFAULT_FLOW_SWEEP_INTERVAL_SECONDS


def parse_flow_retention_config(raw: Optional[Dict[str, Any]]) -> FlowRetentionConfig:
    if not isinstance(raw, dict):
        return FlowRetentionConfig()
    retention_days = raw.get("retention_days")
    sweep_interval_seconds = raw.get("sweep_interval_seconds")
    return FlowRetentionConfig(
        retention_days=(
            int(retention_days)
            if retention_days is not None
            else _DEFAULT_FLOW_RETENTION_DAYS
        ),
        sweep_interval_seconds=(
            int(sweep_interval_seconds)
            if sweep_interval_seconds is not None
            else _DEFAULT_FLOW_SWEEP_INTERVAL_SECONDS
        ),
    )


DOTENV_AVAILABLE = True
try:
    from dotenv import dotenv_values, load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    DOTENV_AVAILABLE = False

    def load_dotenv(
        dotenv_path: Optional[Union[str, PathLike[str]]] = None,
        stream: Optional[IO[str]] = None,
        verbose: bool = False,
        override: bool = False,
        interpolate: bool = True,
        encoding: Optional[str] = None,
    ) -> bool:
        return False

    def dotenv_values(
        dotenv_path: Optional[Union[str, PathLike[str]]] = None,
        stream: Optional[IO[str]] = None,
        verbose: bool = False,
        interpolate: bool = True,
        encoding: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        return {}


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


def _parse_update_backend(update_cfg: Mapping[str, Any]) -> str:
    raw = update_cfg.get("backend")
    if raw is None:
        return "auto"
    value = str(raw).strip().lower()
    return value or "auto"


def _parse_update_linux_service_names(update_cfg: Mapping[str, Any]) -> Dict[str, str]:
    merged = dict(_default_update_linux_service_names())
    raw = update_cfg.get("linux_service_names")
    if not isinstance(raw, dict):
        return merged
    for key in ("hub", "telegram", "discord"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = value.strip()
    return merged


# Import validation helpers after approval-mode constants are defined.
from .config_validation import (  # noqa: E402,I001
    _is_loopback_host as _is_loopback_host_impl,
    _normalize_ticket_flow_approval_mode,
    _validate_hub_config,
    _validate_repo_config,
)

__all__ = [
    "ConfigError",
    "ConfigPathError",
    "REPO_OVERRIDE_FILENAME",
    "REPO_SHARED_KEYS",
    "ROOT_CONFIG_FILENAME",
    "find_nearest_hub_config_path",
    "load_root_defaults",
]


def _is_loopback_host(host: str) -> bool:
    return _is_loopback_host_impl(host)


@dataclasses.dataclass
class LogConfig:
    path: Path
    max_bytes: int
    backup_count: int


@dataclasses.dataclass
class StaticAssetsConfig:
    cache_root: Path
    max_cache_entries: int
    max_cache_age_days: Optional[int]


@dataclasses.dataclass
class AppServerDocChatPromptConfig:
    max_chars: int
    message_max_chars: int
    target_excerpt_max_chars: int
    recent_summary_max_chars: int


@dataclasses.dataclass
class AppServerSpecIngestPromptConfig:
    max_chars: int
    message_max_chars: int
    spec_excerpt_max_chars: int


@dataclasses.dataclass
class AppServerAutorunnerPromptConfig:
    max_chars: int
    message_max_chars: int
    todo_excerpt_max_chars: int
    prev_run_max_chars: int


@dataclasses.dataclass
class AppServerPromptsConfig:
    doc_chat: AppServerDocChatPromptConfig
    spec_ingest: AppServerSpecIngestPromptConfig
    autorunner: AppServerAutorunnerPromptConfig


@dataclasses.dataclass
class AppServerClientConfig:
    max_message_bytes: int
    oversize_preview_bytes: int
    max_oversize_drain_bytes: int
    restart_backoff_initial_seconds: float
    restart_backoff_max_seconds: float
    restart_backoff_jitter_ratio: float


@dataclasses.dataclass
class AppServerOutputConfig:
    policy: str


@dataclasses.dataclass
class AppServerConfig:
    command: List[str]
    state_root: Path
    auto_restart: Optional[bool]
    max_handles: Optional[int]
    idle_ttl_seconds: Optional[int]
    turn_timeout_seconds: Optional[float]
    turn_stall_timeout_seconds: Optional[float]
    turn_stall_poll_interval_seconds: Optional[float]
    turn_stall_recovery_min_interval_seconds: Optional[float]
    turn_stall_max_recovery_attempts: Optional[int]
    request_timeout: Optional[float]
    client: AppServerClientConfig
    output: AppServerOutputConfig
    prompts: AppServerPromptsConfig


@dataclasses.dataclass
class OpenCodeConfig:
    server_scope: str
    session_stall_timeout_seconds: Optional[float]
    max_text_chars: Optional[int]
    max_handles: Optional[int]
    idle_ttl_seconds: Optional[int]


@dataclasses.dataclass
class PmaConfig:
    enabled: bool
    default_agent: str
    profile: Optional[str]
    model: Optional[str]
    reasoning: Optional[str]
    managed_thread_terminal_followup_default: bool
    max_upload_bytes: int
    max_repos: int
    max_messages: int
    max_text_chars: int
    # Hub-level PMA durable context docs
    docs_max_chars: int = 12_000
    active_context_max_lines: int = 200
    context_log_tail_lines: int = 120
    freshness_stale_threshold_seconds: int = 1800
    dispatch_interception_enabled: bool = False
    reactive_enabled: bool = True
    reactive_event_types: List[str] = dataclasses.field(default_factory=list)
    reactive_debounce_seconds: int = 300
    reactive_origin_blocklist: List[str] = dataclasses.field(default_factory=list)
    filebox_inbox_max_age_days: int = 7
    filebox_outbox_max_age_days: int = 7
    report_max_history_files: int = DEFAULT_REPORT_MAX_HISTORY_FILES
    report_max_total_bytes: int = DEFAULT_REPORT_MAX_TOTAL_BYTES
    app_server_workspace_max_age_days: int = 7
    inbox_auto_dismiss_grace_seconds: int = 3600
    # Worktree cleanup policies
    cleanup_require_archive: bool = True
    cleanup_auto_delete_orphans: bool = False
    worktree_archive_profile: str = "portable"
    worktree_archive_max_snapshots_per_repo: int = 10
    worktree_archive_max_age_days: int = 30
    worktree_archive_max_total_bytes: int = 1_000_000_000
    run_archive_max_entries: int = 200
    run_archive_max_age_days: int = 30
    run_archive_max_total_bytes: int = 1_000_000_000
    orchestration_compaction_max_hot_rows: int = 16
    orchestration_hot_history_retention_days: int = 30
    orchestration_cold_trace_retention_days: int = 90


@dataclasses.dataclass
class UsageConfig:
    cache_scope: str
    global_cache_root: Path
    repo_cache_path: Path


@dataclasses.dataclass(frozen=True)
class TemplateRepoConfig:
    id: str
    url: str
    trusted: bool
    default_ref: str


@dataclasses.dataclass(frozen=True)
class TemplatesConfig:
    enabled: bool
    repos: List[TemplateRepoConfig]


@dataclasses.dataclass(frozen=True)
class TicketFlowConfig:
    approval_mode: str
    default_approval_decision: str
    include_previous_ticket_context: bool
    auto_resume: bool = False


class SecurityConfigSection(TypedDict, total=False):
    redact_run_logs: bool
    redact_patterns: List[str]


class NotificationTargetSection(TypedDict, total=False):
    enabled: bool
    webhook_url_env: str
    bot_token_env: str
    chat_id_env: str


class NotificationsConfigSection(TypedDict, total=False):
    enabled: Union[bool, Literal["auto"]]
    events: List[str]
    tui_idle_seconds: int
    timeout_seconds: float
    discord: NotificationTargetSection
    telegram: NotificationTargetSection


class VoiceConfigSection(TypedDict, total=False):
    enabled: bool
    provider: str
    latency_mode: str
    chunk_ms: int
    sample_rate: int
    warn_on_remote_api: bool
    push_to_talk: dict[str, object]
    providers: dict[str, dict[str, object]]


class DestinationConfigSection(TypedDict, total=False):
    kind: str
    image: str
    container_name: str
    mounts: List[dict[str, object]]
    env_passthrough: List[str]
    workdir: str
    profile: str
    env: dict[str, str]


def _parse_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


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


# Alias used by existing code paths that only support repo mode
Config = RepoConfig


def _parse_security_config_section(raw: object) -> SecurityConfigSection:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, object] = dict(raw)
    redact_run_logs = raw.get("redact_run_logs")
    if isinstance(redact_run_logs, bool):
        normalized["redact_run_logs"] = redact_run_logs
    else:
        normalized.pop("redact_run_logs", None)

    redact_patterns = raw.get("redact_patterns")
    if isinstance(redact_patterns, list):
        normalized["redact_patterns"] = [
            value.strip()
            for value in redact_patterns
            if isinstance(value, str) and value.strip()
        ]
    else:
        normalized.pop("redact_patterns", None)
    return cast(SecurityConfigSection, normalized)


def _parse_notification_target_section(raw: object) -> NotificationTargetSection:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, object] = dict(raw)
    enabled = raw.get("enabled")
    if isinstance(enabled, bool):
        normalized["enabled"] = enabled
    else:
        normalized.pop("enabled", None)

    for key in ("webhook_url_env", "bot_token_env", "chat_id_env"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
        else:
            normalized.pop(key, None)
    return cast(NotificationTargetSection, normalized)


def _parse_notifications_config_section(raw: object) -> NotificationsConfigSection:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, object] = dict(raw)

    enabled = raw.get("enabled")
    if isinstance(enabled, bool):
        normalized["enabled"] = enabled
    elif isinstance(enabled, str) and enabled.strip().lower() == "auto":
        normalized["enabled"] = "auto"
    else:
        normalized.pop("enabled", None)

    events = raw.get("events")
    if isinstance(events, list):
        normalized["events"] = [
            value.strip()
            for value in events
            if isinstance(value, str) and value.strip()
        ]
    else:
        normalized.pop("events", None)

    tui_idle_seconds = raw.get("tui_idle_seconds")
    if isinstance(tui_idle_seconds, (int, float)) and int(tui_idle_seconds) > 0:
        normalized["tui_idle_seconds"] = int(tui_idle_seconds)
    else:
        normalized.pop("tui_idle_seconds", None)

    timeout_seconds = raw.get("timeout_seconds")
    if isinstance(timeout_seconds, (int, float)) and float(timeout_seconds) > 0:
        normalized["timeout_seconds"] = float(timeout_seconds)
    else:
        normalized.pop("timeout_seconds", None)

    discord = _parse_notification_target_section(raw.get("discord"))
    if discord:
        normalized["discord"] = discord
    else:
        normalized.pop("discord", None)

    telegram = _parse_notification_target_section(raw.get("telegram"))
    if telegram:
        normalized["telegram"] = telegram
    else:
        normalized.pop("telegram", None)
    return cast(NotificationsConfigSection, normalized)


def _parse_voice_config_section(raw: object) -> VoiceConfigSection:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, object] = dict(raw)
    bool_keys = ("enabled", "warn_on_remote_api")
    for key in bool_keys:
        value = raw.get(key)
        if isinstance(value, bool):
            normalized[key] = value
        else:
            normalized.pop(key, None)

    for key in ("provider", "latency_mode"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
        else:
            normalized.pop(key, None)

    for key in ("chunk_ms", "sample_rate"):
        value = raw.get(key)
        if isinstance(value, (int, float)):
            normalized[key] = int(value)
        else:
            normalized.pop(key, None)

    push_to_talk = raw.get("push_to_talk")
    if isinstance(push_to_talk, dict):
        normalized["push_to_talk"] = {
            str(key): value
            for key, value in push_to_talk.items()
            if isinstance(key, str)
        }
    else:
        normalized.pop("push_to_talk", None)

    providers_raw = raw.get("providers")
    if isinstance(providers_raw, dict):
        providers: dict[str, dict[str, object]] = {}
        for provider_name, provider_cfg in providers_raw.items():
            if not isinstance(provider_name, str) or not provider_name.strip():
                continue
            if not isinstance(provider_cfg, dict):
                continue
            providers[provider_name.strip()] = {
                str(key): value
                for key, value in provider_cfg.items()
                if isinstance(key, str)
            }
        normalized["providers"] = providers
    else:
        normalized.pop("providers", None)
    return cast(VoiceConfigSection, normalized)


def _parse_destination_config_section(raw: object) -> DestinationConfigSection:
    if not isinstance(raw, dict):
        return cast(DestinationConfigSection, default_local_destination())
    return cast(DestinationConfigSection, dict(raw))


def _parse_ticket_flow_config(
    cfg: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, Any]],
) -> TicketFlowConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    approval_mode = _normalize_ticket_flow_approval_mode(
        cfg.get("approval_mode", defaults.get("approval_mode", "yolo")),
        scope="ticket_flow.approval_mode",
    )
    default_approval_decision = cfg.get(
        "default_approval_decision", defaults.get("default_approval_decision", "accept")
    )
    if not isinstance(default_approval_decision, str):
        raise ConfigError("ticket_flow.default_approval_decision must be a string")
    include_previous_ticket_context = cfg.get(
        "include_previous_ticket_context",
        defaults.get("include_previous_ticket_context", False),
    )
    if not isinstance(include_previous_ticket_context, bool):
        raise ConfigError("ticket_flow.include_previous_ticket_context must be boolean")
    auto_resume = cfg.get("auto_resume", defaults.get("auto_resume", False))
    if not isinstance(auto_resume, bool):
        raise ConfigError("ticket_flow.auto_resume must be boolean")
    return TicketFlowConfig(
        approval_mode=approval_mode,
        default_approval_decision=default_approval_decision,
        include_previous_ticket_context=include_previous_ticket_context,
        auto_resume=auto_resume,
    )


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


def _normalize_base_path(path: Optional[str]) -> str:
    """Normalize base path to either '' or a single-leading-slash path without trailing slash."""
    if not path:
        return ""
    normalized = str(path).strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = normalized.rstrip("/")
    return normalized or ""


def _parse_prompt_int(cfg: Dict[str, Any], defaults: Dict[str, Any], key: str) -> int:
    raw = cfg.get(key)
    if raw is None:
        raw = defaults.get(key, 0)
    return int(raw)


def _parse_app_server_prompts_config(
    cfg: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, Any]],
) -> AppServerPromptsConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    doc_chat_cfg = cfg.get("doc_chat")
    doc_chat_defaults = defaults.get("doc_chat")
    doc_chat_cfg = doc_chat_cfg if isinstance(doc_chat_cfg, dict) else {}
    doc_chat_defaults = doc_chat_defaults if isinstance(doc_chat_defaults, dict) else {}
    spec_ingest_cfg = cfg.get("spec_ingest")
    spec_ingest_defaults = defaults.get("spec_ingest")
    spec_ingest_cfg = spec_ingest_cfg if isinstance(spec_ingest_cfg, dict) else {}
    spec_ingest_defaults = (
        spec_ingest_defaults if isinstance(spec_ingest_defaults, dict) else {}
    )
    autorunner_cfg = cfg.get("autorunner")
    autorunner_defaults = defaults.get("autorunner")
    autorunner_cfg = autorunner_cfg if isinstance(autorunner_cfg, dict) else {}
    autorunner_defaults = (
        autorunner_defaults if isinstance(autorunner_defaults, dict) else {}
    )
    return AppServerPromptsConfig(
        doc_chat=AppServerDocChatPromptConfig(
            max_chars=_parse_prompt_int(doc_chat_cfg, doc_chat_defaults, "max_chars"),
            message_max_chars=_parse_prompt_int(
                doc_chat_cfg, doc_chat_defaults, "message_max_chars"
            ),
            target_excerpt_max_chars=_parse_prompt_int(
                doc_chat_cfg, doc_chat_defaults, "target_excerpt_max_chars"
            ),
            recent_summary_max_chars=_parse_prompt_int(
                doc_chat_cfg, doc_chat_defaults, "recent_summary_max_chars"
            ),
        ),
        spec_ingest=AppServerSpecIngestPromptConfig(
            max_chars=_parse_prompt_int(
                spec_ingest_cfg, spec_ingest_defaults, "max_chars"
            ),
            message_max_chars=_parse_prompt_int(
                spec_ingest_cfg, spec_ingest_defaults, "message_max_chars"
            ),
            spec_excerpt_max_chars=_parse_prompt_int(
                spec_ingest_cfg, spec_ingest_defaults, "spec_excerpt_max_chars"
            ),
        ),
        autorunner=AppServerAutorunnerPromptConfig(
            max_chars=_parse_prompt_int(
                autorunner_cfg, autorunner_defaults, "max_chars"
            ),
            message_max_chars=_parse_prompt_int(
                autorunner_cfg, autorunner_defaults, "message_max_chars"
            ),
            todo_excerpt_max_chars=_parse_prompt_int(
                autorunner_cfg, autorunner_defaults, "todo_excerpt_max_chars"
            ),
            prev_run_max_chars=_parse_prompt_int(
                autorunner_cfg, autorunner_defaults, "prev_run_max_chars"
            ),
        ),
    )


_APP_SERVER_OUTPUT_POLICIES = {"final_only", "all_agent_messages"}


def _parse_app_server_output_config(
    cfg: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, Any]],
) -> AppServerOutputConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    policy_raw = cfg.get("policy", defaults.get("policy"))
    policy = str(policy_raw).strip().lower() if policy_raw is not None else ""
    if policy not in _APP_SERVER_OUTPUT_POLICIES:
        policy = str(defaults.get("policy") or "final_only").strip().lower()
    if policy not in _APP_SERVER_OUTPUT_POLICIES:
        policy = "final_only"
    return AppServerOutputConfig(policy=policy)


def _parse_app_server_config(
    cfg: Optional[Dict[str, Any]],
    root: Path,
    defaults: Dict[str, Any],
) -> AppServerConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    raw_command = cfg.get("command", dataclasses.MISSING)
    if raw_command is dataclasses.MISSING:
        command = resolve_app_server_command(
            defaults.get("command"),
            env=os.environ,
        )
    else:
        command = resolve_app_server_command(
            raw_command,
            env=os.environ,
            fallback=(),
        )
    state_root_raw = cfg.get("state_root", defaults.get("state_root"))
    if state_root_raw is None:
        raise ConfigError("app_server.state_root is required")
    state_root = resolve_config_path(
        state_root_raw,
        root,
        allow_home=True,
        scope="app_server.state_root",
    )
    auto_restart_raw = cfg.get("auto_restart", defaults.get("auto_restart"))
    if auto_restart_raw is None:
        auto_restart = None
    else:
        auto_restart = bool(auto_restart_raw)
    max_handles_raw = cfg.get("max_handles", defaults.get("max_handles"))
    max_handles = _parse_optional_int(max_handles_raw)
    idle_ttl_raw = cfg.get("idle_ttl_seconds", defaults.get("idle_ttl_seconds"))
    idle_ttl_seconds = _parse_optional_int(idle_ttl_raw)
    turn_timeout_raw = cfg.get(
        "turn_timeout_seconds", defaults.get("turn_timeout_seconds")
    )
    turn_timeout_seconds = (
        float(turn_timeout_raw) if turn_timeout_raw is not None else None
    )
    stall_timeout_raw = cfg.get(
        "turn_stall_timeout_seconds", defaults.get("turn_stall_timeout_seconds")
    )
    turn_stall_timeout_seconds = (
        float(stall_timeout_raw) if stall_timeout_raw is not None else None
    )
    stall_poll_raw = cfg.get(
        "turn_stall_poll_interval_seconds",
        defaults.get("turn_stall_poll_interval_seconds"),
    )
    turn_stall_poll_interval_seconds = (
        float(stall_poll_raw) if stall_poll_raw is not None else None
    )
    stall_recovery_raw = cfg.get(
        "turn_stall_recovery_min_interval_seconds",
        defaults.get("turn_stall_recovery_min_interval_seconds"),
    )
    turn_stall_recovery_min_interval_seconds = (
        float(stall_recovery_raw) if stall_recovery_raw is not None else None
    )
    stall_max_attempts_raw = cfg.get(
        "turn_stall_max_recovery_attempts",
        defaults.get("turn_stall_max_recovery_attempts"),
    )
    turn_stall_max_recovery_attempts = _parse_optional_int(stall_max_attempts_raw)
    request_timeout_raw = cfg.get("request_timeout", defaults.get("request_timeout"))
    request_timeout = (
        float(request_timeout_raw) if request_timeout_raw is not None else None
    )
    client_defaults = defaults.get("client")
    client_defaults = client_defaults if isinstance(client_defaults, dict) else {}
    client_cfg_raw = cfg.get("client")
    client_cfg = client_cfg_raw if isinstance(client_cfg_raw, dict) else {}

    def _client_int(key: str) -> int:
        value = client_cfg.get(key, client_defaults.get(key))
        return int(value) if value is not None else int(client_defaults.get(key) or 0)

    def _client_float(key: str) -> float:
        value = client_cfg.get(key, client_defaults.get(key))
        return (
            float(value)
            if value is not None
            else float(client_defaults.get(key) or 0.0)
        )

    output_defaults = defaults.get("output")
    output_cfg_raw = cfg.get("output")
    output = _parse_app_server_output_config(output_cfg_raw, output_defaults)
    prompt_defaults = defaults.get("prompts")
    prompts = _parse_app_server_prompts_config(cfg.get("prompts"), prompt_defaults)
    return AppServerConfig(
        command=command,
        state_root=state_root,
        auto_restart=auto_restart,
        max_handles=max_handles,
        idle_ttl_seconds=idle_ttl_seconds,
        turn_timeout_seconds=turn_timeout_seconds,
        turn_stall_timeout_seconds=turn_stall_timeout_seconds,
        turn_stall_poll_interval_seconds=turn_stall_poll_interval_seconds,
        turn_stall_recovery_min_interval_seconds=turn_stall_recovery_min_interval_seconds,
        turn_stall_max_recovery_attempts=turn_stall_max_recovery_attempts,
        request_timeout=request_timeout,
        client=AppServerClientConfig(
            max_message_bytes=_client_int("max_message_bytes"),
            oversize_preview_bytes=_client_int("oversize_preview_bytes"),
            max_oversize_drain_bytes=_client_int("max_oversize_drain_bytes"),
            restart_backoff_initial_seconds=_client_float(
                "restart_backoff_initial_seconds"
            ),
            restart_backoff_max_seconds=_client_float("restart_backoff_max_seconds"),
            restart_backoff_jitter_ratio=_client_float("restart_backoff_jitter_ratio"),
        ),
        output=output,
        prompts=prompts,
    )


def _parse_opencode_config(
    cfg: Optional[Dict[str, Any]],
    _root: Path,
    defaults: Optional[Dict[str, Any]],
) -> OpenCodeConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    server_scope_raw = cfg.get(
        "server_scope", defaults.get("server_scope", "workspace")
    )
    server_scope = str(server_scope_raw).strip().lower() or "workspace"
    stall_timeout_raw = cfg.get(
        "session_stall_timeout_seconds",
        defaults.get("session_stall_timeout_seconds"),
    )
    stall_timeout_seconds = (
        float(stall_timeout_raw) if stall_timeout_raw is not None else None
    )
    max_text_chars_raw = cfg.get("max_text_chars", defaults.get("max_text_chars"))
    max_text_chars = int(max_text_chars_raw) if max_text_chars_raw is not None else None
    max_handles_raw = cfg.get("max_handles", defaults.get("max_handles"))
    max_handles = _parse_optional_int(max_handles_raw)
    idle_ttl_raw = cfg.get("idle_ttl_seconds", defaults.get("idle_ttl_seconds"))
    idle_ttl_seconds = _parse_optional_int(idle_ttl_raw)
    return OpenCodeConfig(
        server_scope=server_scope,
        session_stall_timeout_seconds=stall_timeout_seconds,
        max_text_chars=max_text_chars,
        max_handles=max_handles,
        idle_ttl_seconds=idle_ttl_seconds,
    )


def _parse_pma_config(
    cfg: Optional[Dict[str, Any]],
    _root: Path,
    defaults: Optional[Dict[str, Any]],
) -> PmaConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    enabled = bool(cfg.get("enabled", defaults.get("enabled", True)))
    default_agent = str(
        cfg.get("default_agent", defaults.get("default_agent", "codex"))
    )
    profile_raw = cfg.get("profile", defaults.get("profile"))
    profile = str(profile_raw).strip().lower() or None if profile_raw else None
    model_raw = cfg.get("model", defaults.get("model"))
    model = str(model_raw).strip() or None if model_raw else None
    reasoning_raw = cfg.get("reasoning", defaults.get("reasoning"))
    reasoning = str(reasoning_raw).strip() or None if reasoning_raw else None
    managed_thread_terminal_followup_default = bool(
        cfg.get(
            "managed_thread_terminal_followup_default",
            defaults.get("managed_thread_terminal_followup_default", True),
        )
    )
    max_upload_bytes_raw = cfg.get(
        "max_upload_bytes", defaults.get("max_upload_bytes", 10_000_000)
    )
    try:
        max_upload_bytes = int(max_upload_bytes_raw)
    except (ValueError, TypeError):
        max_upload_bytes = 10_000_000
    if max_upload_bytes <= 0:
        max_upload_bytes = 10_000_000

    def _parse_positive_int(key: str, fallback: int) -> int:
        raw = cfg.get(key, defaults.get(key, fallback))
        try:
            value = int(raw)
        except (ValueError, TypeError):
            return fallback
        return value if value > 0 else fallback

    max_repos = _parse_positive_int("max_repos", 25)
    max_messages = _parse_positive_int("max_messages", 10)
    max_text_chars = _parse_positive_int("max_text_chars", PMA_DEFAULT_MAX_TEXT_CHARS)
    docs_max_chars = _parse_positive_int("docs_max_chars", 12_000)
    active_context_max_lines = _parse_positive_int("active_context_max_lines", 200)
    context_log_tail_lines = _parse_positive_int("context_log_tail_lines", 120)
    freshness_stale_threshold_seconds = _parse_positive_int(
        "freshness_stale_threshold_seconds", 1800
    )
    dispatch_interception_enabled = bool(
        cfg.get(
            "dispatch_interception_enabled",
            defaults.get("dispatch_interception_enabled", False),
        )
    )
    reactive_enabled = bool(
        cfg.get("reactive_enabled", defaults.get("reactive_enabled", True))
    )
    reactive_event_types_raw = cfg.get(
        "reactive_event_types", defaults.get("reactive_event_types", [])
    )
    if isinstance(reactive_event_types_raw, list):
        reactive_event_types = [
            str(value).strip()
            for value in reactive_event_types_raw
            if str(value).strip()
        ]
    else:
        reactive_event_types = []
    reactive_debounce_seconds_raw = cfg.get(
        "reactive_debounce_seconds", defaults.get("reactive_debounce_seconds", 300)
    )
    try:
        reactive_debounce_seconds = int(reactive_debounce_seconds_raw)
    except (ValueError, TypeError):
        reactive_debounce_seconds = 300
    if reactive_debounce_seconds < 0:
        reactive_debounce_seconds = 0
    reactive_origin_blocklist_raw = cfg.get(
        "reactive_origin_blocklist",
        defaults.get("reactive_origin_blocklist", ["pma"]),
    )
    if isinstance(reactive_origin_blocklist_raw, list):
        reactive_origin_blocklist = [
            str(value).strip()
            for value in reactive_origin_blocklist_raw
            if str(value).strip()
        ]
    else:
        reactive_origin_blocklist = []
    cleanup_require_archive = bool(
        cfg.get(
            "cleanup_require_archive", defaults.get("cleanup_require_archive", True)
        )
    )
    cleanup_auto_delete_orphans = bool(
        cfg.get(
            "cleanup_auto_delete_orphans",
            defaults.get("cleanup_auto_delete_orphans", False),
        )
    )
    worktree_archive_profile = (
        str(
            cfg.get(
                "worktree_archive_profile",
                defaults.get("worktree_archive_profile", "portable"),
            )
        )
        .strip()
        .lower()
    )
    if worktree_archive_profile not in {"portable", "full"}:
        worktree_archive_profile = "portable"

    def _parse_nonnegative_int(name: str, fallback: int) -> int:
        raw = cfg.get(name, defaults.get(name, fallback))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = fallback
        return max(0, value)

    filebox_inbox_max_age_days = _parse_nonnegative_int("filebox_inbox_max_age_days", 7)
    filebox_outbox_max_age_days = _parse_nonnegative_int(
        "filebox_outbox_max_age_days", 7
    )
    report_max_history_files = _parse_nonnegative_int(
        "report_max_history_files", DEFAULT_REPORT_MAX_HISTORY_FILES
    )
    report_max_total_bytes = _parse_nonnegative_int(
        "report_max_total_bytes", DEFAULT_REPORT_MAX_TOTAL_BYTES
    )
    app_server_workspace_max_age_days = _parse_nonnegative_int(
        "app_server_workspace_max_age_days", 7
    )
    inbox_auto_dismiss_grace_seconds = _parse_nonnegative_int(
        "inbox_auto_dismiss_grace_seconds", 3600
    )
    worktree_archive_max_snapshots_per_repo = _parse_nonnegative_int(
        "worktree_archive_max_snapshots_per_repo", 10
    )
    worktree_archive_max_age_days = _parse_nonnegative_int(
        "worktree_archive_max_age_days", 30
    )
    worktree_archive_max_total_bytes = _parse_nonnegative_int(
        "worktree_archive_max_total_bytes", 1_000_000_000
    )
    run_archive_max_entries = _parse_nonnegative_int("run_archive_max_entries", 200)
    run_archive_max_age_days = _parse_nonnegative_int("run_archive_max_age_days", 30)
    run_archive_max_total_bytes = _parse_nonnegative_int(
        "run_archive_max_total_bytes", 1_000_000_000
    )
    orchestration_compaction_max_hot_rows = _parse_nonnegative_int(
        "orchestration_compaction_max_hot_rows",
        16,
    )
    if orchestration_compaction_max_hot_rows <= 0:
        orchestration_compaction_max_hot_rows = 16
    orchestration_hot_history_retention_days = _parse_nonnegative_int(
        "orchestration_hot_history_retention_days",
        30,
    )
    orchestration_cold_trace_retention_days = _parse_nonnegative_int(
        "orchestration_cold_trace_retention_days",
        90,
    )
    return PmaConfig(
        enabled=enabled,
        default_agent=default_agent,
        profile=profile,
        model=model,
        reasoning=reasoning,
        managed_thread_terminal_followup_default=managed_thread_terminal_followup_default,
        max_upload_bytes=max_upload_bytes,
        max_repos=max_repos,
        max_messages=max_messages,
        max_text_chars=max_text_chars,
        docs_max_chars=docs_max_chars,
        active_context_max_lines=active_context_max_lines,
        context_log_tail_lines=context_log_tail_lines,
        freshness_stale_threshold_seconds=freshness_stale_threshold_seconds,
        dispatch_interception_enabled=dispatch_interception_enabled,
        reactive_enabled=reactive_enabled,
        reactive_event_types=reactive_event_types,
        reactive_debounce_seconds=reactive_debounce_seconds,
        reactive_origin_blocklist=reactive_origin_blocklist,
        filebox_inbox_max_age_days=filebox_inbox_max_age_days,
        filebox_outbox_max_age_days=filebox_outbox_max_age_days,
        report_max_history_files=report_max_history_files,
        report_max_total_bytes=report_max_total_bytes,
        app_server_workspace_max_age_days=app_server_workspace_max_age_days,
        inbox_auto_dismiss_grace_seconds=inbox_auto_dismiss_grace_seconds,
        cleanup_require_archive=cleanup_require_archive,
        cleanup_auto_delete_orphans=cleanup_auto_delete_orphans,
        worktree_archive_profile=worktree_archive_profile,
        worktree_archive_max_snapshots_per_repo=worktree_archive_max_snapshots_per_repo,
        worktree_archive_max_age_days=worktree_archive_max_age_days,
        worktree_archive_max_total_bytes=worktree_archive_max_total_bytes,
        run_archive_max_entries=run_archive_max_entries,
        run_archive_max_age_days=run_archive_max_age_days,
        run_archive_max_total_bytes=run_archive_max_total_bytes,
        orchestration_compaction_max_hot_rows=orchestration_compaction_max_hot_rows,
        orchestration_hot_history_retention_days=orchestration_hot_history_retention_days,
        orchestration_cold_trace_retention_days=orchestration_cold_trace_retention_days,
    )


def _parse_usage_config(
    cfg: Optional[Dict[str, Any]],
    root: Path,
    defaults: Optional[Dict[str, Any]],
) -> UsageConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    cache_scope = str(cfg.get("cache_scope", defaults.get("cache_scope", "global")))
    cache_scope = cache_scope.lower().strip() or "global"
    global_cache_raw = cfg.get("global_cache_root", defaults.get("global_cache_root"))
    if global_cache_raw is None:
        global_cache_raw = os.environ.get("CODEX_HOME", "~/.codex")
    global_cache_root = resolve_config_path(
        global_cache_raw,
        root,
        allow_absolute=True,
        allow_home=True,
        scope="usage.global_cache_root",
    )
    repo_cache_raw = cfg.get("repo_cache_path", defaults.get("repo_cache_path"))
    if repo_cache_raw is None:
        repo_cache_raw = ".codex-autorunner/usage/usage_series_cache.json"
    repo_cache_path = resolve_config_path(
        repo_cache_raw,
        root,
        scope="usage.repo_cache_path",
    )
    return UsageConfig(
        cache_scope=cache_scope,
        global_cache_root=global_cache_root,
        repo_cache_path=repo_cache_path,
    )


def _parse_templates_config(
    cfg: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, Any]],
) -> TemplatesConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    enabled_raw = cfg.get("enabled", defaults.get("enabled", True))
    if "enabled" in cfg and not isinstance(enabled_raw, bool):
        raise ConfigError("templates.enabled must be boolean")
    enabled = bool(enabled_raw)
    repos_raw = cfg.get("repos", defaults.get("repos", []))
    if repos_raw is None:
        repos_raw = []
    if not isinstance(repos_raw, list):
        raise ConfigError("templates.repos must be a list")
    repos: List[TemplateRepoConfig] = []
    seen_ids: set[str] = set()
    for idx, repo in enumerate(repos_raw):
        if not isinstance(repo, dict):
            raise ConfigError(f"templates.repos[{idx}] must be a mapping")
        repo_id = repo.get("id")
        if not isinstance(repo_id, str) or not repo_id.strip():
            raise ConfigError(f"templates.repos[{idx}].id must be a non-empty string")
        repo_id = repo_id.strip()
        if repo_id in seen_ids:
            raise ConfigError(f"templates.repos[{idx}].id must be unique")
        seen_ids.add(repo_id)
        url = repo.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ConfigError(f"templates.repos[{idx}].url must be a non-empty string")
        trusted = repo.get("trusted", False)
        if "trusted" in repo and not isinstance(trusted, bool):
            raise ConfigError(f"templates.repos[{idx}].trusted must be boolean")
        default_ref = repo.get("default_ref", "main")
        if not isinstance(default_ref, str) or not default_ref.strip():
            raise ConfigError(
                f"templates.repos[{idx}].default_ref must be a non-empty string"
            )
        repos.append(
            TemplateRepoConfig(
                id=repo_id,
                url=url.strip(),
                trusted=bool(trusted),
                default_ref=default_ref.strip(),
            )
        )
    return TemplatesConfig(enabled=enabled, repos=repos)


def _parse_static_assets_config(
    cfg: Optional[Dict[str, Any]],
    root: Path,
    defaults: Dict[str, Any],
) -> StaticAssetsConfig:
    if not isinstance(cfg, dict):
        cfg = defaults
    cache_root_raw = cfg.get("cache_root", defaults.get("cache_root"))
    if cache_root_raw is None:
        raise ConfigError("static_assets.cache_root is required")
    cache_root = resolve_config_path(
        cache_root_raw,
        root,
        allow_home=True,
        scope="static_assets.cache_root",
    )
    max_cache_entries = int(
        cfg.get("max_cache_entries", defaults.get("max_cache_entries", 0))
    )
    max_cache_age_days_raw = cfg.get(
        "max_cache_age_days", defaults.get("max_cache_age_days")
    )
    max_cache_age_days = _parse_optional_int(max_cache_age_days_raw)
    return StaticAssetsConfig(
        cache_root=cache_root,
        max_cache_entries=max_cache_entries,
        max_cache_age_days=max_cache_age_days,
    )


def load_dotenv_for_root(root: Path) -> None:
    """
    Best-effort load of environment variables for the provided repo root.

    We intentionally load from deterministic locations rather than relying on
    process CWD (which differs for installed entrypoints, launchd, etc.).
    """
    try:
        root = root.resolve()
        candidates = [
            root / ".env",
            root / ".codex-autorunner" / ".env",
        ]

        for candidate in candidates:
            if candidate.exists():
                # Prefer repo-local .env over inherited process env to avoid stale keys
                # (common when running via launchd/daemon or with a global shell export).
                load_dotenv(dotenv_path=candidate, override=True)
    except OSError as exc:
        logger.debug("Failed to load .env file: %s", exc)


def _parse_dotenv_fallback(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export ") :].strip()
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if value and value[0] in {"'", '"'} and value[-1] == value[0]:
                value = value[1:-1]
            env[key] = value
    except OSError:
        return {}
    return env


def resolve_env_for_root(
    root: Path, base_env: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    """
    Return a merged env mapping for a repo root without mutating process env.

    Precedence mirrors load_dotenv_for_root: root/.env then root/.codex-autorunner/.env.
    """
    env = dict(base_env) if base_env is not None else dict(os.environ)
    candidates = [
        root / ".env",
        root / ".codex-autorunner" / ".env",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        if DOTENV_AVAILABLE:
            values = dotenv_values(candidate)
            if isinstance(values, dict):
                for key, value in values.items():
                    if key and value is not None:
                        env[str(key)] = str(value)
                continue
        env.update(_parse_dotenv_fallback(candidate))
    return env


VOICE_ENV_OVERRIDES = (
    "CODEX_AUTORUNNER_VOICE_ENABLED",
    "CODEX_AUTORUNNER_VOICE_PROVIDER",
    "CODEX_AUTORUNNER_VOICE_LATENCY",
    "CODEX_AUTORUNNER_VOICE_CHUNK_MS",
    "CODEX_AUTORUNNER_VOICE_SAMPLE_RATE",
    "CODEX_AUTORUNNER_VOICE_WARN_REMOTE",
    "CODEX_AUTORUNNER_VOICE_MAX_MS",
    "CODEX_AUTORUNNER_VOICE_SILENCE_MS",
    "CODEX_AUTORUNNER_VOICE_MIN_HOLD_MS",
)

COMMON_ENV_OVERRIDES = (GLOBAL_APP_SERVER_COMMAND_ENV,)

TELEGRAM_ENV_OVERRIDES = (
    "CAR_OPENCODE_COMMAND",
    LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV,
)

DISCORD_ENV_OVERRIDES = (
    "CAR_DISCORD_BOT_TOKEN",
    "CAR_DISCORD_APP_ID",
)


def collect_env_overrides(
    *,
    env: Optional[Mapping[str, str]] = None,
    include_telegram: bool = False,
    include_discord: bool = False,
) -> list[str]:
    source = env if env is not None else os.environ
    overrides: list[str] = []

    def _has_value(key: str) -> bool:
        value = source.get(key)
        if value is None:
            return False
        return str(value).strip() != ""

    if source.get("CODEX_AUTORUNNER_SKIP_UPDATE_CHECKS") == "1":
        overrides.append("CODEX_AUTORUNNER_SKIP_UPDATE_CHECKS")
    if _has_value("CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS"):
        overrides.append("CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS")
    if _has_value("CAR_GLOBAL_STATE_ROOT"):
        overrides.append("CAR_GLOBAL_STATE_ROOT")
    for key in VOICE_ENV_OVERRIDES:
        if _has_value(key):
            overrides.append(key)
    for key in COMMON_ENV_OVERRIDES:
        if _has_value(key):
            overrides.append(key)
    if include_telegram:
        for key in TELEGRAM_ENV_OVERRIDES:
            if _has_value(key):
                overrides.append(key)
    if include_discord:
        for key in DISCORD_ENV_OVERRIDES:
            if _has_value(key):
                overrides.append(key)
    return overrides


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
    log_cfg = cfg.get("log", {})
    log_cfg = cast(Dict[str, Any], log_cfg if isinstance(log_cfg, dict) else {})
    server_log_cfg = cfg.get("server_log", {}) or {}
    server_log_cfg = cast(
        Dict[str, Any], server_log_cfg if isinstance(server_log_cfg, dict) else {}
    )
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
        log=LogConfig(
            path=root / log_cfg.get("path", DEFAULT_REPO_CONFIG["log"]["path"]),
            max_bytes=int(
                log_cfg.get("max_bytes", DEFAULT_REPO_CONFIG["log"]["max_bytes"])
            ),
            backup_count=int(
                log_cfg.get("backup_count", DEFAULT_REPO_CONFIG["log"]["backup_count"])
            ),
        ),
        server_log=LogConfig(
            path=root
            / server_log_cfg.get("path", DEFAULT_REPO_CONFIG["server_log"]["path"]),
            max_bytes=int(
                server_log_cfg.get(
                    "max_bytes", DEFAULT_REPO_CONFIG["server_log"]["max_bytes"]
                )
            ),
            backup_count=int(
                server_log_cfg.get(
                    "backup_count",
                    DEFAULT_REPO_CONFIG["server_log"]["backup_count"],
                )
            ),
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
    log_cfg = hub_cfg["log"]
    server_log_cfg = cfg.get("server_log")
    # Default to hub log if server_log is not configured.
    if not isinstance(server_log_cfg, dict):
        server_log_cfg = {
            "path": log_cfg["path"],
            "max_bytes": log_cfg["max_bytes"],
            "backup_count": log_cfg["backup_count"],
        }

    log_path_str = log_cfg["path"]
    try:
        log_path = resolve_config_path(log_path_str, root, scope="log.path")
    except ConfigPathError as exc:
        raise ConfigError(str(exc)) from exc

    server_log_path_str = str(server_log_cfg.get("path", log_cfg["path"]))
    try:
        server_log_path = resolve_config_path(
            server_log_path_str,
            root,
            scope="server_log.path",
        )
    except ConfigPathError as exc:
        raise ConfigError(str(exc)) from exc

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
        log=LogConfig(
            path=log_path,
            max_bytes=int(log_cfg["max_bytes"]),
            backup_count=int(log_cfg["backup_count"]),
        ),
        server_log=LogConfig(
            path=server_log_path,
            max_bytes=int(server_log_cfg.get("max_bytes", log_cfg["max_bytes"])),
            backup_count=int(
                server_log_cfg.get("backup_count", log_cfg["backup_count"])
            ),
        ),
        static_assets=_parse_static_assets_config(
            cfg.get("static_assets"), root, DEFAULT_HUB_CONFIG["static_assets"]
        ),
        housekeeping=parse_housekeeping_config(cfg.get("housekeeping")),
    )
