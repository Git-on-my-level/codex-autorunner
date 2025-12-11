import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

CONFIG_FILENAME = ".codex-autorunner/config.yml"
CONFIG_VERSION = 2

DEFAULT_REPO_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "mode": "repo",
    "docs": {
        "todo": ".codex-autorunner/TODO.md",
        "progress": ".codex-autorunner/PROGRESS.md",
        "opinions": ".codex-autorunner/OPINIONS.md",
        "spec": ".codex-autorunner/SPEC.md",
    },
    "codex": {
        "binary": "codex",
        "args": ["--yolo", "exec", "--sandbox", "danger-full-access"],
        "terminal_args": [],
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
    "server": {
        "host": "127.0.0.1",
        "port": 4173,
        "base_path": "",
    },
    "log": {
        "path": ".codex-autorunner/codex-autorunner.log",
        "max_bytes": 10_000_000,
        "backup_count": 3,
    },
}

DEFAULT_HUB_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "mode": "hub",
    "hub": {
        "repos_root": ".",
        "manifest": ".codex-autorunner/manifest.yml",
        "discover_depth": 1,
        "auto_init_missing": True,
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
    log: LogConfig

    def doc_path(self, key: str) -> Path:
        return self.root / self.docs[key]


@dataclasses.dataclass
class HubConfig:
    raw: Dict[str, Any]
    root: Path
    version: int
    mode: str
    repos_root: Path
    manifest_path: Path
    discover_depth: int
    auto_init_missing: bool
    server_host: str
    server_port: int
    server_base_path: str
    log: LogConfig


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
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    mode = data.get("mode", "repo")
    if mode == "hub":
        merged = _merge_defaults(DEFAULT_HUB_CONFIG, data)
        _validate_hub_config(merged)
        return _build_hub_config(config_path, merged)
    if mode == "repo":
        merged = _merge_defaults(DEFAULT_REPO_CONFIG, data)
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
    }
    template_val = cfg["prompt"].get("template")
    template = root / template_val if template_val else None
    term_args = cfg["codex"].get("terminal_args") or []
    log_cfg = cfg.get("log", {})
    return RepoConfig(
        raw=cfg,
        root=root,
        version=int(cfg["version"]),
        mode="repo",
        docs=docs,
        codex_binary=cfg["codex"]["binary"],
        codex_args=list(cfg["codex"].get("args", [])),
        codex_terminal_args=list(term_args) if isinstance(term_args, list) else [],
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
        log=LogConfig(
            path=root / log_cfg.get("path", DEFAULT_REPO_CONFIG["log"]["path"]),
            max_bytes=int(log_cfg.get("max_bytes", DEFAULT_REPO_CONFIG["log"]["max_bytes"])),
            backup_count=int(
                log_cfg.get("backup_count", DEFAULT_REPO_CONFIG["log"]["backup_count"])
            ),
        ),
    )


def _build_hub_config(config_path: Path, cfg: Dict[str, Any]) -> HubConfig:
    root = config_path.parent.parent.resolve()
    hub_cfg = cfg["hub"]
    log_cfg = hub_cfg["log"]
    return HubConfig(
        raw=cfg,
        root=root,
        version=int(cfg["version"]),
        mode="hub",
        repos_root=(root / hub_cfg["repos_root"]).resolve(),
        manifest_path=root / hub_cfg["manifest"],
        discover_depth=int(hub_cfg["discover_depth"]),
        auto_init_missing=bool(hub_cfg["auto_init_missing"]),
        server_host=str(cfg["server"]["host"]),
        server_port=int(cfg["server"]["port"]),
        server_base_path=_normalize_base_path(cfg["server"].get("base_path", "")),
        log=LogConfig(
            path=root / log_cfg["path"],
            max_bytes=int(log_cfg["max_bytes"]),
            backup_count=int(log_cfg["backup_count"]),
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
    for key in ("todo", "progress", "opinions", "spec"):
        if not isinstance(docs.get(key), str) or not docs[key]:
            raise ConfigError(f"docs.{key} must be a non-empty string path")
    codex = cfg.get("codex")
    if not isinstance(codex, dict):
        raise ConfigError("codex section must be a mapping")
    if not codex.get("binary"):
        raise ConfigError("codex.binary is required")
    if not isinstance(codex.get("args", []), list):
        raise ConfigError("codex.args must be a list")
    if "terminal_args" in codex and not isinstance(codex.get("terminal_args", []), list):
        raise ConfigError("codex.terminal_args must be a list if provided")
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
    server = cfg.get("server")
    if not isinstance(server, dict):
        raise ConfigError("server section must be a mapping")
    if not isinstance(server.get("host", ""), str):
        raise ConfigError("server.host must be a string")
    if not isinstance(server.get("port", 0), int):
        raise ConfigError("server.port must be an integer")
    if "base_path" in server and not isinstance(server.get("base_path", ""), str):
        raise ConfigError("server.base_path must be a string if provided")
    log_cfg = cfg.get("log")
    if not isinstance(log_cfg, dict):
        raise ConfigError("log section must be a mapping")
    for key in ("path",):
        if not isinstance(log_cfg.get(key, ""), str):
            raise ConfigError(f"log.{key} must be a string path")
    for key in ("max_bytes", "backup_count"):
        if not isinstance(log_cfg.get(key, 0), int):
            raise ConfigError(f"log.{key} must be an integer")


def _validate_hub_config(cfg: Dict[str, Any]) -> None:
    _validate_version(cfg)
    if cfg.get("mode") != "hub":
        raise ConfigError("Hub config must set mode: hub")
    hub_cfg = cfg.get("hub")
    if not isinstance(hub_cfg, dict):
        raise ConfigError("hub section must be a mapping")
    if not isinstance(hub_cfg.get("repos_root", ""), str):
        raise ConfigError("hub.repos_root must be a string path")
    if not isinstance(hub_cfg.get("manifest", ""), str):
        raise ConfigError("hub.manifest must be a string path")
    if hub_cfg.get("discover_depth") not in (None, 1):
        raise ConfigError("hub.discover_depth is fixed to 1 for now")
    if not isinstance(hub_cfg.get("auto_init_missing", True), bool):
        raise ConfigError("hub.auto_init_missing must be boolean")
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
