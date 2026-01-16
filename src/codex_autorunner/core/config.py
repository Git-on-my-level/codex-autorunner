import dataclasses
import ipaddress
import json
import shlex
from os import PathLike
from pathlib import Path
from typing import IO, Any, Dict, List, Optional, Union, cast

import yaml

from ..housekeeping import HousekeepingConfig, parse_housekeeping_config

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover

    def load_dotenv(
        dotenv_path: Optional[Union[str, PathLike[str]]] = None,
        stream: Optional[IO[str]] = None,
        verbose: bool = False,
        override: bool = False,
        interpolate: bool = True,
        encoding: Optional[str] = None,
    ) -> bool:
        return False


CONFIG_FILENAME = ".codex-autorunner/config.yml"
ROOT_CONFIG_FILENAME = "codex-autorunner.yml"
ROOT_OVERRIDE_FILENAME = "codex-autorunner.override.yml"
CONFIG_VERSION = 2
TWELVE_HOUR_SECONDS = 12 * 60 * 60

DEFAULT_REPO_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "mode": "repo",
    "docs": {
        "todo": ".codex-autorunner/TODO.md",
        "progress": ".codex-autorunner/PROGRESS.md",
        "opinions": ".codex-autorunner/OPINIONS.md",
        "spec": ".codex-autorunner/SPEC.md",
        "summary": ".codex-autorunner/SUMMARY.md",
        "snapshot": ".codex-autorunner/SNAPSHOT.md",
        "snapshot_state": ".codex-autorunner/snapshot_state.json",
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
    "prompt": {
        "prev_run_max_chars": 6000,
        "template": ".codex-autorunner/prompt.txt",
    },
    "runner": {
        "sleep_seconds": 5,
        "stop_after_runs": None,
        "max_wallclock_seconds": None,
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
    "app_server": {
        "command": ["codex", "app-server"],
        "state_root": "~/.codex-autorunner/workspaces",
        "max_handles": 20,
        "idle_ttl_seconds": 3600,
        "turn_timeout_seconds": 28800,
        "request_timeout": None,
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
    },
    "server": {
        "host": "127.0.0.1",
        "port": 4173,
        "base_path": "",
        "access_log": False,
        "auth_token_env": "",
        "allowed_hosts": [],
        "allowed_origins": [],
    },
    "notifications": {
        "enabled": "auto",
        "events": ["run_finished", "run_error", "tui_idle"],
        "tui_idle_seconds": 60,
        "discord": {
            "webhook_url_env": "CAR_DISCORD_WEBHOOK_URL",
        },
        "telegram": {
            "bot_token_env": "CAR_TELEGRAM_BOT_TOKEN",
            "chat_id_env": "CAR_TELEGRAM_CHAT_ID",
        },
    },
    "telegram_bot": {
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
            "max_file_bytes": 10_000_000,
            "image_prompt": "Describe the image.",
        },
        "shell": {
            "enabled": True,
            "timeout_ms": 120000,
            "max_output_chars": 3800,
        },
        "command_registration": {
            "enabled": True,
            "scopes": [
                {"type": "default", "language_code": ""},
                {"type": "all_group_chats", "language_code": ""},
            ],
        },
        "state_file": ".codex-autorunner/telegram_state.json",
        "app_server_command_env": "CAR_TELEGRAM_APP_SERVER_COMMAND",
        "app_server_command": ["codex", "app-server"],
        "app_server": {
            "max_handles": 20,
            "idle_ttl_seconds": 3600,
        },
        "polling": {
            "timeout_seconds": 30,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        },
    },
    "terminal": {
        "idle_timeout_seconds": TWELVE_HOUR_SECONDS,
    },
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
                "api_key_env": "OPENAI_API_KEY",
                "model": "whisper-1",
                "base_url": None,
                "temperature": 0,
                "language": None,
                "redact_request": True,
            }
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
    "housekeeping": {
        "enabled": True,
        "interval_seconds": 3600,
        "min_file_age_seconds": 600,
        "dry_run": False,
        "rules": [
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
        ],
    },
}

DEFAULT_HUB_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "mode": "hub",
    "terminal": {
        "idle_timeout_seconds": TWELVE_HOUR_SECONDS,
    },
    "telegram_bot": {
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
            "max_file_bytes": 10_000_000,
            "image_prompt": "Describe the image.",
        },
        "shell": {
            "enabled": False,
            "timeout_ms": 120000,
            "max_output_chars": 3800,
        },
        "command_registration": {
            "enabled": True,
            "scopes": [
                {"type": "default", "language_code": ""},
                {"type": "all_group_chats", "language_code": ""},
            ],
        },
        "state_file": ".codex-autorunner/telegram_state.json",
        "app_server_command_env": "CAR_TELEGRAM_APP_SERVER_COMMAND",
        "app_server_command": ["codex", "app-server"],
        "app_server": {
            "max_handles": 20,
            "idle_ttl_seconds": 3600,
        },
        "polling": {
            "timeout_seconds": 30,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        },
    },
    "hub": {
        "repos_root": ".",
        # Hub-managed git worktrees live here (depth=1 scan). Each worktree is treated as a repo.
        "worktrees_root": "worktrees",
        "manifest": ".codex-autorunner/manifest.yml",
        "discover_depth": 1,
        "auto_init_missing": True,
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
    "app_server": {
        "command": ["codex", "app-server"],
        "state_root": "~/.codex-autorunner/workspaces",
        "max_handles": 20,
        "idle_ttl_seconds": 3600,
        "turn_timeout_seconds": 28800,
        "request_timeout": None,
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
    },
    "server": {
        "host": "127.0.0.1",
        "port": 4173,
        "base_path": "",
        "access_log": False,
        "auth_token_env": "",
        "allowed_hosts": [],
        "allowed_origins": [],
    },
    # Hub already has hub.log, but we still support an explicit server_log for consistency.
    "server_log": None,
    "static_assets": {
        "cache_root": ".codex-autorunner/static-cache",
        "max_cache_entries": 5,
        "max_cache_age_days": 30,
    },
    "housekeeping": {
        "enabled": True,
        "interval_seconds": 3600,
        "min_file_age_seconds": 600,
        "dry_run": False,
        "rules": [
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
        ],
    },
}

# Backwards-compatible alias for repo defaults
DEFAULT_CONFIG = DEFAULT_REPO_CONFIG


class ConfigError(Exception):
    """Raised when configuration is invalid."""


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
class AppServerConfig:
    command: List[str]
    state_root: Path
    max_handles: Optional[int]
    idle_ttl_seconds: Optional[int]
    turn_timeout_seconds: Optional[float]
    request_timeout: Optional[float]
    prompts: AppServerPromptsConfig


@dataclasses.dataclass
class RepoConfig:
    raw: Dict[str, Any]
    root: Path
    version: int
    mode: str
    docs: Dict[str, Path]
    codex_binary: str
    codex_args: List[str]
    codex_terminal_args: List[str]
    codex_model: Optional[str]
    codex_reasoning: Optional[str]
    prompt_prev_run_max_chars: int
    prompt_template: Optional[Path]
    runner_sleep_seconds: int
    runner_stop_after_runs: Optional[int]
    runner_max_wallclock_seconds: Optional[int]
    git_auto_commit: bool
    git_commit_message_template: str
    app_server: AppServerConfig
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

    def doc_path(self, key: str) -> Path:
        return self.root / self.docs[key]


@dataclasses.dataclass
class HubConfig:
    raw: Dict[str, Any]
    root: Path
    version: int
    mode: str
    repos_root: Path
    worktrees_root: Path
    manifest_path: Path
    discover_depth: int
    auto_init_missing: bool
    repo_server_inherit: bool
    update_repo_url: str
    update_repo_ref: str
    app_server: AppServerConfig
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


# Alias used by existing code paths that only support repo mode
Config = RepoConfig


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


def load_root_defaults(root: Path, mode: str) -> Dict[str, Any]:
    """Load repo/hub defaults from the root config + override file."""
    raw = _load_root_config(root)
    if not raw:
        return {}
    if "repo" in raw or "hub" in raw:
        if mode == "hub":
            return raw.get("hub", {}) if isinstance(raw.get("hub"), dict) else {}
        return raw.get("repo", {}) if isinstance(raw.get("repo"), dict) else {}
    return raw


def resolve_config_data(
    root: Path, mode: str, overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    if mode not in ("repo", "hub"):
        raise ConfigError(f"Invalid mode '{mode}'; expected 'hub' or 'repo'")
    base = DEFAULT_HUB_CONFIG if mode == "hub" else DEFAULT_REPO_CONFIG
    merged = _merge_defaults(base, load_root_defaults(root, mode))
    if overrides:
        merged = _merge_defaults(merged, overrides)
    return merged


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


def _parse_app_server_config(
    cfg: Optional[Dict[str, Any]],
    root: Path,
    defaults: Dict[str, Any],
) -> AppServerConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    command = _parse_command(cfg.get("command", defaults.get("command")))
    state_root_raw = cfg.get("state_root", defaults.get("state_root"))
    state_root = Path(str(state_root_raw)).expanduser()
    if not state_root.is_absolute():
        state_root = root / state_root
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
    request_timeout_raw = cfg.get("request_timeout", defaults.get("request_timeout"))
    request_timeout = (
        float(request_timeout_raw) if request_timeout_raw is not None else None
    )
    if request_timeout is not None and request_timeout <= 0:
        request_timeout = None
    prompt_defaults = defaults.get("prompts")
    prompts = _parse_app_server_prompts_config(cfg.get("prompts"), prompt_defaults)
    return AppServerConfig(
        command=command,
        state_root=state_root,
        max_handles=max_handles,
        idle_ttl_seconds=idle_ttl_seconds,
        turn_timeout_seconds=turn_timeout_seconds,
        request_timeout=request_timeout,
        prompts=prompts,
    )


def _parse_static_assets_config(
    cfg: Optional[Dict[str, Any]],
    root: Path,
    defaults: Dict[str, Any],
) -> StaticAssetsConfig:
    if not isinstance(cfg, dict):
        cfg = defaults
    cache_root_raw = cfg.get("cache_root", defaults.get("cache_root"))
    cache_root = Path(str(cache_root_raw))
    if not cache_root.is_absolute():
        cache_root = root / cache_root
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


def find_nearest_config_path(start: Path) -> Optional[Path]:
    """Return the closest .codex-autorunner/config.yml walking upward from start."""
    start = start.resolve()
    search_dir = start if start.is_dir() else start.parent
    for current in [search_dir] + list(search_dir.parents):
        candidate = current / CONFIG_FILENAME
        if candidate.exists():
            return candidate
    return None


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
    except Exception:
        # Never fail config loading due to dotenv issues.
        pass


def load_config_data(config_path: Path) -> Dict[str, Any]:
    """Load, merge, and return a raw config dict for the given config path."""
    load_dotenv_for_root(config_path.parent.parent.resolve())
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    except Exception as exc:
        raise ConfigError(f"Failed to read config file {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file must be a mapping: {config_path}")
    mode = data.get("mode", "repo")
    root = config_path.parent.parent.resolve()
    return resolve_config_data(root, mode, data)


def load_config(start: Path) -> Union[RepoConfig, HubConfig]:
    """
    Load the nearest config walking upward from the provided path.
    Returns a RepoConfig or HubConfig depending on the mode.
    """
    config_path = find_nearest_config_path(start)
    if not config_path:
        raise ConfigError(
            f"Missing config file; expected to find {CONFIG_FILENAME} in {start} or parents"
        )
    merged = load_config_data(config_path)
    mode = merged.get("mode", "repo")
    if mode == "hub":
        _validate_hub_config(merged)
        return _build_hub_config(config_path, merged)
    if mode == "repo":
        root = config_path.parent.parent.resolve()
        _validate_repo_config(merged, root=root)
        return _build_repo_config(config_path, merged)
    raise ConfigError(f"Invalid mode '{mode}'; expected 'hub' or 'repo'")


def _build_repo_config(config_path: Path, cfg: Dict[str, Any]) -> RepoConfig:
    root = config_path.parent.parent.resolve()
    docs = {
        "todo": Path(cfg["docs"]["todo"]),
        "progress": Path(cfg["docs"]["progress"]),
        "opinions": Path(cfg["docs"]["opinions"]),
        "spec": Path(cfg["docs"]["spec"]),
        "summary": Path(cfg["docs"]["summary"]),
        "snapshot": Path(cfg["docs"].get("snapshot", ".codex-autorunner/SNAPSHOT.md")),
        "snapshot_state": Path(
            cfg["docs"].get("snapshot_state", ".codex-autorunner/snapshot_state.json")
        ),
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
    log_cfg = cfg.get("log", {})
    log_cfg = cast(Dict[str, Any], log_cfg if isinstance(log_cfg, dict) else {})
    server_log_cfg = cfg.get("server_log", {}) or {}
    server_log_cfg = cast(
        Dict[str, Any], server_log_cfg if isinstance(server_log_cfg, dict) else {}
    )
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
        prompt_prev_run_max_chars=int(cfg["prompt"]["prev_run_max_chars"]),
        prompt_template=template,
        runner_sleep_seconds=int(cfg["runner"]["sleep_seconds"]),
        runner_stop_after_runs=cfg["runner"].get("stop_after_runs"),
        runner_max_wallclock_seconds=cfg["runner"].get("max_wallclock_seconds"),
        git_auto_commit=bool(cfg["git"].get("auto_commit", False)),
        git_commit_message_template=str(cfg["git"].get("commit_message_template")),
        app_server=_parse_app_server_config(
            cfg.get("app_server"),
            root,
            DEFAULT_REPO_CONFIG["app_server"],
        ),
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
    return HubConfig(
        raw=cfg,
        root=root,
        version=int(cfg["version"]),
        mode="hub",
        repos_root=(root / hub_cfg["repos_root"]).resolve(),
        worktrees_root=(root / hub_cfg["worktrees_root"]).resolve(),
        manifest_path=root / hub_cfg["manifest"],
        discover_depth=int(hub_cfg["discover_depth"]),
        auto_init_missing=bool(hub_cfg["auto_init_missing"]),
        repo_server_inherit=bool(hub_cfg.get("repo_server_inherit", True)),
        update_repo_url=str(hub_cfg.get("update_repo_url", "")),
        update_repo_ref=str(hub_cfg.get("update_repo_ref", "main")),
        app_server=_parse_app_server_config(
            cfg.get("app_server"),
            root,
            DEFAULT_HUB_CONFIG["app_server"],
        ),
        server_host=str(cfg["server"]["host"]),
        server_port=int(cfg["server"]["port"]),
        server_base_path=_normalize_base_path(cfg["server"].get("base_path", "")),
        server_access_log=bool(cfg["server"].get("access_log", False)),
        server_auth_token_env=str(cfg["server"].get("auth_token_env", "")),
        server_allowed_hosts=list(cfg["server"].get("allowed_hosts") or []),
        server_allowed_origins=list(cfg["server"].get("allowed_origins") or []),
        log=LogConfig(
            path=root / log_cfg["path"],
            max_bytes=int(log_cfg["max_bytes"]),
            backup_count=int(log_cfg["backup_count"]),
        ),
        server_log=LogConfig(
            path=root / str(server_log_cfg.get("path", log_cfg["path"])),
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


def _validate_version(cfg: Dict[str, Any]) -> None:
    if cfg.get("version") != CONFIG_VERSION:
        raise ConfigError(f"Unsupported config version; expected {CONFIG_VERSION}")


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_server_security(server: Dict[str, Any]) -> None:
    allowed_hosts = server.get("allowed_hosts")
    if allowed_hosts is not None and not isinstance(allowed_hosts, list):
        raise ConfigError("server.allowed_hosts must be a list of strings if provided")
    if isinstance(allowed_hosts, list):
        for entry in allowed_hosts:
            if not isinstance(entry, str):
                raise ConfigError("server.allowed_hosts must be a list of strings")

    allowed_origins = server.get("allowed_origins")
    if allowed_origins is not None and not isinstance(allowed_origins, list):
        raise ConfigError(
            "server.allowed_origins must be a list of strings if provided"
        )
    if isinstance(allowed_origins, list):
        for entry in allowed_origins:
            if not isinstance(entry, str):
                raise ConfigError("server.allowed_origins must be a list of strings")

    host = str(server.get("host", ""))
    if not _is_loopback_host(host) and not allowed_hosts:
        raise ConfigError(
            "server.allowed_hosts must be set when binding to a non-loopback host"
        )


def _validate_app_server_config(cfg: Dict[str, Any]) -> None:
    app_server_cfg = cfg.get("app_server")
    if app_server_cfg is None:
        return
    if not isinstance(app_server_cfg, dict):
        raise ConfigError("app_server section must be a mapping if provided")
    command = app_server_cfg.get("command")
    if command is not None and not isinstance(command, (list, str)):
        raise ConfigError("app_server.command must be a list or string if provided")
    if "state_root" in app_server_cfg and not isinstance(
        app_server_cfg.get("state_root", ""), str
    ):
        raise ConfigError("app_server.state_root must be a string path")
    for key in ("max_handles", "idle_ttl_seconds"):
        if key in app_server_cfg and app_server_cfg.get(key) is not None:
            if not isinstance(app_server_cfg.get(key), int):
                raise ConfigError(f"app_server.{key} must be an integer or null")
    if (
        "turn_timeout_seconds" in app_server_cfg
        and app_server_cfg.get("turn_timeout_seconds") is not None
    ):
        if not isinstance(app_server_cfg.get("turn_timeout_seconds"), (int, float)):
            raise ConfigError(
                "app_server.turn_timeout_seconds must be a number or null"
            )
    if (
        "request_timeout" in app_server_cfg
        and app_server_cfg.get("request_timeout") is not None
    ):
        if not isinstance(app_server_cfg.get("request_timeout"), (int, float)):
            raise ConfigError("app_server.request_timeout must be a number or null")
    prompts = app_server_cfg.get("prompts")
    if prompts is not None:
        if not isinstance(prompts, dict):
            raise ConfigError("app_server.prompts must be a mapping if provided")
        expected = {
            "doc_chat": {
                "max_chars": 1,
                "message_max_chars": 1,
                "target_excerpt_max_chars": 0,
                "recent_summary_max_chars": 0,
            },
            "spec_ingest": {
                "max_chars": 1,
                "message_max_chars": 1,
                "spec_excerpt_max_chars": 0,
            },
            "autorunner": {
                "max_chars": 1,
                "message_max_chars": 1,
                "todo_excerpt_max_chars": 0,
                "prev_run_max_chars": 0,
            },
        }
        for section, keys in expected.items():
            section_cfg = prompts.get(section)
            if section_cfg is None:
                continue
            if not isinstance(section_cfg, dict):
                raise ConfigError(f"app_server.prompts.{section} must be a mapping")
            for key, min_value in keys.items():
                if key not in section_cfg:
                    continue
                value = section_cfg.get(key)
                if value is None or not isinstance(value, int):
                    raise ConfigError(
                        f"app_server.prompts.{section}.{key} must be an integer"
                    )
                if value < min_value:
                    raise ConfigError(
                        f"app_server.prompts.{section}.{key} must be >= {min_value}"
                    )


def _validate_repo_config(cfg: Dict[str, Any], *, root: Path) -> None:
    _validate_version(cfg)
    if cfg.get("mode") != "repo":
        raise ConfigError("Repo config must set mode: repo")
    docs = cfg.get("docs")
    if not isinstance(docs, dict):
        raise ConfigError("docs must be a mapping")
    for key, value in docs.items():
        if not isinstance(value, str) or not value:
            raise ConfigError(f"docs.{key} must be a non-empty string path")
        path = Path(value)
        if path.is_absolute():
            raise ConfigError(f"docs.{key} must be a relative path under repo root")
        if ".." in path.parts:
            raise ConfigError(f"docs.{key} must not contain '..' segments")
        try:
            (root / path).resolve().relative_to(root)
        except ValueError as exc:
            raise ConfigError(
                f"docs.{key} must resolve under repo root ({root})"
            ) from exc
    for key in ("todo", "progress", "opinions", "spec", "summary"):
        if not isinstance(docs.get(key), str) or not docs[key]:
            raise ConfigError(f"docs.{key} must be a non-empty string path")
    codex = cfg.get("codex")
    if not isinstance(codex, dict):
        raise ConfigError("codex section must be a mapping")
    if not codex.get("binary"):
        raise ConfigError("codex.binary is required")
    if not isinstance(codex.get("args", []), list):
        raise ConfigError("codex.args must be a list")
    if "terminal_args" in codex and not isinstance(
        codex.get("terminal_args", []), list
    ):
        raise ConfigError("codex.terminal_args must be a list if provided")
    if (
        "model" in codex
        and codex.get("model") is not None
        and not isinstance(codex.get("model"), str)
    ):
        raise ConfigError("codex.model must be a string or null if provided")
    if (
        "reasoning" in codex
        and codex.get("reasoning") is not None
        and not isinstance(codex.get("reasoning"), str)
    ):
        raise ConfigError("codex.reasoning must be a string or null if provided")
    if "models" in codex:
        models = codex.get("models")
        if models is not None and not isinstance(models, dict):
            raise ConfigError("codex.models must be a mapping or null if provided")
        if isinstance(models, dict):
            for key in ("small", "large"):
                if (
                    key in models
                    and models.get(key) is not None
                    and not isinstance(models.get(key), str)
                ):
                    raise ConfigError(f"codex.models.{key} must be a string or null")
    prompt = cfg.get("prompt")
    if not isinstance(prompt, dict):
        raise ConfigError("prompt section must be a mapping")
    if not isinstance(prompt.get("prev_run_max_chars", 0), int):
        raise ConfigError("prompt.prev_run_max_chars must be an integer")
    runner = cfg.get("runner")
    if not isinstance(runner, dict):
        raise ConfigError("runner section must be a mapping")
    if not isinstance(runner.get("sleep_seconds", 0), int):
        raise ConfigError("runner.sleep_seconds must be an integer")
    for k in ("stop_after_runs", "max_wallclock_seconds"):
        val = runner.get(k)
        if val is not None and not isinstance(val, int):
            raise ConfigError(f"runner.{k} must be an integer or null")
    git = cfg.get("git")
    if not isinstance(git, dict):
        raise ConfigError("git section must be a mapping")
    if not isinstance(git.get("auto_commit", False), bool):
        raise ConfigError("git.auto_commit must be boolean")
    github = cfg.get("github", {})
    if github is not None and not isinstance(github, dict):
        raise ConfigError("github section must be a mapping if provided")
    if isinstance(github, dict):
        if "enabled" in github and not isinstance(github.get("enabled"), bool):
            raise ConfigError("github.enabled must be boolean")
        if "pr_draft_default" in github and not isinstance(
            github.get("pr_draft_default"), bool
        ):
            raise ConfigError("github.pr_draft_default must be boolean")
        if "sync_commit_mode" in github and not isinstance(
            github.get("sync_commit_mode"), str
        ):
            raise ConfigError("github.sync_commit_mode must be a string")
        if "sync_agent_timeout_seconds" in github and not isinstance(
            github.get("sync_agent_timeout_seconds"), int
        ):
            raise ConfigError("github.sync_agent_timeout_seconds must be an integer")
    server = cfg.get("server")
    if not isinstance(server, dict):
        raise ConfigError("server section must be a mapping")
    if not isinstance(server.get("host", ""), str):
        raise ConfigError("server.host must be a string")
    if not isinstance(server.get("port", 0), int):
        raise ConfigError("server.port must be an integer")
    if "base_path" in server and not isinstance(server.get("base_path", ""), str):
        raise ConfigError("server.base_path must be a string if provided")
    if "access_log" in server and not isinstance(server.get("access_log", False), bool):
        raise ConfigError("server.access_log must be boolean if provided")
    if "auth_token_env" in server and not isinstance(
        server.get("auth_token_env", ""), str
    ):
        raise ConfigError("server.auth_token_env must be a string if provided")
    _validate_server_security(server)
    _validate_app_server_config(cfg)
    notifications_cfg = cfg.get("notifications")
    if notifications_cfg is not None:
        if not isinstance(notifications_cfg, dict):
            raise ConfigError("notifications section must be a mapping if provided")
        if "enabled" in notifications_cfg:
            enabled_val = notifications_cfg.get("enabled")
            if not (
                isinstance(enabled_val, bool)
                or enabled_val is None
                or (isinstance(enabled_val, str) and enabled_val.lower() == "auto")
            ):
                raise ConfigError(
                    "notifications.enabled must be boolean, null, or 'auto'"
                )
        events = notifications_cfg.get("events")
        if events is not None and not isinstance(events, list):
            raise ConfigError("notifications.events must be a list if provided")
        if isinstance(events, list):
            for entry in events:
                if not isinstance(entry, str):
                    raise ConfigError("notifications.events must be a list of strings")
        tui_idle_seconds = notifications_cfg.get("tui_idle_seconds")
        if tui_idle_seconds is not None:
            if not isinstance(tui_idle_seconds, (int, float)):
                raise ConfigError(
                    "notifications.tui_idle_seconds must be a number if provided"
                )
            if tui_idle_seconds < 0:
                raise ConfigError(
                    "notifications.tui_idle_seconds must be >= 0 if provided"
                )
        discord_cfg = notifications_cfg.get("discord")
        if discord_cfg is not None and not isinstance(discord_cfg, dict):
            raise ConfigError("notifications.discord must be a mapping if provided")
        if isinstance(discord_cfg, dict):
            if "enabled" in discord_cfg and not isinstance(
                discord_cfg.get("enabled"), bool
            ):
                raise ConfigError("notifications.discord.enabled must be boolean")
            if "webhook_url_env" in discord_cfg and not isinstance(
                discord_cfg.get("webhook_url_env"), str
            ):
                raise ConfigError(
                    "notifications.discord.webhook_url_env must be a string"
                )
        telegram_cfg = notifications_cfg.get("telegram")
        if telegram_cfg is not None and not isinstance(telegram_cfg, dict):
            raise ConfigError("notifications.telegram must be a mapping if provided")
        if isinstance(telegram_cfg, dict):
            if "enabled" in telegram_cfg and not isinstance(
                telegram_cfg.get("enabled"), bool
            ):
                raise ConfigError("notifications.telegram.enabled must be boolean")
            if "bot_token_env" in telegram_cfg and not isinstance(
                telegram_cfg.get("bot_token_env"), str
            ):
                raise ConfigError(
                    "notifications.telegram.bot_token_env must be a string"
                )
            if "chat_id_env" in telegram_cfg and not isinstance(
                telegram_cfg.get("chat_id_env"), str
            ):
                raise ConfigError("notifications.telegram.chat_id_env must be a string")
            if "thread_id_env" in telegram_cfg and not isinstance(
                telegram_cfg.get("thread_id_env"), str
            ):
                raise ConfigError(
                    "notifications.telegram.thread_id_env must be a string"
                )
            if "thread_id" in telegram_cfg:
                thread_id = telegram_cfg.get("thread_id")
                if thread_id is not None and not isinstance(thread_id, int):
                    raise ConfigError(
                        "notifications.telegram.thread_id must be an integer or null"
                    )
            if "thread_id_map" in telegram_cfg:
                thread_id_map = telegram_cfg.get("thread_id_map")
                if not isinstance(thread_id_map, dict):
                    raise ConfigError(
                        "notifications.telegram.thread_id_map must be a mapping"
                    )
                for key, value in thread_id_map.items():
                    if not isinstance(key, str) or not isinstance(value, int):
                        raise ConfigError(
                            "notifications.telegram.thread_id_map must map strings to integers"
                        )
    terminal_cfg = cfg.get("terminal")
    if terminal_cfg is not None:
        if not isinstance(terminal_cfg, dict):
            raise ConfigError("terminal section must be a mapping if provided")
        idle_timeout_seconds = terminal_cfg.get("idle_timeout_seconds")
        if idle_timeout_seconds is not None and not isinstance(
            idle_timeout_seconds, int
        ):
            raise ConfigError(
                "terminal.idle_timeout_seconds must be an integer or null"
            )
        if isinstance(idle_timeout_seconds, int) and idle_timeout_seconds < 0:
            raise ConfigError("terminal.idle_timeout_seconds must be >= 0")
    log_cfg = cfg.get("log")
    if not isinstance(log_cfg, dict):
        raise ConfigError("log section must be a mapping")
    for key in ("path",):
        if not isinstance(log_cfg.get(key, ""), str):
            raise ConfigError(f"log.{key} must be a string path")
    for key in ("max_bytes", "backup_count"):
        if not isinstance(log_cfg.get(key, 0), int):
            raise ConfigError(f"log.{key} must be an integer")
    server_log_cfg = cfg.get("server_log", {})
    if server_log_cfg is not None and not isinstance(server_log_cfg, dict):
        raise ConfigError("server_log section must be a mapping or null")
    if isinstance(server_log_cfg, dict):
        if "path" in server_log_cfg and not isinstance(
            server_log_cfg.get("path", ""), str
        ):
            raise ConfigError("server_log.path must be a string path")
        for key in ("max_bytes", "backup_count"):
            if key in server_log_cfg and not isinstance(server_log_cfg.get(key), int):
                raise ConfigError(f"server_log.{key} must be an integer")
    voice_cfg = cfg.get("voice", {})
    if voice_cfg is not None and not isinstance(voice_cfg, dict):
        raise ConfigError("voice section must be a mapping if provided")
    _validate_static_assets_config(cfg, scope="repo")
    _validate_housekeeping_config(cfg)
    _validate_telegram_bot_config(cfg)


def _validate_hub_config(cfg: Dict[str, Any]) -> None:
    _validate_version(cfg)
    if cfg.get("mode") != "hub":
        raise ConfigError("Hub config must set mode: hub")
    hub_cfg = cfg.get("hub")
    if not isinstance(hub_cfg, dict):
        raise ConfigError("hub section must be a mapping")
    if not isinstance(hub_cfg.get("repos_root", ""), str):
        raise ConfigError("hub.repos_root must be a string path")
    if not isinstance(hub_cfg.get("worktrees_root", ""), str):
        raise ConfigError("hub.worktrees_root must be a string path")
    if not isinstance(hub_cfg.get("manifest", ""), str):
        raise ConfigError("hub.manifest must be a string path")
    if hub_cfg.get("discover_depth") not in (None, 1):
        raise ConfigError("hub.discover_depth is fixed to 1 for now")
    if not isinstance(hub_cfg.get("auto_init_missing", True), bool):
        raise ConfigError("hub.auto_init_missing must be boolean")
    if "repo_server_inherit" in hub_cfg and not isinstance(
        hub_cfg.get("repo_server_inherit"), bool
    ):
        raise ConfigError("hub.repo_server_inherit must be boolean")
    if "update_repo_url" in hub_cfg and not isinstance(
        hub_cfg.get("update_repo_url"), str
    ):
        raise ConfigError("hub.update_repo_url must be a string")
    if "update_repo_ref" in hub_cfg and not isinstance(
        hub_cfg.get("update_repo_ref"), str
    ):
        raise ConfigError("hub.update_repo_ref must be a string")
    log_cfg = hub_cfg.get("log")
    if not isinstance(log_cfg, dict):
        raise ConfigError("hub.log section must be a mapping")
    for key in ("path",):
        if not isinstance(log_cfg.get(key, ""), str):
            raise ConfigError(f"hub.log.{key} must be a string path")
    for key in ("max_bytes", "backup_count"):
        if not isinstance(log_cfg.get(key, 0), int):
            raise ConfigError(f"hub.log.{key} must be an integer")
    server = cfg.get("server")
    if not isinstance(server, dict):
        raise ConfigError("server section must be a mapping")
    if not isinstance(server.get("host", ""), str):
        raise ConfigError("server.host must be a string")
    if not isinstance(server.get("port", 0), int):
        raise ConfigError("server.port must be an integer")
    if "base_path" in server and not isinstance(server.get("base_path", ""), str):
        raise ConfigError("server.base_path must be a string if provided")
    if "access_log" in server and not isinstance(server.get("access_log", False), bool):
        raise ConfigError("server.access_log must be boolean if provided")
    if "auth_token_env" in server and not isinstance(
        server.get("auth_token_env", ""), str
    ):
        raise ConfigError("server.auth_token_env must be a string if provided")
    _validate_server_security(server)
    _validate_app_server_config(cfg)
    server_log_cfg = cfg.get("server_log")
    if server_log_cfg is not None and not isinstance(server_log_cfg, dict):
        raise ConfigError("server_log section must be a mapping or null")
    if isinstance(server_log_cfg, dict):
        if "path" in server_log_cfg and not isinstance(
            server_log_cfg.get("path", ""), str
        ):
            raise ConfigError("server_log.path must be a string path")
        for key in ("max_bytes", "backup_count"):
            if key in server_log_cfg and not isinstance(server_log_cfg.get(key), int):
                raise ConfigError(f"server_log.{key} must be an integer")
    _validate_static_assets_config(cfg, scope="hub")
    _validate_housekeeping_config(cfg)
    _validate_telegram_bot_config(cfg)


def _validate_housekeeping_config(cfg: Dict[str, Any]) -> None:
    housekeeping_cfg = cfg.get("housekeeping")
    if housekeeping_cfg is None:
        return
    if not isinstance(housekeeping_cfg, dict):
        raise ConfigError("housekeeping section must be a mapping if provided")
    if "enabled" in housekeeping_cfg and not isinstance(
        housekeeping_cfg.get("enabled"), bool
    ):
        raise ConfigError("housekeeping.enabled must be boolean")
    if "interval_seconds" in housekeeping_cfg and not isinstance(
        housekeeping_cfg.get("interval_seconds"), int
    ):
        raise ConfigError("housekeeping.interval_seconds must be an integer")
    interval_seconds = housekeeping_cfg.get("interval_seconds")
    if isinstance(interval_seconds, int) and interval_seconds <= 0:
        raise ConfigError("housekeeping.interval_seconds must be greater than 0")
    if "min_file_age_seconds" in housekeeping_cfg and not isinstance(
        housekeeping_cfg.get("min_file_age_seconds"), int
    ):
        raise ConfigError("housekeeping.min_file_age_seconds must be an integer")
    min_file_age_seconds = housekeeping_cfg.get("min_file_age_seconds")
    if isinstance(min_file_age_seconds, int) and min_file_age_seconds < 0:
        raise ConfigError("housekeeping.min_file_age_seconds must be >= 0")
    if "dry_run" in housekeeping_cfg and not isinstance(
        housekeeping_cfg.get("dry_run"), bool
    ):
        raise ConfigError("housekeeping.dry_run must be boolean")
    rules = housekeeping_cfg.get("rules")
    if rules is not None and not isinstance(rules, list):
        raise ConfigError("housekeeping.rules must be a list if provided")
    if isinstance(rules, list):
        for idx, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise ConfigError(
                    f"housekeeping.rules[{idx}] must be a mapping if provided"
                )
            if "name" in rule and not isinstance(rule.get("name"), str):
                raise ConfigError(
                    f"housekeeping.rules[{idx}].name must be a string if provided"
                )
            if "kind" in rule:
                kind = rule.get("kind")
                if not isinstance(kind, str):
                    raise ConfigError(
                        f"housekeeping.rules[{idx}].kind must be a string"
                    )
                if kind not in ("directory", "file"):
                    raise ConfigError(
                        f"housekeeping.rules[{idx}].kind must be 'directory' or 'file'"
                    )
            if "path" in rule and not isinstance(rule.get("path"), str):
                raise ConfigError(f"housekeeping.rules[{idx}].path must be a string")
            if "glob" in rule and not isinstance(rule.get("glob"), str):
                raise ConfigError(
                    f"housekeeping.rules[{idx}].glob must be a string if provided"
                )
            if "recursive" in rule and not isinstance(rule.get("recursive"), bool):
                raise ConfigError(
                    f"housekeeping.rules[{idx}].recursive must be boolean if provided"
                )
            for key in (
                "max_files",
                "max_total_bytes",
                "max_age_days",
                "max_bytes",
                "max_lines",
            ):
                if key in rule and not isinstance(rule.get(key), int):
                    raise ConfigError(
                        f"housekeeping.rules[{idx}].{key} must be an integer if provided"
                    )
                value = rule.get(key)
                if isinstance(value, int) and value < 0:
                    raise ConfigError(f"housekeeping.rules[{idx}].{key} must be >= 0")


def _validate_static_assets_config(cfg: Dict[str, Any], scope: str) -> None:
    static_cfg = cfg.get("static_assets")
    if static_cfg is None:
        return
    if not isinstance(static_cfg, dict):
        raise ConfigError(f"{scope}.static_assets must be a mapping if provided")
    cache_root = static_cfg.get("cache_root")
    if cache_root is not None and not isinstance(cache_root, str):
        raise ConfigError(f"{scope}.static_assets.cache_root must be a string")
    max_entries = static_cfg.get("max_cache_entries")
    if max_entries is not None and not isinstance(max_entries, int):
        raise ConfigError(f"{scope}.static_assets.max_cache_entries must be an integer")
    if isinstance(max_entries, int) and max_entries < 0:
        raise ConfigError(f"{scope}.static_assets.max_cache_entries must be >= 0")
    max_age_days = static_cfg.get("max_cache_age_days")
    if max_age_days is not None and not isinstance(max_age_days, int):
        raise ConfigError(
            f"{scope}.static_assets.max_cache_age_days must be an integer or null"
        )
    if isinstance(max_age_days, int) and max_age_days < 0:
        raise ConfigError(f"{scope}.static_assets.max_cache_age_days must be >= 0")


def _validate_telegram_bot_config(cfg: Dict[str, Any]) -> None:
    telegram_cfg = cfg.get("telegram_bot")
    if telegram_cfg is None:
        return
    if not isinstance(telegram_cfg, dict):
        raise ConfigError("telegram_bot section must be a mapping if provided")
    if "enabled" in telegram_cfg and not isinstance(telegram_cfg.get("enabled"), bool):
        raise ConfigError("telegram_bot.enabled must be boolean")
    if "mode" in telegram_cfg and not isinstance(telegram_cfg.get("mode"), str):
        raise ConfigError("telegram_bot.mode must be a string")
    if "parse_mode" in telegram_cfg:
        parse_mode = telegram_cfg.get("parse_mode")
        if parse_mode is not None and not isinstance(parse_mode, str):
            raise ConfigError("telegram_bot.parse_mode must be a string or null")
        if isinstance(parse_mode, str):
            normalized = parse_mode.strip().lower()
            if normalized and normalized not in ("html", "markdown", "markdownv2"):
                raise ConfigError(
                    "telegram_bot.parse_mode must be HTML, Markdown, MarkdownV2, or null"
                )
    debug_cfg = telegram_cfg.get("debug")
    if debug_cfg is not None and not isinstance(debug_cfg, dict):
        raise ConfigError("telegram_bot.debug must be a mapping if provided")
    if isinstance(debug_cfg, dict):
        if "prefix_context" in debug_cfg and not isinstance(
            debug_cfg.get("prefix_context"), bool
        ):
            raise ConfigError("telegram_bot.debug.prefix_context must be boolean")
    for key in ("bot_token_env", "chat_id_env", "app_server_command_env"):
        if key in telegram_cfg and not isinstance(telegram_cfg.get(key), str):
            raise ConfigError(f"telegram_bot.{key} must be a string")
    for key in ("allowed_chat_ids", "allowed_user_ids"):
        if key in telegram_cfg and not isinstance(telegram_cfg.get(key), list):
            raise ConfigError(f"telegram_bot.{key} must be a list")
    if "require_topics" in telegram_cfg and not isinstance(
        telegram_cfg.get("require_topics"), bool
    ):
        raise ConfigError("telegram_bot.require_topics must be boolean")
    defaults_cfg = telegram_cfg.get("defaults")
    if defaults_cfg is not None and not isinstance(defaults_cfg, dict):
        raise ConfigError("telegram_bot.defaults must be a mapping if provided")
    if isinstance(defaults_cfg, dict):
        if "approval_mode" in defaults_cfg and not isinstance(
            defaults_cfg.get("approval_mode"), str
        ):
            raise ConfigError("telegram_bot.defaults.approval_mode must be a string")
        for key in (
            "approval_policy",
            "sandbox_policy",
            "yolo_approval_policy",
            "yolo_sandbox_policy",
        ):
            if (
                key in defaults_cfg
                and defaults_cfg.get(key) is not None
                and not isinstance(defaults_cfg.get(key), str)
            ):
                raise ConfigError(
                    f"telegram_bot.defaults.{key} must be a string or null"
                )
    concurrency_cfg = telegram_cfg.get("concurrency")
    if concurrency_cfg is not None and not isinstance(concurrency_cfg, dict):
        raise ConfigError("telegram_bot.concurrency must be a mapping if provided")
    if isinstance(concurrency_cfg, dict):
        if "max_parallel_turns" in concurrency_cfg and not isinstance(
            concurrency_cfg.get("max_parallel_turns"), int
        ):
            raise ConfigError(
                "telegram_bot.concurrency.max_parallel_turns must be an integer"
            )
        if "per_topic_queue" in concurrency_cfg and not isinstance(
            concurrency_cfg.get("per_topic_queue"), bool
        ):
            raise ConfigError(
                "telegram_bot.concurrency.per_topic_queue must be boolean"
            )
    media_cfg = telegram_cfg.get("media")
    if media_cfg is not None and not isinstance(media_cfg, dict):
        raise ConfigError("telegram_bot.media must be a mapping if provided")
    if isinstance(media_cfg, dict):
        if "enabled" in media_cfg and not isinstance(media_cfg.get("enabled"), bool):
            raise ConfigError("telegram_bot.media.enabled must be boolean")
        if "images" in media_cfg and not isinstance(media_cfg.get("images"), bool):
            raise ConfigError("telegram_bot.media.images must be boolean")
        if "voice" in media_cfg and not isinstance(media_cfg.get("voice"), bool):
            raise ConfigError("telegram_bot.media.voice must be boolean")
        if "files" in media_cfg and not isinstance(media_cfg.get("files"), bool):
            raise ConfigError("telegram_bot.media.files must be boolean")
        for key in ("max_image_bytes", "max_voice_bytes", "max_file_bytes"):
            value = media_cfg.get(key)
            if value is not None and not isinstance(value, int):
                raise ConfigError(f"telegram_bot.media.{key} must be an integer")
            if isinstance(value, int) and value <= 0:
                raise ConfigError(f"telegram_bot.media.{key} must be greater than 0")
        if "image_prompt" in media_cfg and not isinstance(
            media_cfg.get("image_prompt"), str
        ):
            raise ConfigError("telegram_bot.media.image_prompt must be a string")
    shell_cfg = telegram_cfg.get("shell")
    if shell_cfg is not None and not isinstance(shell_cfg, dict):
        raise ConfigError("telegram_bot.shell must be a mapping if provided")
    if isinstance(shell_cfg, dict):
        if "enabled" in shell_cfg and not isinstance(shell_cfg.get("enabled"), bool):
            raise ConfigError("telegram_bot.shell.enabled must be boolean")
        for key in ("timeout_ms", "max_output_chars"):
            value = shell_cfg.get(key)
            if value is not None and not isinstance(value, int):
                raise ConfigError(f"telegram_bot.shell.{key} must be an integer")
            if isinstance(value, int) and value <= 0:
                raise ConfigError(f"telegram_bot.shell.{key} must be greater than 0")
    command_reg_cfg = telegram_cfg.get("command_registration")
    if command_reg_cfg is not None and not isinstance(command_reg_cfg, dict):
        raise ConfigError("telegram_bot.command_registration must be a mapping")
    if isinstance(command_reg_cfg, dict):
        if "enabled" in command_reg_cfg and not isinstance(
            command_reg_cfg.get("enabled"), bool
        ):
            raise ConfigError(
                "telegram_bot.command_registration.enabled must be boolean"
            )
        if "scopes" in command_reg_cfg:
            scopes = command_reg_cfg.get("scopes")
            if not isinstance(scopes, list):
                raise ConfigError(
                    "telegram_bot.command_registration.scopes must be a list"
                )
            for scope in scopes:
                if isinstance(scope, str):
                    continue
                if not isinstance(scope, dict):
                    raise ConfigError(
                        "telegram_bot.command_registration.scopes must contain strings or mappings"
                    )
                scope_payload = scope.get("scope")
                if scope_payload is not None and not isinstance(scope_payload, dict):
                    raise ConfigError(
                        "telegram_bot.command_registration.scopes.scope must be a mapping"
                    )
                if "type" in scope and not isinstance(scope.get("type"), str):
                    raise ConfigError(
                        "telegram_bot.command_registration.scopes.type must be a string"
                    )
                language_code = scope.get("language_code")
                if language_code is not None and not isinstance(language_code, str):
                    raise ConfigError(
                        "telegram_bot.command_registration.scopes.language_code must be a string or null"
                    )
    if "state_file" in telegram_cfg and not isinstance(
        telegram_cfg.get("state_file"), str
    ):
        raise ConfigError("telegram_bot.state_file must be a string path")
    if "app_server_command" in telegram_cfg and not isinstance(
        telegram_cfg.get("app_server_command"), (list, str)
    ):
        raise ConfigError("telegram_bot.app_server_command must be a list or string")
    polling_cfg = telegram_cfg.get("polling")
    if polling_cfg is not None and not isinstance(polling_cfg, dict):
        raise ConfigError("telegram_bot.polling must be a mapping if provided")
    if isinstance(polling_cfg, dict):
        if "timeout_seconds" in polling_cfg and not isinstance(
            polling_cfg.get("timeout_seconds"), int
        ):
            raise ConfigError("telegram_bot.polling.timeout_seconds must be an integer")
        timeout_seconds = polling_cfg.get("timeout_seconds")
        if isinstance(timeout_seconds, int) and timeout_seconds <= 0:
            raise ConfigError(
                "telegram_bot.polling.timeout_seconds must be greater than 0"
            )
        if "allowed_updates" in polling_cfg and not isinstance(
            polling_cfg.get("allowed_updates"), list
        ):
            raise ConfigError("telegram_bot.polling.allowed_updates must be a list")
