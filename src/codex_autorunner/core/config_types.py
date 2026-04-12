import dataclasses
from pathlib import Path
from typing import List, Literal, Optional, TypedDict, Union

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
