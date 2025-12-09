import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_CONFIG = {
    "version": 1,
    "docs": {
        "todo": ".codex-autorunner/TODO.md",
        "progress": ".codex-autorunner/PROGRESS.md",
        "opinions": ".codex-autorunner/OPINIONS.md",
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
    },
}


class ConfigError(Exception):
    """Raised when configuration is invalid."""


@dataclasses.dataclass
class Config:
    raw: Dict[str, Any]
    repo_root: Path
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

    def doc_path(self, key: str) -> Path:
        return self.repo_root / self.docs[key]


def _merge_defaults(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    merged = json.loads(json.dumps(base))
    for key, value in overrides.items():
        if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(repo_root: Path) -> Config:
    config_path = repo_root / ".codex-autorunner" / "config.yml"
    if not config_path.exists():
        raise ConfigError(f"Missing config file at {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    merged = _merge_defaults(DEFAULT_CONFIG, data)
    _validate_config(merged)

    docs = {
        "todo": Path(merged["docs"]["todo"]),
        "progress": Path(merged["docs"]["progress"]),
        "opinions": Path(merged["docs"]["opinions"]),
    }

    template_val = merged["prompt"].get("template")
    template = repo_root / template_val if template_val else None
    term_args = merged["codex"].get("terminal_args")
    terminal_args = list(term_args) if isinstance(term_args, list) else []

    return Config(
        raw=merged,
        repo_root=repo_root,
        docs=docs,
        codex_binary=merged["codex"]["binary"],
        codex_args=list(merged["codex"].get("args", [])),
        codex_terminal_args=terminal_args,
        prompt_prev_run_max_chars=int(merged["prompt"]["prev_run_max_chars"]),
        prompt_template=template,
        runner_sleep_seconds=int(merged["runner"]["sleep_seconds"]),
        runner_stop_after_runs=merged["runner"].get("stop_after_runs"),
        runner_max_wallclock_seconds=merged["runner"].get("max_wallclock_seconds"),
        git_auto_commit=bool(merged["git"].get("auto_commit", False)),
        git_commit_message_template=str(merged["git"].get("commit_message_template")),
        server_host=str(merged["server"].get("host")),
        server_port=int(merged["server"].get("port")),
    )


def _validate_config(cfg: Dict[str, Any]) -> None:
    if cfg.get("version") != 1:
        raise ConfigError("Unsupported config version; expected 1")
    docs = cfg.get("docs")
    if not isinstance(docs, dict):
        raise ConfigError("docs must be a mapping")
    for key in ("todo", "progress", "opinions"):
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
