import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover

    def load_dotenv(*_args, **_kwargs):  # type: ignore[no-redef]
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
    "server": {
        "host": "127.0.0.1",
        "port": 4173,
        "base_path": "",
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
        "require_topics": True,
        "defaults": {
            "approval_mode": "yolo",
            "approval_policy": "on-request",
            "sandbox_policy": "dangerFullAccess",
            "yolo_approval_policy": "never",
            "yolo_sandbox_policy": "dangerFullAccess",
        },
        "concurrency": {
            "max_parallel_turns": 2,
            "per_topic_queue": True,
        },
        "media": {
            "enabled": True,
            "images": True,
            "voice": True,
            "max_image_bytes": 10_000_000,
            "max_voice_bytes": 10_000_000,
            "image_prompt": "Describe the image.",
        },
        "shell": {
            "enabled": False,
            "timeout_ms": 120000,
            "max_output_chars": 3800,
        },
        "state_file": ".codex-autorunner/telegram_state.json",
        "app_server_command_env": "CAR_TELEGRAM_APP_SERVER_COMMAND",
        "app_server_command": ["codex", "app-server"],
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
        "require_topics": True,
        "defaults": {
            "approval_mode": "yolo",
            "approval_policy": "on-request",
            "sandbox_policy": "dangerFullAccess",
            "yolo_approval_policy": "never",
            "yolo_sandbox_policy": "dangerFullAccess",
        },
        "concurrency": {
            "max_parallel_turns": 2,
            "per_topic_queue": True,
        },
        "media": {
            "enabled": True,
            "images": True,
            "voice": True,
            "max_image_bytes": 10_000_000,
            "max_voice_bytes": 10_000_000,
            "image_prompt": "Describe the image.",
        },
        "shell": {
            "enabled": False,
            "timeout_ms": 120000,
            "max_output_chars": 3800,
        },
        "state_file": ".codex-autorunner/telegram_state.json",
        "app_server_command_env": "CAR_TELEGRAM_APP_SERVER_COMMAND",
        "app_server_command": ["codex", "app-server"],
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
        # Where to pull system updates from (defaults to main upstream)
        "update_repo_url": "https://github.com/Git-on-my-level/codex-autorunner.git",
        "update_repo_ref": "main",
        "log": {
            "path": ".codex-autorunner/codex-autorunner-hub.log",
            "max_bytes": 10_000_000,
            "backup_count": 3,
        },
    },
    "server": {
        "host": "127.0.0.1",
        "port": 4173,
        "base_path": "",
    },
    # Hub already has hub.log, but we still support an explicit server_log for consistency.
    "server_log": None,
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
    server_host: str
    server_port: int
    server_base_path: str
    notifications: Dict[str, Any]
    terminal_idle_timeout_seconds: Optional[int]
    log: LogConfig
    server_log: LogConfig
    voice: Dict[str, Any]

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
    update_repo_url: str
    update_repo_ref: str
    server_host: str
    server_port: int
    server_base_path: str
    log: LogConfig
    server_log: LogConfig


# Alias used by existing code paths that only support repo mode
Config = RepoConfig


def _merge_defaults(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    merged = json.loads(json.dumps(base))
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
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_root_config(root: Path) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    base = _load_yaml_dict(root / ROOT_CONFIG_FILENAME)
    if base:
        merged = _merge_defaults(merged, base)
    override = _load_yaml_dict(root / ROOT_OVERRIDE_FILENAME)
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


def find_nearest_config_path(start: Path) -> Optional[Path]:
    """Return the closest .codex-autorunner/config.yml walking upward from start."""
    start = start.resolve()
    search_dir = start if start.is_dir() else start.parent
    for current in [search_dir] + list(search_dir.parents):
        candidate = current / CONFIG_FILENAME
        if candidate.exists():
            return candidate
    return None


def _load_dotenv_for_config(config_path: Path) -> None:
    """
    Best-effort load of environment variables for this config root.

    We intentionally load from deterministic locations rather than relying on
    process CWD (which differs for installed entrypoints, launchd, etc.).
    """
    try:
        root = config_path.parent.parent.resolve()
        candidates = [
            root / ".env",
            config_path.parent / ".env",  # .codex-autorunner/.env
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
    _load_dotenv_for_config(config_path)
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError("Config file must be a mapping")
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
        _validate_repo_config(merged)
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
    template_val = cfg["prompt"].get("template")
    template = root / template_val if template_val else None
    term_args = cfg["codex"].get("terminal_args") or []
    terminal_cfg = cfg.get("terminal") if isinstance(cfg.get("terminal"), dict) else {}
    idle_timeout_seconds = terminal_cfg.get("idle_timeout_seconds")
    notifications_cfg = (
        cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    )
    if idle_timeout_seconds is not None:
        idle_timeout_seconds = int(idle_timeout_seconds)
        if idle_timeout_seconds <= 0:
            idle_timeout_seconds = None
    log_cfg = cfg.get("log", {})
    server_log_cfg = cfg.get("server_log", {}) or {}
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
        server_host=str(cfg["server"].get("host")),
        server_port=int(cfg["server"].get("port")),
        server_base_path=_normalize_base_path(cfg["server"].get("base_path", "")),
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
            / server_log_cfg.get(
                "path", DEFAULT_REPO_CONFIG["server_log"]["path"]  # type: ignore[index]
            ),
            max_bytes=int(
                server_log_cfg.get(
                    "max_bytes", DEFAULT_REPO_CONFIG["server_log"]["max_bytes"]  # type: ignore[index]
                )
            ),
            backup_count=int(
                server_log_cfg.get(
                    "backup_count",
                    DEFAULT_REPO_CONFIG["server_log"]["backup_count"],  # type: ignore[index]
                )
            ),
        ),
        voice=voice_cfg,
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
        update_repo_url=str(hub_cfg.get("update_repo_url", "")),
        update_repo_ref=str(hub_cfg.get("update_repo_ref", "main")),
        server_host=str(cfg["server"]["host"]),
        server_port=int(cfg["server"]["port"]),
        server_base_path=_normalize_base_path(cfg["server"].get("base_path", "")),
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
    )


def _validate_version(cfg: Dict[str, Any]) -> None:
    if cfg.get("version") != CONFIG_VERSION:
        raise ConfigError(f"Unsupported config version; expected {CONFIG_VERSION}")


def _validate_repo_config(cfg: Dict[str, Any]) -> None:
    _validate_version(cfg)
    if cfg.get("mode") != "repo":
        raise ConfigError("Repo config must set mode: repo")
    docs = cfg.get("docs")
    if not isinstance(docs, dict):
        raise ConfigError("docs must be a mapping")
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
    if "model" in codex and codex.get("model") is not None and not isinstance(
        codex.get("model"), str
    ):
        raise ConfigError("codex.model must be a string or null if provided")
    if "reasoning" in codex and codex.get("reasoning") is not None and not isinstance(
        codex.get("reasoning"), str
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
                raise ConfigError(
                    "notifications.telegram.chat_id_env must be a string"
                )
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
    _validate_telegram_bot_config(cfg)


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
        for key in ("approval_policy", "sandbox_policy", "yolo_approval_policy", "yolo_sandbox_policy"):
            if key in defaults_cfg and defaults_cfg.get(key) is not None and not isinstance(
                defaults_cfg.get(key), str
            ):
                raise ConfigError(f"telegram_bot.defaults.{key} must be a string or null")
    concurrency_cfg = telegram_cfg.get("concurrency")
    if concurrency_cfg is not None and not isinstance(concurrency_cfg, dict):
        raise ConfigError("telegram_bot.concurrency must be a mapping if provided")
    if isinstance(concurrency_cfg, dict):
        if "max_parallel_turns" in concurrency_cfg and not isinstance(
            concurrency_cfg.get("max_parallel_turns"), int
        ):
            raise ConfigError("telegram_bot.concurrency.max_parallel_turns must be an integer")
        if "per_topic_queue" in concurrency_cfg and not isinstance(
            concurrency_cfg.get("per_topic_queue"), bool
        ):
            raise ConfigError("telegram_bot.concurrency.per_topic_queue must be boolean")
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
        for key in ("max_image_bytes", "max_voice_bytes"):
            if key in media_cfg and not isinstance(media_cfg.get(key), int):
                raise ConfigError(f"telegram_bot.media.{key} must be an integer")
            if isinstance(media_cfg.get(key), int) and media_cfg.get(key) <= 0:
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
            if key in shell_cfg and not isinstance(shell_cfg.get(key), int):
                raise ConfigError(f"telegram_bot.shell.{key} must be an integer")
            if isinstance(shell_cfg.get(key), int) and shell_cfg.get(key) <= 0:
                raise ConfigError(f"telegram_bot.shell.{key} must be greater than 0")
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
        if (
            isinstance(polling_cfg.get("timeout_seconds"), int)
            and polling_cfg.get("timeout_seconds") <= 0
        ):
            raise ConfigError(
                "telegram_bot.polling.timeout_seconds must be greater than 0"
            )
        if "allowed_updates" in polling_cfg and not isinstance(
            polling_cfg.get("allowed_updates"), list
        ):
            raise ConfigError("telegram_bot.polling.allowed_updates must be a list")
