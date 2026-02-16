import json
import re
from pathlib import Path
from typing import Any, Optional

import typer

from ....core.config import load_hub_config, load_repo_config
from ....core.self_describe import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    default_runtime_schema_path,
)
from ....core.state_roots import resolve_hub_templates_root
from ....core.templates import index_templates
from ....core.utils import RepoNotFoundError, find_repo_root
from .utils import get_car_version

SECRET_PATTERNS = [
    r"sk-[a-zA-Z0-9]{20,}",
    r"-----BEGIN [A-Z]+ PRIVATE KEY-----",
    r"-----BEGIN CERTIFICATE-----",
    r"ghp_[a-zA-Z0-9]{36,}",
    r"gho_[a-zA-Z0-9]{36,}",
    r"glpat-[a-zA-Z0-9\-]{20,}",
    r"AKIA[0-9A-Z]{16}",
]

SECRET_KEY_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)", re.IGNORECASE),
]


def _is_secret_value(value: str) -> bool:
    """Check if a value looks like a secret."""
    value = value.strip()
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, value):
            return True
    return False


def _looks_like_secret_key(key: str) -> bool:
    """Check if a key name suggests it contains a secret."""
    key_lower = key.lower()
    for pattern in SECRET_KEY_PATTERNS:
        if pattern.search(key_lower):
            return True
    return False


def _redact_value(key: str, value: Any) -> Any:
    """Redact a value if its key or content suggests it's a secret."""
    if isinstance(value, str):
        if _looks_like_secret_key(key) or _is_secret_value(value):
            return "<REDACTED>"
    elif isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    return value


def _collect_describe_data(repo_root: Path) -> dict[str, Any]:
    """Collect CAR self-description data from the repo."""
    config = load_repo_config(repo_root)
    raw = config.raw or {}

    car_dir = repo_root / ".codex-autorunner"
    has_init = car_dir.exists()

    schema_path = default_runtime_schema_path(repo_root)
    schema_exists = schema_path.exists()

    env_vars = _collect_env_knobs(raw)

    agents = raw.get("agents", {})
    agent_info = {}
    for agent_name, agent_config in agents.items():
        if isinstance(agent_config, dict):
            agent_info[agent_name] = {
                "binary": agent_config.get("binary"),
                "enabled": agent_config.get("enabled", True),
            }

    surfaces = _detect_active_surfaces(raw, car_dir)

    template_info = _collect_template_info(repo_root, raw)

    data = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "car_version": get_car_version(),
        "repo_root": str(repo_root),
        "initialized": has_init,
        "config_precedence": _get_config_precedence(raw),
        "env_knobs": env_vars,
        "agents": agent_info,
        "surfaces": surfaces,
        "features": _get_features(raw),
        "schema_path": str(schema_path) if schema_exists else None,
        "runtime_schema_exists": schema_exists,
        "templates": template_info,
    }

    data = _redact_dict(data)
    return data


def _redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact secrets from a dictionary."""
    result = {}
    for key, value in data.items():
        result[key] = _redact_value(key, value)
    return result


def _collect_env_knobs(raw: dict[str, Any]) -> list[str]:
    """Collect names of env var knobs (not values)."""
    knobs = set()

    env_section = raw.get("environment_variables", {})
    if isinstance(env_section, dict):
        knobs.update(env_section.keys())

    server_auth = raw.get("server", {}).get("auth_token_env")
    if server_auth:
        knobs.add(server_auth)

    telegram = raw.get("telegram_bot", {})
    for key in ["bot_token_env", "chat_id_env", "thread_id_env"]:
        val = telegram.get(key)
        if val:
            knobs.add(val)

    return sorted(knobs)


def _collect_template_info(repo_root: Path, raw: dict[str, Any]) -> dict[str, Any]:
    """Collect template-related information."""
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
        hub_config = load_hub_config(repo_root)
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
            templates = index_templates(hub_config, hub_root)
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


def _detect_active_surfaces(raw: dict[str, Any], car_dir: Path) -> dict[str, bool]:
    """Detect which surfaces are active."""
    surfaces = {
        "terminal": True,
        "web_ui": raw.get("server", {}).get("host") is not None,
        "telegram": raw.get("telegram_bot", {}).get("enabled", False) is True,
        "ticket_flow": True,
    }

    surfaces["templates"] = raw.get("templates", {}).get("enabled", True)

    return surfaces


def _get_features(raw: dict[str, Any]) -> dict[str, Any]:
    """Get feature flags and configuration."""
    return {
        "templates_enabled": raw.get("templates", {}).get("enabled", True),
        "telegram_enabled": raw.get("telegram_bot", {}).get("enabled", False),
        "voice_enabled": raw.get("voice", {}).get("enabled", False),
        "review_enabled": raw.get("review", {}).get("enabled", False),
    }


def _get_config_precedence(raw: dict[str, Any]) -> list[str]:
    """Return the effective config precedence used."""
    precedence = [
        "built_in_defaults",
    ]

    if raw:
        precedence.append("codex-autorunner.yml")

    override_path = raw.get("_override_path")
    if override_path:
        precedence.append(override_path)

    if raw:
        precedence.append(".codex-autorunner/config.yml")

    precedence.append("environment_variables")

    return precedence


def _format_human(data: dict[str, Any]) -> str:
    """Format describe output as human-readable text."""
    lines = []

    lines.append("Codex Autorunner (CAR) Description")
    lines.append("=" * 40)
    lines.append(f"CAR Version: {data.get('car_version', 'unknown')}")
    lines.append(f"Repo Root: {data.get('repo_root', 'unknown')}")
    lines.append(f"Initialized: {data.get('initialized', False)}")
    lines.append("")

    surfaces = data.get("surfaces", {})
    lines.append("Active Surfaces:")
    for surface, active in surfaces.items():
        status = "enabled" if active else "disabled"
        lines.append(f"  - {surface}: {status}")
    lines.append("")

    agents = data.get("agents", {})
    if agents:
        lines.append("Agents:")
        for agent_name, agent_data in agents.items():
            binary = agent_data.get("binary", "unknown")
            enabled = agent_data.get("enabled", True)
            status = "enabled" if enabled else "disabled"
            lines.append(f"  - {agent_name}: binary={binary} ({status})")
        lines.append("")

    features = data.get("features", {})
    if features:
        lines.append("Features:")
        for feature, enabled in features.items():
            status = "on" if enabled else "off"
            lines.append(f"  - {feature}: {status}")
        lines.append("")

    env_knobs = data.get("env_knobs", [])
    if env_knobs:
        lines.append(f"Environment Variables ({len(env_knobs)}):")
        for knob in env_knobs[:10]:
            lines.append(f"  - {knob}")
        if len(env_knobs) > 10:
            lines.append(f"  ... and {len(env_knobs) - 10} more")
        lines.append("")

    schema_path = data.get("schema_path")
    if schema_path:
        lines.append(f"Schema: {schema_path}")
    templates = data.get("templates", {})
    if isinstance(templates, dict):
        commands = templates.get("commands", {})
        apply_cmds = commands.get("apply", []) if isinstance(commands, dict) else []
        if apply_cmds:
            lines.append("")
            lines.append("Template Apply:")
            for cmd in apply_cmds[:2]:
                lines.append(f"  - {cmd}")

    return "\n".join(lines)


def register_describe_commands(
    app: typer.Typer,
    require_repo_config: Any,
    raise_exit: Any,
) -> None:
    @app.command()
    def describe(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
        schema: bool = typer.Option(
            False, "--schema", help="Print schema location and exit"
        ),
    ):
        """Show CAR layout and behavior summary (human or JSON)."""
        try:
            repo_root = find_repo_root(repo or Path.cwd())
        except RepoNotFoundError as exc:
            raise_exit("No .git directory found for repo commands.", cause=exc)

        if schema:
            schema_path = default_runtime_schema_path(repo_root)
            typer.echo(f"Schema ID: {SCHEMA_ID}")
            typer.echo(f"Schema Version: {SCHEMA_VERSION}")
            typer.echo(f"Runtime Schema Path: {schema_path}")
            if schema_path.exists():
                typer.echo("Schema Status: available")
            else:
                typer.echo("Schema Status: not found (run car init to generate)")
            return

        try:
            data = _collect_describe_data(repo_root)
        except Exception as exc:
            raise_exit(f"Failed to collect describe data: {exc}", cause=exc)

        if json_output:
            typer.echo(json.dumps(data, indent=2))
        else:
            typer.echo(_format_human(data))
