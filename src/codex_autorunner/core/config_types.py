import dataclasses
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict, Union, cast

from ..housekeeping import HousekeepingConfig
from .agent_config import (
    AgentConfig,
    AgentProfileConfig,
    ResolvedAgentTarget,
    resolve_agent_target_from_agents,
)
from .config_contract import ConfigError
from .report_retention import (
    DEFAULT_REPORT_MAX_HISTORY_FILES,
    DEFAULT_REPORT_MAX_TOTAL_BYTES,
)

_DEFAULT_FLOW_RETENTION_DAYS = 7
_DEFAULT_FLOW_SWEEP_INTERVAL_SECONDS = 24 * 60 * 60


@dataclasses.dataclass(frozen=True)
class FlowRetentionConfig:
    retention_days: int = _DEFAULT_FLOW_RETENTION_DAYS
    sweep_interval_seconds: int = _DEFAULT_FLOW_SWEEP_INTERVAL_SECONDS


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
    turn_timeout_seconds: int
    managed_thread_terminal_followup_default: bool
    max_upload_bytes: int
    max_repos: int
    max_messages: int
    max_text_chars: int
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
    max_total_turns: Optional[int] = None


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
        default_factory=lambda: cast(DestinationConfigSection, {"kind": "local"})
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
