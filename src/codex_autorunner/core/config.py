import dataclasses
import json
import logging
import os
import shlex
from os import PathLike
from pathlib import Path
from typing import IO, Any, Dict, List, Mapping, Optional, Union, cast

import yaml

from ..housekeeping import HousekeepingConfig, parse_housekeeping_config
from ..manifest import load_manifest
from .config_contract import CONFIG_VERSION, ConfigError
from .destinations import default_local_destination, resolve_effective_repo_destination
from .path_utils import ConfigPathError, resolve_config_path
from .utils import atomic_write

logger = logging.getLogger("codex_autorunner.core.config")

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


CONFIG_FILENAME = ".codex-autorunner/config.yml"
ROOT_CONFIG_FILENAME = "codex-autorunner.yml"
ROOT_OVERRIDE_FILENAME = "codex-autorunner.override.yml"
REPO_OVERRIDE_FILENAME = ".codex-autorunner/repo.override.yml"
TWELVE_HOUR_SECONDS = 12 * 60 * 60


def _default_agents_section() -> Dict[str, Any]:
    """Build the default agents section."""
    return {
        "codex": {
            "binary": "codex",
        },
        "opencode": {
            "binary": "opencode",
            "subagent_models": {
                "subagent": "zai-coding-plan/glm-4.7-flashx",
            },
        },
    }


def _default_app_server_section() -> Dict[str, Any]:
    """Build the default app_server section."""
    return {
        "command": ["codex", "app-server"],
        "state_root": "~/.codex-autorunner/workspaces",
        "auto_restart": True,
        "max_handles": 20,
        "idle_ttl_seconds": 3600,
        "turn_timeout_seconds": 28800,
        "turn_stall_timeout_seconds": 60,
        "turn_stall_poll_interval_seconds": 2,
        "turn_stall_recovery_min_interval_seconds": 10,
        "request_timeout": None,
        "client": {
            "max_message_bytes": 50 * 1024 * 1024,
            "oversize_preview_bytes": 4096,
            "max_oversize_drain_bytes": 100 * 1024 * 1024,
            "restart_backoff_initial_seconds": 0.5,
            "restart_backoff_max_seconds": 30.0,
            "restart_backoff_jitter_ratio": 0.1,
        },
        "output": {
            "policy": "final_only",
        },
        "prompts": {
            "doc_chat": {
                "max_chars": 12000,
                "message_max_chars": 2000,
                "target_excerpt_max_chars": 4000,
                "recent_summary_max_chars": 2000,
            },
            "spec_ingest": {
                "max_chars": 12000,
                "message_max_chars": 2000,
                "spec_excerpt_max_chars": 5000,
            },
            "autorunner": {
                "max_chars": 16000,
                "message_max_chars": 2000,
                "todo_excerpt_max_chars": 4000,
                "prev_run_max_chars": 3000,
            },
        },
    }


def _default_telegram_bot_section(
    shell_enabled: bool = True, include_agent_timeouts: bool = False
) -> Dict[str, Any]:
    """Build the default telegram_bot section.

    Args:
        shell_enabled: Whether shell integration is enabled (True for repo, False for hub).
        include_agent_timeouts: Whether to include agent_timeouts (True for repo, False for hub).
    """
    config = {
        "enabled": False,
        "mode": "polling",
        "bot_token_env": "CAR_TELEGRAM_BOT_TOKEN",
        "chat_id_env": "CAR_TELEGRAM_CHAT_ID",
        "parse_mode": "HTML",
        "debug": {
            "prefix_context": False,
        },
        "allowed_chat_ids": [],
        "allowed_user_ids": [],
        "require_topics": False,
        "defaults": {
            "approval_mode": "yolo",
            "approval_policy": "on-request",
            "sandbox_policy": "dangerFullAccess",
            "yolo_approval_policy": "never",
            "yolo_sandbox_policy": "dangerFullAccess",
        },
        "concurrency": {
            "max_parallel_turns": 5,
            "per_topic_queue": True,
        },
        "media": {
            "enabled": True,
            "images": True,
            "voice": True,
            "files": True,
            "max_image_bytes": 10_000_000,
            "max_voice_bytes": 10_000_000,
            "max_file_bytes": 100_000_000,
            "image_prompt": (
                "The user sent an image with no caption. Use it to continue the "
                "conversation; if no clear task, describe the image and ask what "
                "they want."
            ),
        },
        "shell": {
            "enabled": shell_enabled,
            "timeout_ms": 120000,
            "max_output_chars": 3800,
        },
        "cache": {
            "cleanup_interval_seconds": 300,
            "coalesce_buffer_ttl_seconds": 60,
            "media_batch_buffer_ttl_seconds": 60,
            "model_pending_ttl_seconds": 1800,
            "pending_approval_ttl_seconds": 600,
            "pending_question_ttl_seconds": 600,
            "reasoning_buffer_ttl_seconds": 900,
            "selection_state_ttl_seconds": 1800,
            "turn_preview_ttl_seconds": 900,
            "progress_stream_ttl_seconds": 900,
            "oversize_warning_ttl_seconds": 3600,
            "update_id_persist_interval_seconds": 60,
        },
        "command_registration": {
            "enabled": True,
            "scopes": [
                {"type": "default", "language_code": ""},
                {"type": "all_group_chats", "language_code": ""},
            ],
        },
        "opencode_command": None,
        "state_file": ".codex-autorunner/telegram_state.sqlite3",
        "app_server_command_env": "CAR_TELEGRAM_APP_SERVER_COMMAND",
        "app_server_command": ["codex", "app-server"],
        "app_server": {
            "max_handles": 20,
            "idle_ttl_seconds": 3600,
            "turn_timeout_seconds": 28800,
        },
        "polling": {
            "timeout_seconds": 30,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        },
    }

    if include_agent_timeouts:
        config["agent_timeouts"] = {
            "codex": 28800,
            "opencode": 28800,
        }

    return config


def _default_discord_bot_section() -> Dict[str, Any]:
    """Build the default discord_bot section."""
    return {
        "enabled": False,
        "bot_token_env": "CAR_DISCORD_BOT_TOKEN",
        "app_id_env": "CAR_DISCORD_APP_ID",
        "allowed_guild_ids": [],
        "allowed_channel_ids": [],
        "allowed_user_ids": [],
        "command_registration": {
            "enabled": True,
            "scope": "guild",
            "guild_ids": [],
        },
        "state_file": ".codex-autorunner/discord_state.sqlite3",
        "intents": 33281,
        "max_message_length": 2000,
        "shell": {
            "enabled": True,
            "timeout_ms": 120000,
            "max_output_chars": 3800,
        },
        "media": {
            "enabled": True,
            "voice": True,
            "max_voice_bytes": 10_000_000,
        },
    }


def _default_terminal_section() -> Dict[str, Any]:
    """Build the default terminal section."""
    return {
        "idle_timeout_seconds": TWELVE_HOUR_SECONDS,
    }


def _default_opencode_section() -> Dict[str, Any]:
    """Build the default opencode section."""
    return {
        "server_scope": "workspace",
        "session_stall_timeout_seconds": None,
        "max_text_chars": 20000,
        "max_handles": 20,
        "idle_ttl_seconds": 3600,
    }


def _default_usage_section() -> Dict[str, Any]:
    """Build the default usage section."""
    return {
        "cache_scope": "global",
        "global_cache_root": None,
        "repo_cache_path": ".codex-autorunner/usage/usage_series_cache.json",
    }


def _default_server_section() -> Dict[str, Any]:
    """Build the default server section."""
    return {
        "host": "127.0.0.1",
        "port": 4173,
        "base_path": "",
        "access_log": False,
        "auth_token_env": "",
        "allowed_hosts": [],
        "allowed_origins": [],
    }


def _default_static_assets_section() -> Dict[str, Any]:
    """Build the default static_assets section."""
    return {
        "cache_root": ".codex-autorunner/static-cache",
        "max_cache_entries": 5,
        "max_cache_age_days": 30,
    }


def _default_update_section() -> Dict[str, Any]:
    """Build the default update section."""
    return {
        "skip_checks": False,
        "backend": "auto",
        "linux_service_names": _default_update_linux_service_names(),
    }


def _default_update_linux_service_names() -> Dict[str, str]:
    return {
        "hub": "car-hub",
        "telegram": "car-telegram",
        "discord": "car-discord",
    }


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


def _default_housekeeping_rules_basic() -> list:
    """Build the basic housekeeping rules (shared by repo and hub)."""
    return [
        {
            "name": "run_logs",
            "kind": "directory",
            "path": ".codex-autorunner/runs",
            "glob": "run-*.log",
            "recursive": False,
            "max_files": 200,
            "max_total_bytes": 500_000_000,
            "max_age_days": 30,
        },
        {
            "name": "terminal_image_uploads",
            "kind": "directory",
            "path": ".codex-autorunner/uploads/terminal-images",
            "glob": "*",
            "recursive": False,
            "max_files": 500,
            "max_total_bytes": 200_000_000,
            "max_age_days": 14,
        },
        {
            "name": "telegram_images",
            "kind": "directory",
            "path": ".codex-autorunner/uploads/telegram-images",
            "glob": "*",
            "recursive": False,
            "max_files": 500,
            "max_total_bytes": 200_000_000,
            "max_age_days": 14,
        },
        {
            "name": "telegram_voice",
            "kind": "directory",
            "path": ".codex-autorunner/uploads/telegram-voice",
            "glob": "*",
            "recursive": False,
            "max_files": 500,
            "max_total_bytes": 500_000_000,
            "max_age_days": 14,
        },
        {
            "name": "telegram_files",
            "kind": "directory",
            "path": ".codex-autorunner/uploads/telegram-files",
            "glob": "*",
            "recursive": True,
            "max_files": 500,
            "max_total_bytes": 500_000_000,
            "max_age_days": 14,
        },
        {
            "name": "github_context",
            "kind": "directory",
            "path": ".codex-autorunner/github_context",
            "glob": "*",
            "recursive": False,
            "max_files": 200,
            "max_total_bytes": 100_000_000,
            "max_age_days": 30,
        },
    ]


def _default_housekeeping_section(
    include_repo_review_runs: bool = False, include_hub_update_rules: bool = False
) -> Dict[str, Any]:
    """Build the default housekeeping section.

    Args:
        include_repo_review_runs: Whether to include review_runs rule (repo mode only).
        include_hub_update_rules: Whether to include hub-specific update rules (hub mode only).
    """
    rules = _default_housekeeping_rules_basic()

    if include_repo_review_runs:
        rules.append(
            {
                "name": "review_runs",
                "kind": "directory",
                "path": ".codex-autorunner/review/runs",
                "glob": "*",
                "recursive": True,
                "max_files": 100,
                "max_total_bytes": 500_000_000,
                "max_age_days": 30,
            }
        )

    if include_hub_update_rules:
        rules.extend(
            [
                {
                    "name": "update_cache",
                    "kind": "directory",
                    "path": "~/.codex-autorunner/update_cache",
                    "glob": "*",
                    "recursive": True,
                    "max_files": 2000,
                    "max_total_bytes": 1_000_000_000,
                    "max_age_days": 30,
                },
                {
                    "name": "update_log",
                    "kind": "file",
                    "path": "~/.codex-autorunner/update-standalone.log",
                    "max_bytes": 5_000_000,
                },
            ]
        )

    return {
        "enabled": True,
        "interval_seconds": 3600,
        "min_file_age_seconds": 600,
        "dry_run": False,
        "rules": rules,
    }


DEFAULT_REPO_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "mode": "repo",
    "docs": {
        "active_context": ".codex-autorunner/contextspace/active_context.md",
        "decisions": ".codex-autorunner/contextspace/decisions.md",
        "spec": ".codex-autorunner/contextspace/spec.md",
    },
    "review": {
        "enabled": True,
        "agent": "opencode",
        "model": "zai-coding-plan/glm-5",
        "subagent_agent": "subagent",
        "subagent_model": "zai-coding-plan/glm-4.7-flashx",
        "reasoning": None,
        "max_wallclock_seconds": None,
    },
    "codex": {
        "binary": "codex",
        "args": ["--yolo", "exec", "--sandbox", "danger-full-access"],
        "terminal_args": ["--yolo"],
        "model": None,
        "reasoning": None,
        # Optional model tiers for different Codex invocations.
        # If codex.models.large is unset/null, callers should avoid passing --model
        # so Codex uses the user's default/global profile model.
        "models": {
            "small": "gpt-5.1-codex-mini",
            "large": None,
        },
    },
    # Agent binaries/commands live here so adding new agents is config-driven.
    "agents": _default_agents_section(),
    "prompt": {
        "prev_run_max_chars": 6000,
        "template": ".codex-autorunner/prompt.txt",
    },
    "ui": {
        "editor": "vi",
    },
    "security": {
        "redact_run_logs": True,
    },
    "runner": {
        "sleep_seconds": 5,
        "stop_after_runs": None,
        "max_wallclock_seconds": None,
        "no_progress_threshold": 3,
        "review": {
            "enabled": False,
            "trigger": {
                "on_todos_complete": True,
                "on_no_progress_stop": True,
                "on_max_runs_stop": True,
                "on_stop_requested": False,
                "on_error_exit": False,
            },
            "agent": None,
            "model": None,
            "reasoning": None,
            "max_wallclock_seconds": None,
            "context": {
                "primary_docs": ["spec", "decisions"],
                "include_docs": ["active_context"],
                "include_last_run_artifacts": True,
                "max_doc_chars": 20000,
            },
            "artifacts": {
                "write_to_review_runs_dir": True,
            },
        },
    },
    "autorunner": {
        "reuse_session": False,
    },
    "ticket_flow": {
        "approval_mode": "yolo",
        # Keep ticket_flow deterministic by default; surfaces can tighten this.
        "default_approval_decision": "accept",
        "include_previous_ticket_context": False,
    },
    "git": {
        "auto_commit": False,
        "commit_message_template": "[codex] run #{run_id}",
    },
    "github": {
        "enabled": True,
        "pr_draft_default": True,
        "sync_commit_mode": "auto",  # none|auto|always
        # Bounds the agentic sync step in GitHubService.sync_pr (seconds).
        "sync_agent_timeout_seconds": 1800,
    },
    "update": _default_update_section(),
    "app_server": _default_app_server_section(),
    "opencode": _default_opencode_section(),
    "usage": _default_usage_section(),
    "server": _default_server_section(),
    "notifications": {
        "enabled": "auto",
        "events": ["run_finished", "run_error", "tui_idle"],
        "tui_idle_seconds": 60,
        "timeout_seconds": 5.0,
        "discord": {
            "webhook_url_env": "CAR_DISCORD_WEBHOOK_URL",
        },
        "telegram": {
            "bot_token_env": "CAR_TELEGRAM_BOT_TOKEN",
            "chat_id_env": "CAR_TELEGRAM_CHAT_ID",
        },
    },
    "telegram_bot": _default_telegram_bot_section(
        shell_enabled=True, include_agent_timeouts=True
    ),
    "discord_bot": _default_discord_bot_section(),
    "terminal": _default_terminal_section(),
    "voice": {
        "enabled": True,
        "provider": "openai_whisper",
        "latency_mode": "balanced",
        "chunk_ms": 600,
        "sample_rate": 16_000,
        "warn_on_remote_api": True,
        "push_to_talk": {
            "max_ms": 15_000,
            "silence_auto_stop_ms": 1_200,
            "min_hold_ms": 150,
        },
        "providers": {
            "openai_whisper": {
                "remote_api": True,
                "api_key_env": "OPENAI_API_KEY",
                "model": "whisper-1",
                "base_url": None,
                "temperature": 0,
                "language": None,
                "redact_request": True,
            },
            "local_whisper": {
                "remote_api": False,
                "model": "tiny",
                "device": "auto",
                "compute_type": "default",
                "cpu_threads": 0,
                "num_workers": 1,
                "download_root": None,
                "local_files_only": False,
                "beam_size": 1,
                "vad_filter": True,
                "language": None,
            },
        },
    },
    "log": {
        "path": ".codex-autorunner/codex-autorunner.log",
        "max_bytes": 10_000_000,
        "backup_count": 3,
    },
    "server_log": {
        "path": ".codex-autorunner/codex-server.log",
        "max_bytes": 10_000_000,
        "backup_count": 3,
    },
    "static_assets": {
        "cache_root": ".codex-autorunner/static-cache",
        "max_cache_entries": 5,
        "max_cache_age_days": 30,
    },
    "housekeeping": _default_housekeeping_section(include_repo_review_runs=True),
    "storage": {
        "durable_writes": False,
    },
}

REPO_DEFAULT_KEYS = {
    "docs",
    "codex",
    "prompt",
    "ui",
    "runner",
    "autorunner",
    "ticket_flow",
    "git",
    "github",
    "update",
    "notifications",
    "voice",
    "log",
    "server_log",
    "review",
    "opencode",
    "usage",
}
DEFAULT_REPO_DEFAULTS = {
    key: json.loads(json.dumps(DEFAULT_REPO_CONFIG[key])) for key in REPO_DEFAULT_KEYS
}
REPO_SHARED_KEYS = {
    "agents",
    "server",
    "app_server",
    "opencode",
    "telegram_bot",
    "discord_bot",
    "terminal",
    "static_assets",
    "housekeeping",
    "update",
    "usage",
    "templates",
}

DEFAULT_HUB_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "mode": "hub",
    "repo_defaults": DEFAULT_REPO_DEFAULTS,
    "pma": {
        "enabled": True,
        "default_agent": "codex",
        "model": None,
        "reasoning": None,
        "max_upload_bytes": 10_000_000,
        "max_repos": 25,
        "max_messages": 10,
        "max_text_chars": 800,
        # PMA durable context docs (hub-level)
        "docs_max_chars": 12_000,
        "active_context_max_lines": 200,
        "context_log_tail_lines": 120,
        "reactive_enabled": True,
        "reactive_event_types": [
            "flow_paused",
            "flow_failed",
            "flow_completed",
            "dispatch_created",
        ],
        "reactive_debounce_seconds": 300,
        "reactive_origin_blocklist": ["pma"],
        # Worktree cleanup policies
        "cleanup_require_archive": True,
        "cleanup_auto_delete_orphans": False,
    },
    "templates": {
        "enabled": True,
        "repos": [
            {
                "id": "blessed",
                "url": "https://github.com/Git-on-my-level/car-ticket-templates",
                "trusted": True,
                "default_ref": "main",
            }
        ],
    },
    "agents": _default_agents_section(),
    "terminal": _default_terminal_section(),
    "telegram_bot": _default_telegram_bot_section(
        shell_enabled=False, include_agent_timeouts=False
    ),
    "discord_bot": _default_discord_bot_section(),
    "hub": {
        "repos_root": ".",
        # Hub-managed git worktrees live here (depth=1 scan). Each worktree is treated as a repo.
        "worktrees_root": "worktrees",
        "manifest": ".codex-autorunner/manifest.yml",
        "discover_depth": 1,
        "auto_init_missing": True,
        # Include the hub root itself as a manifest repo entry (path: ".").
        "include_root_repo": False,
        "repo_server_inherit": True,
        # Where to pull system updates from (defaults to main upstream)
        "update_repo_url": "https://github.com/Git-on-my-level/codex-autorunner.git",
        "update_repo_ref": "main",
        "log": {
            "path": ".codex-autorunner/codex-autorunner-hub.log",
            "max_bytes": 10_000_000,
            "backup_count": 3,
        },
    },
    "update": _default_update_section(),
    "app_server": _default_app_server_section(),
    "opencode": _default_opencode_section(),
    "usage": _default_usage_section(),
    "server": _default_server_section(),
    # Hub already has hub.log, but we still support an explicit server_log for consistency.
    "server_log": None,
    "static_assets": _default_static_assets_section(),
    "housekeeping": _default_housekeeping_section(include_hub_update_rules=True),
    "storage": {
        "durable_writes": False,
    },
}

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
    model: Optional[str]
    reasoning: Optional[str]
    max_upload_bytes: int
    max_repos: int
    max_messages: int
    max_text_chars: int
    # Hub-level PMA durable context docs
    docs_max_chars: int = 12_000
    active_context_max_lines: int = 200
    context_log_tail_lines: int = 120
    dispatch_interception_enabled: bool = False
    reactive_enabled: bool = True
    reactive_event_types: List[str] = dataclasses.field(default_factory=list)
    reactive_debounce_seconds: int = 300
    reactive_origin_blocklist: List[str] = dataclasses.field(default_factory=list)
    # Worktree cleanup policies
    cleanup_require_archive: bool = True
    cleanup_auto_delete_orphans: bool = False


@dataclasses.dataclass
class UsageConfig:
    cache_scope: str
    global_cache_root: Path
    repo_cache_path: Path


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    binary: str
    serve_command: Optional[List[str]]
    base_url: Optional[str]
    subagent_models: Optional[Dict[str, str]]


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


@dataclasses.dataclass
class RepoConfig:
    raw: Dict[str, Any]
    root: Path
    version: int
    mode: str
    security: Dict[str, Any]
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
    usage: UsageConfig
    server_host: str
    server_port: int
    server_base_path: str
    server_access_log: bool
    server_auth_token_env: str
    server_allowed_hosts: List[str]
    server_allowed_origins: List[str]
    notifications: Dict[str, Any]
    terminal_idle_timeout_seconds: Optional[int]
    log: LogConfig
    server_log: LogConfig
    voice: Dict[str, Any]
    static_assets: StaticAssetsConfig
    housekeeping: HousekeepingConfig
    durable_writes: bool
    templates: TemplatesConfig
    effective_destination: Dict[str, Any] = dataclasses.field(
        default_factory=default_local_destination
    )

    def doc_path(self, key: str) -> Path:
        return self.root / self.docs[key]

    def agent_binary(self, agent_id: str) -> str:
        agent = self.agents.get(agent_id)
        if agent and agent.binary:
            return agent.binary
        raise ConfigError(f"agents.{agent_id}.binary is required")

    def agent_serve_command(self, agent_id: str) -> Optional[List[str]]:
        agent = self.agents.get(agent_id)
        if agent:
            return list(agent.serve_command) if agent.serve_command else None
        return None


@dataclasses.dataclass
class HubConfig:
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

    def agent_binary(self, agent_id: str) -> str:
        agent = self.agents.get(agent_id)
        if agent and agent.binary:
            return agent.binary
        raise ConfigError(f"agents.{agent_id}.binary is required")

    def agent_serve_command(self, agent_id: str) -> Optional[List[str]]:
        agent = self.agents.get(agent_id)
        if agent:
            return list(agent.serve_command) if agent.serve_command else None
        return None


# Alias used by existing code paths that only support repo mode
Config = RepoConfig


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


def _merge_defaults(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    merged = cast(Dict[str, Any], json.loads(json.dumps(base)))
    for key, value in overrides.items():
        if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml_dict(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    except Exception as exc:
        raise ConfigError(f"Failed to read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file must be a mapping: {path}")
    return data


def _load_root_config(root: Path) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    base_path = root / ROOT_CONFIG_FILENAME
    base = _load_yaml_dict(base_path)
    if base:
        merged = _merge_defaults(merged, base)
    override_path = root / ROOT_OVERRIDE_FILENAME
    try:
        override = _load_yaml_dict(override_path)
    except ConfigError as exc:
        raise ConfigError(
            f"Invalid override config {override_path}; fix or delete it: {exc}"
        ) from exc
    if override:
        merged = _merge_defaults(merged, override)
    return merged


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


def load_root_defaults(root: Path) -> Dict[str, Any]:
    """Load hub defaults from the root config + override file."""
    return _load_root_config(root)


def resolve_hub_config_data(
    root: Path, overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    merged = _merge_defaults(DEFAULT_HUB_CONFIG, load_root_defaults(root))
    if overrides:
        merged = _merge_defaults(merged, overrides)
    return merged


def repo_shared_overrides_from_hub(hub_data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: hub_data[key] for key in REPO_SHARED_KEYS if key in hub_data}


def _load_repo_override(repo_root: Path) -> Dict[str, Any]:
    override_path = repo_root / REPO_OVERRIDE_FILENAME
    data = _load_yaml_dict(override_path)
    if not data:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Repo override file must be a mapping: {override_path}")
    if "mode" in data or "version" in data:
        raise ConfigError(
            f"{override_path} must not set mode or version; those are hub-managed."
        )
    return data


def derive_repo_config_data(
    hub_data: Dict[str, Any], repo_root: Path
) -> Dict[str, Any]:
    repo_defaults = hub_data.get("repo_defaults") or {}
    if not isinstance(repo_defaults, dict):
        raise ConfigError("hub.repo_defaults must be a mapping if provided")
    merged = _merge_defaults(
        DEFAULT_REPO_CONFIG, repo_shared_overrides_from_hub(hub_data)
    )
    if repo_defaults:
        merged = _merge_defaults(merged, repo_defaults)
    repo_overrides = _load_repo_override(repo_root)
    if repo_overrides:
        merged = _merge_defaults(merged, repo_overrides)
    return merged


def find_nearest_hub_config_path(start: Path) -> Optional[Path]:
    start = start.resolve()
    search_dir = start if start.is_dir() else start.parent
    for current in [search_dir] + list(search_dir.parents):
        candidate = current / CONFIG_FILENAME
        if not candidate.exists():
            continue
        data = _load_yaml_dict(candidate)
        if data.get("mode") in (None, "hub"):
            return candidate
    return None


def _normalize_base_path(path: Optional[str]) -> str:
    """Normalize base path to either '' or a single-leading-slash path without trailing slash."""
    if not path:
        return ""
    normalized = str(path).strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = normalized.rstrip("/")
    return normalized or ""


def _parse_command(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, str):
        return [part for part in shlex.split(raw) if part]
    return []


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
    command = _parse_command(cfg.get("command", defaults.get("command")))
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
    max_handles = int(max_handles_raw) if max_handles_raw is not None else None
    if max_handles is not None and max_handles <= 0:
        max_handles = None
    idle_ttl_raw = cfg.get("idle_ttl_seconds", defaults.get("idle_ttl_seconds"))
    idle_ttl_seconds = int(idle_ttl_raw) if idle_ttl_raw is not None else None
    if idle_ttl_seconds is not None and idle_ttl_seconds <= 0:
        idle_ttl_seconds = None
    turn_timeout_raw = cfg.get(
        "turn_timeout_seconds", defaults.get("turn_timeout_seconds")
    )
    turn_timeout_seconds = (
        float(turn_timeout_raw) if turn_timeout_raw is not None else None
    )
    if turn_timeout_seconds is not None and turn_timeout_seconds <= 0:
        turn_timeout_seconds = None
    stall_timeout_raw = cfg.get(
        "turn_stall_timeout_seconds", defaults.get("turn_stall_timeout_seconds")
    )
    turn_stall_timeout_seconds = (
        float(stall_timeout_raw) if stall_timeout_raw is not None else None
    )
    if turn_stall_timeout_seconds is not None and turn_stall_timeout_seconds <= 0:
        turn_stall_timeout_seconds = None
    stall_poll_raw = cfg.get(
        "turn_stall_poll_interval_seconds",
        defaults.get("turn_stall_poll_interval_seconds"),
    )
    turn_stall_poll_interval_seconds = (
        float(stall_poll_raw) if stall_poll_raw is not None else None
    )
    if (
        turn_stall_poll_interval_seconds is not None
        and turn_stall_poll_interval_seconds <= 0
    ):
        turn_stall_poll_interval_seconds = defaults.get(
            "turn_stall_poll_interval_seconds"
        )
    stall_recovery_raw = cfg.get(
        "turn_stall_recovery_min_interval_seconds",
        defaults.get("turn_stall_recovery_min_interval_seconds"),
    )
    turn_stall_recovery_min_interval_seconds = (
        float(stall_recovery_raw) if stall_recovery_raw is not None else None
    )
    if (
        turn_stall_recovery_min_interval_seconds is not None
        and turn_stall_recovery_min_interval_seconds < 0
    ):
        turn_stall_recovery_min_interval_seconds = defaults.get(
            "turn_stall_recovery_min_interval_seconds"
        )
    request_timeout_raw = cfg.get("request_timeout", defaults.get("request_timeout"))
    request_timeout = (
        float(request_timeout_raw) if request_timeout_raw is not None else None
    )
    if request_timeout is not None and request_timeout <= 0:
        request_timeout = None
    client_defaults = defaults.get("client")
    client_defaults = client_defaults if isinstance(client_defaults, dict) else {}
    client_cfg_raw = cfg.get("client")
    client_cfg = client_cfg_raw if isinstance(client_cfg_raw, dict) else {}

    def _client_int(key: str) -> int:
        value = client_cfg.get(key, client_defaults.get(key))
        value = int(value) if value is not None else 0
        if value <= 0:
            value = int(client_defaults.get(key) or 0)
        return value

    def _client_float(key: str, *, allow_zero: bool = False) -> float:
        value = client_cfg.get(key, client_defaults.get(key))
        value = float(value) if value is not None else 0.0
        if value < 0 or (not allow_zero and value <= 0):
            value = float(client_defaults.get(key) or 0.0)
        return value

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
        request_timeout=request_timeout,
        client=AppServerClientConfig(
            max_message_bytes=_client_int("max_message_bytes"),
            oversize_preview_bytes=_client_int("oversize_preview_bytes"),
            max_oversize_drain_bytes=_client_int("max_oversize_drain_bytes"),
            restart_backoff_initial_seconds=_client_float(
                "restart_backoff_initial_seconds"
            ),
            restart_backoff_max_seconds=_client_float("restart_backoff_max_seconds"),
            restart_backoff_jitter_ratio=_client_float(
                "restart_backoff_jitter_ratio", allow_zero=True
            ),
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
    if server_scope not in {"workspace", "global"}:
        server_scope = "workspace"
    stall_timeout_raw = cfg.get(
        "session_stall_timeout_seconds",
        defaults.get("session_stall_timeout_seconds"),
    )
    stall_timeout_seconds = (
        float(stall_timeout_raw) if stall_timeout_raw is not None else None
    )
    if stall_timeout_seconds is not None and stall_timeout_seconds <= 0:
        stall_timeout_seconds = None
    max_text_chars_raw = cfg.get("max_text_chars", defaults.get("max_text_chars"))
    max_text_chars = (
        int(max_text_chars_raw)
        if isinstance(max_text_chars_raw, int) and max_text_chars_raw > 0
        else None
    )
    max_handles_raw = cfg.get("max_handles", defaults.get("max_handles"))
    max_handles = int(max_handles_raw) if max_handles_raw is not None else None
    if max_handles is not None and max_handles <= 0:
        max_handles = None
    idle_ttl_raw = cfg.get("idle_ttl_seconds", defaults.get("idle_ttl_seconds"))
    idle_ttl_seconds = int(idle_ttl_raw) if idle_ttl_raw is not None else None
    if idle_ttl_seconds is not None and idle_ttl_seconds <= 0:
        idle_ttl_seconds = None
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
    model_raw = cfg.get("model", defaults.get("model"))
    model = str(model_raw).strip() or None if model_raw else None
    reasoning_raw = cfg.get("reasoning", defaults.get("reasoning"))
    reasoning = str(reasoning_raw).strip() or None if reasoning_raw else None
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
    max_text_chars = _parse_positive_int("max_text_chars", 800)
    docs_max_chars = _parse_positive_int("docs_max_chars", 12_000)
    active_context_max_lines = _parse_positive_int("active_context_max_lines", 200)
    context_log_tail_lines = _parse_positive_int("context_log_tail_lines", 120)
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
    return PmaConfig(
        enabled=enabled,
        default_agent=default_agent,
        model=model,
        reasoning=reasoning,
        max_upload_bytes=max_upload_bytes,
        max_repos=max_repos,
        max_messages=max_messages,
        max_text_chars=max_text_chars,
        docs_max_chars=docs_max_chars,
        active_context_max_lines=active_context_max_lines,
        context_log_tail_lines=context_log_tail_lines,
        dispatch_interception_enabled=dispatch_interception_enabled,
        reactive_enabled=reactive_enabled,
        reactive_event_types=reactive_event_types,
        reactive_debounce_seconds=reactive_debounce_seconds,
        reactive_origin_blocklist=reactive_origin_blocklist,
        cleanup_require_archive=cleanup_require_archive,
        cleanup_auto_delete_orphans=cleanup_auto_delete_orphans,
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


def _parse_agents_config(
    cfg: Optional[Dict[str, Any]], defaults: Dict[str, Any]
) -> Dict[str, AgentConfig]:
    raw_agents = cfg.get("agents") if cfg else None
    if not isinstance(raw_agents, dict):
        raw_agents = defaults.get("agents", {})
    agents: Dict[str, AgentConfig] = {}
    for agent_id, agent_cfg in raw_agents.items():
        if not isinstance(agent_cfg, dict):
            continue
        binary = agent_cfg.get("binary")
        if not isinstance(binary, str) or not binary.strip():
            continue
        serve_command = None
        if "serve_command" in agent_cfg:
            serve_command = _parse_command(agent_cfg.get("serve_command"))
        base_url = agent_cfg.get("base_url")
        subagent_models = agent_cfg.get("subagent_models")
        if not isinstance(subagent_models, dict):
            subagent_models = None
        agents[str(agent_id)] = AgentConfig(
            binary=binary,
            serve_command=serve_command,
            base_url=base_url,
            subagent_models=subagent_models,
        )
    return agents


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
    max_cache_age_days = (
        int(max_cache_age_days_raw) if max_cache_age_days_raw is not None else None
    )
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

TELEGRAM_ENV_OVERRIDES = (
    "CAR_OPENCODE_COMMAND",
    "CAR_TELEGRAM_APP_SERVER_COMMAND",
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
    data = _load_yaml_dict(config_path)
    mode = data.get("mode")
    if mode not in (None, "hub"):
        raise ConfigError(f"Invalid mode '{mode}'; expected 'hub'")
    root = config_path.parent.parent.resolve()
    return resolve_hub_config_data(root, data)


def _resolve_hub_config_path(start: Path) -> Path:
    config_path = find_nearest_hub_config_path(start)
    if not config_path:
        # Auto-initialize hub config if missing in the current directory or parents.
        # If we are in a git repo, we'll initialize a hub there.
        try:
            from .utils import find_repo_root

            target_root = find_repo_root(start)
        except Exception:
            target_root = start

        from ..bootstrap import seed_hub_files

        seed_hub_files(target_root)
        config_path = find_nearest_hub_config_path(target_root)

    if not config_path:
        raise ConfigError(
            f"Missing hub config file; expected to find {CONFIG_FILENAME} in {start} or parents (use --hub to specify)"
        )
    return config_path


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
) -> Dict[str, Any]:
    try:
        manifest = load_manifest(hub.manifest_path, hub.root)
    except Exception:
        return default_local_destination()
    repo = manifest.get_by_path(hub.root, repo_root)
    if repo is None:
        return default_local_destination()
    repos_by_id = {entry.id: entry for entry in manifest.repos}
    resolution = resolve_effective_repo_destination(repo, repos_by_id)
    return resolution.to_dict()


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
    voice_cfg = cfg.get("voice") if isinstance(cfg.get("voice"), dict) else {}
    voice_cfg = cast(Dict[str, Any], voice_cfg)
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
    notifications_cfg = (
        cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    )
    notifications_cfg = cast(Dict[str, Any], notifications_cfg)
    security_cfg = cfg.get("security") if isinstance(cfg.get("security"), dict) else {}
    security_cfg = cast(Dict[str, Any], security_cfg)
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
