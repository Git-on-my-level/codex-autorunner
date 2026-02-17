import json
from pathlib import Path
from typing import Any, Optional

import typer

from ....core.self_describe import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    collect_describe_data,
    default_runtime_schema_path,
)


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
        ctx = require_repo_config(repo, hub)
        repo_root = ctx.repo_root

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
            data = collect_describe_data(repo_root, hub_root=hub)
        except Exception as exc:
            raise_exit(f"Failed to collect describe data: {exc}", cause=exc)

        if json_output:
            typer.echo(json.dumps(data, indent=2))
        else:
            typer.echo(_format_human(data))
