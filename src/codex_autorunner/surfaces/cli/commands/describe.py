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
from ..hub_path_option import hub_root_path_option


def _format_human(data: dict[str, Any]) -> str:
    lines = []
    lines.append(f"car_version={data.get('car_version', 'unknown')}")
    lines.append(
        f"repo={data.get('repo_root', 'unknown')} initialized={data.get('initialized', False)}"
    )
    surfaces = data.get("surfaces", {})
    parts = [f"{s}={'on' if a else 'off'}" for s, a in surfaces.items()]
    if parts:
        lines.append(f"surfaces: {', '.join(parts)}")
    agents = data.get("agents", {})
    if agents:
        agent_parts = [
            f"{n}=binary={d.get('binary', '?')} ({'enabled' if d.get('enabled', True) else 'disabled'})"
            for n, d in agents.items()
        ]
        lines.append(f"agents: {', '.join(agent_parts)}")
    features = data.get("features", {})
    if features:
        feat_parts = [f"{f}={'on' if e else 'off'}" for f, e in features.items()]
        lines.append(f"features: {', '.join(feat_parts)}")
    env_knobs = data.get("env_knobs", [])
    if env_knobs:
        knob_list = " ".join(env_knobs[:10])
        suffix = f" +{len(env_knobs) - 10}" if len(env_knobs) > 10 else ""
        lines.append(f"env_knobs({len(env_knobs)}): {knob_list}{suffix}")
    schema_path = data.get("schema_path")
    if schema_path:
        lines.append(f"schema={schema_path}")
    templates = data.get("templates", {})
    if isinstance(templates, dict):
        commands = templates.get("commands", {})
        apply_cmds = commands.get("apply", []) if isinstance(commands, dict) else []
        if apply_cmds:
            lines.append(f"template_apply: {', '.join(apply_cmds[:2])}")
    return "\n".join(lines)


def register_describe_commands(
    app: typer.Typer,
    require_repo_config: Any,
    raise_exit: Any,
) -> None:
    @app.command()
    def describe(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
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
            status = "available" if schema_path.exists() else "not found"
            typer.echo(
                f"schema_id={SCHEMA_ID} version={SCHEMA_VERSION} path={schema_path} status={status}"
            )
            return

        try:
            data = collect_describe_data(repo_root, hub_root=hub)
        except Exception as exc:  # intentional: self-describe diagnostic error barrier
            raise_exit(f"Failed to collect describe data: {exc}", cause=exc)

        if json_output:
            typer.echo(json.dumps(data, indent=2))
        else:
            typer.echo(_format_human(data))
