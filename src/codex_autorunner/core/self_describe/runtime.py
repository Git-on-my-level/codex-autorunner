"""Shared runtime collectors for `car describe` across surfaces."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path
from typing import Any

from ..config import load_hub_config, load_repo_config
from ..state_roots import resolve_hub_templates_root
from ..templates import index_templates
from .contract import (
    CONFIG_PRECEDENCE,
    NON_SECRET_ENV_KNOBS,
    SCHEMA_ID,
    SCHEMA_VERSION,
    default_runtime_schema_path,
)

SECRET_PATTERNS = [
    r"sk-[a-zA-Z0-9]{20,}",
    r"-----BEGIN [A-Z]+ PRIVATE KEY-----",
    r"-----BEGIN CERTIFICATE-----",
    r"ghp_[a-zA-Z0-9]{36,}",
    r"gho_[a-zA-Z0-9]{36,}",
    r"glpat-[a-zA-Z0-9\\-]{20,}",
    r"AKIA[0-9A-Z]{16}",
]

SECRET_KEY_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)", re.IGNORECASE),
]


def get_car_version() -> str:
    try:
        return importlib.metadata.version("codex-autorunner")
    except Exception:
        return "unknown"


def _is_secret_value(value: str) -> bool:
    value = value.strip()
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, value):
            return True
    return False


def _looks_like_secret_key(key: str) -> bool:
    key_lower = key.lower()
    for pattern in SECRET_KEY_PATTERNS:
        if pattern.search(key_lower):
            return True
    return False


def _redact_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        if _looks_like_secret_key(key) or _is_secret_value(value):
            return "<REDACTED>"
    elif isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    return value


def _redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in data.items():
        result[key] = _redact_value(key, value)
    return result


def _collect_env_knobs(raw: dict[str, Any]) -> list[str]:
    knobs = set(NON_SECRET_ENV_KNOBS)

    env_section = raw.get("environment_variables", {})
    if isinstance(env_section, dict):
        knobs.update(str(k) for k in env_section.keys())

    server_auth = raw.get("server", {}).get("auth_token_env")
    if isinstance(server_auth, str) and server_auth.strip():
        knobs.add(server_auth.strip())

    telegram = raw.get("telegram_bot", {})
    for key in ("bot_token_env", "chat_id_env", "thread_id_env"):
        val = telegram.get(key)
        if isinstance(val, str) and val.strip():
            knobs.add(val.strip())

    return sorted(knobs)


def _collect_template_info(
    repo_root: Path,
    raw: dict[str, Any],
    *,
    hub_root: Path | None = None,
) -> dict[str, Any]:
    commands = {
        "list": [
            "car templates repos list",
            "car templates repos list --json",
        ],
        "apply": [
            "car templates apply <repo_id>:<path>[@<ref>]",
            "car template apply <repo_id>:<path>[@<ref>]",
        ],
    }
    try:
        hub_config = (
            load_hub_config(hub_root)
            if hub_root is not None
            else load_hub_config(repo_root)
        )
    except Exception:
        return {
            "enabled": False,
            "root": None,
            "repos": [],
            "count": 0,
            "commands": commands,
        }

    templates_config = raw.get("templates", {})
    enabled = (
        templates_config.get("enabled", True)
        if isinstance(templates_config, dict)
        else True
    )

    if not enabled:
        return {
            "enabled": False,
            "root": None,
            "repos": [],
            "count": 0,
            "commands": commands,
        }

    try:
        hub_root = hub_config.root
        templates_root = resolve_hub_templates_root(hub_root)
    except Exception:
        templates_root = None

    repos = []
    template_repos = (
        templates_config.get("repos", []) if isinstance(templates_config, dict) else []
    )
    for repo in template_repos:
        if isinstance(repo, dict):
            repos.append(
                {
                    "id": repo.get("id"),
                    "url": repo.get("url"),
                    "trusted": repo.get("trusted", False),
                }
            )

    template_count = 0
    if templates_root and templates_root.exists():
        try:
            templates = index_templates(hub_config, hub_config.root)
            template_count = len(templates)
        except Exception:
            pass

    return {
        "enabled": True,
        "root": str(templates_root) if templates_root else None,
        "repos": repos,
        "count": template_count,
        "commands": commands,
    }


def _detect_active_surfaces(raw: dict[str, Any]) -> dict[str, bool]:
    surfaces = {
        "terminal": True,
        "web_ui": raw.get("server", {}).get("host") is not None,
        "telegram": raw.get("telegram_bot", {}).get("enabled", False) is True,
        "ticket_flow": True,
    }
    surfaces["templates"] = raw.get("templates", {}).get("enabled", True)
    return surfaces


def _get_features(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "templates_enabled": raw.get("templates", {}).get("enabled", True),
        "ticket_frontmatter_context_includes": True,
        "telegram_enabled": raw.get("telegram_bot", {}).get("enabled", False),
        "voice_enabled": raw.get("voice", {}).get("enabled", False),
        "review_enabled": raw.get("review", {}).get("enabled", False),
    }


def collect_describe_data(
    repo_root: Path, *, hub_root: Path | None = None
) -> dict[str, Any]:
    if hub_root is None:
        config = load_repo_config(repo_root)
    else:
        config = load_repo_config(repo_root, hub_path=hub_root)
    raw = config.raw or {}

    car_dir = repo_root / ".codex-autorunner"
    has_init = car_dir.exists()

    schema_path = default_runtime_schema_path(repo_root)
    schema_exists = schema_path.exists()

    data = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "car_version": get_car_version(),
        "repo_root": str(repo_root),
        "initialized": has_init,
        "config_precedence": list(CONFIG_PRECEDENCE),
        "env_knobs": _collect_env_knobs(raw),
        "agents": {
            name: {
                "binary": cfg.get("binary"),
                "enabled": cfg.get("enabled", True),
            }
            for name, cfg in raw.get("agents", {}).items()
            if isinstance(cfg, dict)
        },
        "surfaces": _detect_active_surfaces(raw),
        "features": _get_features(raw),
        "schema_path": str(schema_path) if schema_exists else None,
        "runtime_schema_exists": schema_exists,
        "templates": _collect_template_info(repo_root, raw, hub_root=hub_root),
    }

    return _redact_dict(data)
