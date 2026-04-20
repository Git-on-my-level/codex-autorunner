"""PMA CLI commands for Project Management Assistant."""

import json
import logging
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from ...bootstrap import ensure_pma_docs, pma_doc_path
from ...core.config import load_hub_config
from ...core.config_contract import ConfigError
from ...core.filebox import BOXES, list_filebox
from ...core.filebox_lifecycle import (
    LIFECYCLE_BOXES,
    consume_inbox_file,
    dismiss_inbox_file,
    list_consumed_files,
    unconsume_inbox_file,
)
from ...core.locks import file_lock
from ...core.pma_hygiene import (
    _hygiene_lock_path,
    apply_pma_hygiene_report,
    build_pma_hygiene_report,
    render_pma_hygiene_report,
)
from .hub_path_option import hub_root_path_option
from .pma_control_plane import (
    CAPABILITY_REQUIREMENTS as _CAPABILITY_REQUIREMENTS,
)
from .pma_control_plane import (
    build_pma_url as _build_pma_url,
)
from .pma_control_plane import (
    check_capability as _check_capability,
)
from .pma_control_plane import (
    fetch_agent_capabilities as _fetch_agent_capabilities,
)
from .pma_control_plane import (
    format_resource_owner_label as _format_resource_owner_label,
)
from .pma_control_plane import (
    normalize_resource_owner_options as _normalize_resource_owner_options,
)
from .pma_control_plane import (
    request_json as _request_json,
)
from .pma_thread_commands import register_thread_commands

logger = logging.getLogger(__name__)

pma_app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    help="Project Management Assistant commands for chat, docs, and managed threads.",
)
docs_app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    name="docs",
    help="Read and edit PMA durable docs.",
)
context_app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    name="context",
    help="Snapshot and compact PMA active context.",
)
thread_app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    name="thread",
    help="Manage PMA managed threads and turns.",
)
binding_app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    name="binding",
    help="Query orchestration bindings and busy work.",
)
file_app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    name="file",
    help="Manage PMA inbox file lifecycle state without deleting recoverable files.",
)
pma_app.add_typer(docs_app)
pma_app.add_typer(context_app)
register_thread_commands(thread_app)
pma_app.add_typer(thread_app, name="thread")
pma_app.add_typer(binding_app, name="binding")
pma_app.add_typer(file_app, name="file")


def _pma_docs_path(hub_root: Path, doc_name: str) -> Path:
    return pma_doc_path(hub_root, doc_name)


def _resolve_hub_path(path: Optional[Path]) -> Path:
    start = path or Path.cwd()
    try:
        return load_hub_config(start).root
    except (
        OSError,
        ValueError,
        ConfigError,
        AttributeError,
    ):  # intentional: config loading fallback
        candidate = start.resolve()
        if candidate.is_file():
            parent = candidate.parent
            if parent.name == ".codex-autorunner":
                return parent.parent.resolve()
            return parent.resolve()
        return candidate


def _pma_file_listings(hub_root: Path) -> dict[str, list[Any]]:
    listing = list_filebox(hub_root)
    archived = list_consumed_files(hub_root)
    for box in LIFECYCLE_BOXES:
        listing[box] = [entry for entry in archived if entry.box == box]
    return listing


def _emit_pma_file_listing(
    *,
    listing: dict[str, list[Any]],
    boxes: list[str],
    output_json: bool,
) -> None:
    if output_json:
        payload = {
            box: [
                {
                    "name": entry.name,
                    "box": entry.box,
                    "size": entry.size,
                    "modified_at": entry.modified_at,
                    "source": entry.source,
                }
                for entry in listing.get(box, [])
            ]
            for box in boxes
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    for index, box in enumerate(boxes):
        entries = listing.get(box, [])
        heading = box.capitalize()
        prefix = "\n" if index else ""
        typer.echo(f"{prefix}{heading} ({len(entries)}):")
        for entry in entries:
            source_str = f", source={entry.source}" if entry.source else ""
            typer.echo(
                f"  - {entry.name} ({entry.size} bytes, {entry.modified_at}{source_str})"
            )


def _extract_compact_summary_items(content: str, *, limit: int) -> list[str]:
    items: list[str] = []
    if limit <= 0:
        return items
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("# pma active context"):
            continue
        if lower.startswith("use this file for"):
            continue
        if lower.startswith("pruning guidance"):
            continue
        if lower.startswith("> auto-pruned on"):
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        elif line.startswith("* "):
            line = line[2:].strip()
        elif line[:2].isdigit() and line[2:4] == ". ":
            line = line[4:].strip()
        if not line:
            continue
        if line in items:
            continue
        items.append(line)
        if len(items) >= limit:
            break
    return items


def _box_choices_text() -> str:
    return "|".join(BOXES)


def _file_lifecycle_box_choices_text() -> str:
    return "inbox|consumed|dismissed|all"


def _is_json_response_error(data: dict) -> Optional[str]:
    if not isinstance(data, dict):
        return "Unexpected response format"
    if data.get("detail"):
        return str(data["detail"])
    if data.get("error"):
        return str(data["error"])
    return None


def _render_compacted_active_context(
    *,
    timestamp: str,
    previous_line_count: int,
    max_lines: int,
    summary_items: list[str],
) -> str:
    base_lines = [
        "# PMA active context (short-lived)",
        "",
        "## Current priorities",
        "- Keep this section focused on in-flight priorities only.",
        "",
        "## Next steps",
        "- Capture immediate, executable follow-ups for the next PMA turn.",
        "",
        "## Open questions",
        "- Record unresolved blockers requiring explicit answers.",
        "",
        "## Compaction metadata",
        f"- Compacted at: {timestamp}",
        f"- Previous line count: {previous_line_count}",
        f"- Active context line budget: {max_lines}",
        "- Archived snapshot appended to context_log.md.",
        "",
        "## Archived context summary",
    ]
    remaining = max(max_lines - len(base_lines), 0)
    summary_lines = [f"- {item}" for item in summary_items[:remaining]]
    if not summary_lines and max_lines > len(base_lines):
        summary_lines = ["- No additional archival summary captured."]
    output_lines = base_lines + summary_lines
    if max_lines > 0:
        output_lines = output_lines[:max_lines]
    return "\n".join(output_lines).rstrip() + "\n"


@pma_app.command("chat")
def pma_chat(
    message: str = typer.Argument(..., help="Message to send to PMA"),
    agent: Optional[str] = typer.Option(
        None, "--agent", help="Agent to use (codex|opencode|hermes)"
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Model override"),
    reasoning: Optional[str] = typer.Option(
        None, "--reasoning", help="Reasoning effort override"
    ),
    stream: bool = typer.Option(False, "--stream", help="Stream response tokens"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Send a message to the Project Management Assistant."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, "/chat")
    payload: dict[str, Any] = {"message": message, "stream": stream}
    if agent:
        payload["agent"] = agent
    if model:
        payload["model"] = model
    if reasoning:
        payload["reasoning"] = reasoning

    if stream:
        import os

        def parse_sse_line(line: str) -> tuple[Optional[str], Optional[dict]]:
            if not line:
                return None, None
            event_type: Optional[str] = None
            data: Optional[dict] = {}
            for part in line.split("\n"):
                if part.startswith("event:"):
                    event_type = part[6:].strip()
                elif part.startswith("data:"):
                    data_str = part[5:].strip()
                    data = {"raw": data_str}
            return event_type, data

        token_env = config.server_auth_token_env
        headers = None
        if token_env:
            token = os.environ.get(token_env)
            if token and token.strip():
                headers = {"Authorization": f"Bearer {token.strip()}"}

        try:
            with httpx.stream(
                "POST", url, json=payload, timeout=240.0, headers=headers
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    event_type, data = parse_sse_line(line)
                    if event_type is None or data is None:
                        continue
                    if event_type == "status":
                        if output_json:
                            typer.echo(
                                json.dumps({"event": "status", **data}, indent=2)
                            )
                        continue
                    if event_type == "token":
                        token = data.get("token", "") if isinstance(data, dict) else ""
                        if output_json:
                            typer.echo(
                                json.dumps({"event": "token", "token": token}, indent=2)
                            )
                        else:
                            typer.echo(token, nl=False)
                    elif event_type == "update":
                        status = data.get("status") if isinstance(data, dict) else ""
                        msg = data.get("message") if isinstance(data, dict) else ""
                        if output_json:
                            typer.echo(
                                json.dumps(
                                    {
                                        "event": "update",
                                        "status": status,
                                        "message": msg,
                                    },
                                    indent=2,
                                )
                            )
                        else:
                            typer.echo(f"\nStatus: {status}")
                    elif event_type == "error":
                        detail = (
                            data.get("detail")
                            if isinstance(data, dict)
                            else "Unknown error"
                        )
                        if output_json:
                            typer.echo(
                                json.dumps(
                                    {"event": "error", "detail": detail}, indent=2
                                )
                            )
                        else:
                            typer.echo(f"\nError: {detail}", err=True)
                    elif event_type == "done":
                        if not output_json:
                            typer.echo()
                        return
                    elif event_type == "interrupted":
                        detail = (
                            data.get("detail")
                            if isinstance(data, dict)
                            else "Interrupted"
                        )
                        if output_json:
                            typer.echo(
                                json.dumps(
                                    {"event": "interrupted", "detail": detail}, indent=2
                                )
                            )
                        else:
                            typer.echo(f"\nInterrupted: {detail}")
                        return
        except httpx.HTTPError as exc:
            typer.echo(f"HTTP error: {exc}", err=True)
            raise typer.Exit(code=1) from None
        except (ValueError, OSError) as exc:  # intentional: top-level error handler
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from None
        return

    try:
        data = _request_json(
            "POST", url, payload, token_env=config.server_auth_token_env
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    error = _is_json_response_error(data)
    if error:
        if output_json:
            typer.echo(json.dumps({"error": error, "detail": data}, indent=2))
        else:
            typer.echo(f"Chat failed: {error}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        msg = data.get("message") if isinstance(data, dict) else ""
        typer.echo(msg or "No message returned")


@pma_app.command("interrupt")
def pma_interrupt(
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Interrupt a running PMA chat."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    active_url = _build_pma_url(config, "/active")
    try:
        active_data = _request_json(
            "GET", active_url, token_env=config.server_auth_token_env
        )
    except (httpx.HTTPError, ValueError, OSError):  # best-effort active data fetch
        active_data = {}
    current = active_data.get("current", {}) if isinstance(active_data, dict) else {}
    if isinstance(current, dict):
        agent = current.get("agent", "")
        if agent:
            capabilities = _fetch_agent_capabilities(config, path)
            required_cap = _CAPABILITY_REQUIREMENTS.get("interrupt")
            if required_cap and not _check_capability(
                agent, required_cap, capabilities
            ):
                typer.echo(
                    f"Agent '{agent}' does not support interrupt (missing capability: {required_cap})",
                    err=True,
                )
                raise typer.Exit(code=1) from None

    url = _build_pma_url(config, "/interrupt")

    try:
        data = _request_json("POST", url, token_env=config.server_auth_token_env)
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        interrupted = data.get("interrupted") if isinstance(data, dict) else False
        detail = data.get("detail") if isinstance(data, dict) else ""
        agent = data.get("agent") if isinstance(data, dict) else ""
        if interrupted:
            typer.echo(f"PMA chat interrupted (agent={agent})")
        else:
            typer.echo("No active PMA chat to interrupt")
            if detail:
                typer.echo(f"Detail: {detail}")


@pma_app.command("reset")
def pma_reset(
    agent: Optional[str] = typer.Option(
        None, "--agent", help="Agent thread to reset (opencode|codex|hermes|all)"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Reset PMA thread state."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, "/thread/reset")
    payload: dict[str, Any] = {}
    if agent:
        payload["agent"] = agent

    try:
        data = _request_json(
            "POST", url, payload, token_env=config.server_auth_token_env
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        cleared = data.get("cleared") if isinstance(data, dict) else []
        if cleared:
            typer.echo(f"Cleared threads: {', '.join(cleared)}")
        else:
            typer.echo("No threads to clear")


@pma_app.command("active")
def pma_active(
    client_turn_id: Optional[str] = typer.Option(
        None, "--turn-id", help="Filter by client turn ID"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Show active PMA chat status."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, "/active")
    params = {}
    if client_turn_id:
        params["client_turn_id"] = client_turn_id

    try:
        response = httpx.get(url, params=params, timeout=5.0)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        active = data.get("active") if isinstance(data, dict) else False
        current = data.get("current") if isinstance(data, dict) else {}
        last_result = data.get("last_result") if isinstance(data, dict) else {}

        typer.echo(f"Active: {active}")
        if current:
            status = current.get("status", "unknown")
            agent = current.get("agent", "unknown")
            started = current.get("started_at", "")
            typer.echo(
                f"Current turn: status={status}, agent={agent}, started={started}"
            )
        if last_result:
            status = last_result.get("status", "unknown")
            agent = last_result.get("agent", "unknown")
            finished = last_result.get("finished_at", "")
            typer.echo(
                f"Last result: status={status}, agent={agent}, finished={finished}"
            )


@pma_app.command("hygiene")
def pma_hygiene(
    category: list[str] = typer.Option(
        None,
        "--category",
        help=(
            "Limit to one or more hygiene categories: files, threads, automation, "
            "alerts. Repeat the option or pass a comma-separated list."
        ),
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Apply only safe PMA hygiene candidates after showing the report.",
    ),
    include_needs_confirmation: bool = typer.Option(
        False,
        "--include-needs-confirmation",
        "--force-non-safe",
        help=(
            "Also apply reviewed needs-confirmation managed-thread cleanup candidates."
        ),
    ),
    summary: bool = typer.Option(
        False,
        "--summary",
        help="Show counts only instead of expanding individual candidates.",
    ),
    stale_threshold_seconds: Optional[int] = typer.Option(
        None,
        "--stale-threshold-seconds",
        min=1,
        help="Override the stale-age threshold used for PMA hygiene classification.",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Review PMA hygiene candidates and optionally clean only the safe ones."""
    hub_root = _resolve_hub_path(path)
    apply_result: Optional[dict[str, Any]] = None
    try:
        lock_ctx = file_lock(_hygiene_lock_path(hub_root)) if apply else nullcontext()
        with lock_ctx:
            report = build_pma_hygiene_report(
                hub_root,
                categories=category or None,
                stale_threshold_seconds=stale_threshold_seconds,
            )
            if apply:
                apply_result = apply_pma_hygiene_report(
                    hub_root,
                    report,
                    include_needs_confirmation=include_needs_confirmation,
                )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except OSError as exc:
        typer.echo(f"Failed to build PMA hygiene report: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        report_payload: dict[str, Any]
        if summary:
            report_payload = {
                "generated_at": report.get("generated_at"),
                "categories": report.get("categories"),
                "summary": report.get("summary"),
            }
        else:
            report_payload = report
        typer.echo(
            json.dumps({"report": report_payload, "apply": apply_result}, indent=2)
        )
        return

    typer.echo(render_pma_hygiene_report(report, apply=apply, summary_only=summary))
    if apply_result is not None:
        if include_needs_confirmation:
            typer.echo(
                "Applied cleanup: "
                f"safe_attempted={apply_result['safe_attempted']} "
                f"reviewed_attempted={apply_result['reviewed_attempted']} "
                f"applied={apply_result['applied']} "
                f"failed={apply_result['failed']}"
            )
        else:
            typer.echo(
                "Applied safe cleanup: "
                f"attempted={apply_result['attempted']} "
                f"applied={apply_result['applied']} "
                f"failed={apply_result['failed']}"
            )


@pma_app.command("agents")
def pma_agents(
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List available PMA agents."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, "/agents")

    try:
        data = _request_json("GET", url, token_env=config.server_auth_token_env)
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        agents = data.get("agents", []) if isinstance(data, dict) else []
        default = data.get("default", "") if isinstance(data, dict) else ""
        defaults = data.get("defaults", {}) if isinstance(data, dict) else {}

        typer.echo(f"Default agent: {default or 'none'}")
        if defaults:
            typer.echo("Defaults:")
            for key, value in defaults.items():
                typer.echo(f"  {key}: {value}")
        typer.echo(f"\nAgents ({len(agents)}):")
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            agent_id = agent.get("id", "")
            agent_name = agent.get("name", agent_id)
            capabilities = agent.get("capabilities", [])
            capability_str = ", ".join(sorted(capabilities)) if capabilities else "none"
            typer.echo(f"  - {agent_name} ({agent_id})")
            typer.echo(f"    Capabilities: {capability_str}")
            session_controls = agent.get("session_controls", {})
            if isinstance(session_controls, dict):
                enabled_controls = [
                    key for key, enabled in sorted(session_controls.items()) if enabled
                ]
                if enabled_controls:
                    typer.echo("    Session controls: " + ", ".join(enabled_controls))
            commands = agent.get("advertised_commands", [])
            if isinstance(commands, list) and commands:
                typer.echo("    Commands:")
                for command in commands:
                    if not isinstance(command, dict):
                        continue
                    name = str(command.get("name") or "").strip()
                    if not name:
                        continue
                    description = str(command.get("description") or "").strip()
                    if description:
                        typer.echo(f"      {name}: {description}")
                    else:
                        typer.echo(f"      {name}")


@pma_app.command("models")
def pma_models(
    agent: str = typer.Argument(..., help="Agent ID (codex|opencode|hermes)"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List available models for an agent."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    capabilities = _fetch_agent_capabilities(config, path)
    required_cap = _CAPABILITY_REQUIREMENTS.get("models")
    if required_cap and not _check_capability(agent, required_cap, capabilities):
        typer.echo(
            f"Agent '{agent}' does not support model listing (missing capability: {required_cap})",
            err=True,
        )
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, f"/agents/{agent}/models")

    try:
        data = _request_json("GET", url, token_env=config.server_auth_token_env)
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        models = data.get("models", []) if isinstance(data, dict) else []
        default_model = data.get("default_model", "") if isinstance(data, dict) else ""

        typer.echo(f"Default model: {default_model or 'none'}")
        typer.echo(f"\nModels ({len(models)}):")
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id", "")
            model_name = model.get("name", model_id)
            typer.echo(f"  - {model_name} ({model_id})")


@pma_app.command("files")
def pma_files(
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List files in PMA inbox and outbox (FileBox-backed via /hub/pma/files)."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, "/files")

    try:
        data = _request_json("GET", url, token_env=config.server_auth_token_env)
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        for index, box in enumerate(BOXES):
            entries = data.get(box, []) if isinstance(data, dict) else []
            heading = box.capitalize()
            prefix = "\n" if index else ""
            typer.echo(f"{prefix}{heading} ({len(entries)}):")
            for file in entries:
                if not isinstance(file, dict):
                    continue
                name = file.get("name", "")
                size = file.get("size", 0)
                modified = file.get("modified_at", "")
                source = file.get("source", "")
                source_str = f", source={source}" if source else ""
                typer.echo(f"  - {name} ({size} bytes, {modified}{source_str})")


@file_app.command("consume")
def pma_file_consume(
    filename: str = typer.Argument(..., help="Inbox filename to mark as consumed"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Move a PMA inbox file into the recoverable consumed archive."""
    hub_root = _resolve_hub_path(path)
    try:
        entry = consume_inbox_file(hub_root, filename)
    except ValueError as exc:
        typer.echo(f"Invalid filename: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except FileExistsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"Consumed {entry.name} -> {entry.box}")


@file_app.command("dismiss")
def pma_file_dismiss(
    filename: str = typer.Argument(..., help="Inbox filename to mark as dismissed"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Move a PMA inbox file into the recoverable dismissed archive."""
    hub_root = _resolve_hub_path(path)
    try:
        entry = dismiss_inbox_file(hub_root, filename)
    except ValueError as exc:
        typer.echo(f"Invalid filename: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except FileExistsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"Dismissed {entry.name} -> {entry.box}")


@file_app.command("restore")
def pma_file_restore(
    filename: str = typer.Argument(..., help="Archived filename to restore to inbox"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Restore a consumed or dismissed PMA file back to the inbox."""
    hub_root = _resolve_hub_path(path)
    try:
        entry = unconsume_inbox_file(hub_root, filename)
    except ValueError as exc:
        typer.echo(f"Invalid filename: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except FileExistsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"Restored {entry.name} -> {entry.box}")


@file_app.command("list")
def pma_file_list(
    box: str = typer.Option(
        "all",
        "--box",
        help=f"Box to list ({_file_lifecycle_box_choices_text()})",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List PMA inbox and archived lifecycle files from the local FileBox."""
    normalized_box = str(box or "").strip().lower()
    if normalized_box not in {"inbox", *LIFECYCLE_BOXES, "all"}:
        typer.echo(
            f"Box must be one of: {_file_lifecycle_box_choices_text()}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    hub_root = _resolve_hub_path(path)
    listing = _pma_file_listings(hub_root)
    boxes = ["inbox", *LIFECYCLE_BOXES] if normalized_box == "all" else [normalized_box]
    _emit_pma_file_listing(listing=listing, boxes=boxes, output_json=output_json)


@pma_app.command("upload")
def pma_upload(
    box: str = typer.Argument(..., help=f"Target box ({_box_choices_text()})"),
    files: list[Path] = typer.Argument(..., help="Files to upload"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Upload files to PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if box not in BOXES:
        typer.echo(f"Box must be one of: {', '.join(BOXES)}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, f"/files/{box}")

    for file_path in files:
        if not file_path.exists():
            typer.echo(f"File not found: {file_path}", err=True)
            raise typer.Exit(code=1) from None

    import os

    token_env = config.server_auth_token_env
    headers = {}
    if token_env:
        token = os.environ.get(token_env)
        if token and token.strip():
            headers["Authorization"] = f"Bearer {token.strip()}"

    saved_files: list[str] = []
    for file_path in files:
        try:
            with open(file_path, "rb") as f:
                files_data = {"file": (file_path.name, f, "application/octet-stream")}
                response = httpx.post(
                    url, files=files_data, headers=headers, timeout=30.0
                )
                response.raise_for_status()
                data = response.json()
                saved = data.get("saved", []) if isinstance(data, dict) else []
                saved_files.extend(saved)
        except httpx.HTTPError as exc:
            typer.echo(f"HTTP error uploading {file_path}: {exc}", err=True)
            raise typer.Exit(code=1) from None
        except OSError as exc:
            typer.echo(f"Error reading file {file_path}: {exc}", err=True)
            raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps({"saved": saved_files}, indent=2))
    else:
        typer.echo(f"Uploaded {len(saved_files)} file(s): {', '.join(saved_files)}")


@pma_app.command("download")
def pma_download(
    box: str = typer.Argument(..., help=f"Source box ({_box_choices_text()})"),
    filename: str = typer.Argument(..., help="File to download"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Output path (default: current directory)"
    ),
    path: Optional[Path] = hub_root_path_option(),
):
    """Download a file from PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if box not in BOXES:
        typer.echo(f"Box must be one of: {', '.join(BOXES)}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, f"/files/{box}/{filename}")

    try:
        response = httpx.get(url, timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    output_path = output if output else Path(filename)
    output_path.write_bytes(response.content)
    typer.echo(f"Downloaded to {output_path}")


@pma_app.command("delete")
def pma_delete(
    box: Optional[str] = typer.Argument(
        None, help=f"Target box ({_box_choices_text()})"
    ),
    filename: Optional[str] = typer.Argument(None, help="File to delete"),
    all_files: bool = typer.Option(False, "--all", help="Delete all files in the box"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Delete files from PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if all_files:
        if not box or box not in BOXES:
            typer.echo(
                f"Box must be one of: {', '.join(BOXES)} when using --all",
                err=True,
            )
            raise typer.Exit(code=1) from None
        url = _build_pma_url(config, f"/files/{box}")
        method = "DELETE"
        payload = None
    else:
        if not box or not filename:
            typer.echo("Box and filename are required (or use --all)", err=True)
            raise typer.Exit(code=1) from None
        if box not in BOXES:
            typer.echo(f"Box must be one of: {', '.join(BOXES)}", err=True)
            raise typer.Exit(code=1) from None
        url = _build_pma_url(config, f"/files/{box}/{filename}")
        method = "DELETE"
        payload = None

    try:
        response = httpx.request(method, url, json=payload, timeout=30.0)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        if all_files:
            typer.echo(f"Deleted all files in {box}")
        else:
            typer.echo(f"Deleted {filename} from {box}")


@docs_app.command("show")
def pma_docs_show(
    doc_type: str = typer.Argument(..., help="Document type: agents, active, or log"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Show PMA docs content to stdout."""
    hub_root = _resolve_hub_path(path)
    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        typer.echo(f"Failed to ensure PMA docs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if doc_type == "agents":
        doc_path = _pma_docs_path(hub_root, "AGENTS.md")
    elif doc_type == "active":
        doc_path = _pma_docs_path(hub_root, "active_context.md")
    elif doc_type == "log":
        doc_path = _pma_docs_path(hub_root, "context_log.md")
    else:
        typer.echo("Invalid doc_type. Must be one of: agents, active, log", err=True)
        raise typer.Exit(code=1) from None

    try:
        content = doc_path.read_text(encoding="utf-8")
        typer.echo(content, nl=False)
    except OSError as exc:
        typer.echo(f"Failed to read {doc_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None


@context_app.command("reset")
def pma_context_reset(
    path: Optional[Path] = hub_root_path_option(),
):
    """Reset active_context.md to a minimal header."""
    hub_root = _resolve_hub_path(path)
    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        typer.echo(f"Failed to ensure PMA docs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    active_context_path = _pma_docs_path(hub_root, "active_context.md")

    minimal_content = """# PMA active context (short-lived)

Use this file for the current working set: active projects, open questions, links, and immediate next steps.

Pruning guidance:
- Keep this file compact (prefer bullet points).
- When it grows too large, summarize older items and move durable guidance to `AGENTS.md`.
- Before a major prune, append a timestamped snapshot to `context_log.md`.
"""

    try:
        active_context_path.write_text(minimal_content, encoding="utf-8")
        typer.echo(f"Reset active_context.md at {active_context_path}")
    except OSError as exc:
        typer.echo(f"Failed to write {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None


@context_app.command("snapshot")
def pma_context_snapshot(
    path: Optional[Path] = hub_root_path_option(),
):
    """Snapshot active_context.md into context_log.md with ISO timestamp."""
    hub_root = _resolve_hub_path(path)
    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        typer.echo(f"Failed to ensure PMA docs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    active_context_path = _pma_docs_path(hub_root, "active_context.md")
    context_log_path = _pma_docs_path(hub_root, "context_log.md")

    try:
        active_content = active_context_path.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Failed to read {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    timestamp = datetime.now(timezone.utc).isoformat()
    snapshot_header = f"\n\n## Snapshot: {timestamp}\n\n"
    snapshot_content = snapshot_header + active_content

    try:
        with context_log_path.open("a", encoding="utf-8") as f:
            f.write(snapshot_content)
        typer.echo(f"Appended snapshot to {context_log_path}")
    except OSError as exc:
        typer.echo(f"Failed to write {context_log_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None


@context_app.command("prune")
def pma_context_prune(
    path: Optional[Path] = hub_root_path_option(),
):
    """Prune active_context.md if over budget (snapshot first)."""
    hub_root = _resolve_hub_path(path)

    max_lines = 200
    try:
        config = load_hub_config(hub_root)
        pma_cfg = getattr(config, "pma", None)
        if pma_cfg is not None:
            max_lines = int(getattr(pma_cfg, "active_context_max_lines", max_lines))
    except (OSError, ValueError):  # intentional: config fallback
        logger.debug(
            "Failed to read active_context_max_lines from config", exc_info=True
        )

    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        typer.echo(f"Failed to ensure PMA docs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    active_context_path = _pma_docs_path(hub_root, "active_context.md")

    try:
        active_content = active_context_path.read_text(encoding="utf-8")
        line_count = len(active_content.splitlines())
    except OSError as exc:
        typer.echo(f"Failed to read {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if line_count <= max_lines:
        typer.echo(
            f"active_context.md has {line_count} lines (budget: {max_lines}), no prune needed"
        )
        return

    typer.echo(
        f"active_context.md has {line_count} lines (budget: {max_lines}), snapshotting and pruning"
    )

    timestamp = datetime.now(timezone.utc).isoformat()
    snapshot_header = f"\n\n## Snapshot: {timestamp}\n\n"
    snapshot_content = snapshot_header + active_content

    context_log_path = _pma_docs_path(hub_root, "context_log.md")
    try:
        with context_log_path.open("a", encoding="utf-8") as f:
            f.write(snapshot_content)
    except OSError as exc:
        typer.echo(f"Failed to write {context_log_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    minimal_content = f"""# PMA active context (short-lived)

Use this file for the current working set: active projects, open questions, links, and immediate next steps.

Pruning guidance:
- Keep this file compact (prefer bullet points).
- When it grows too large, summarize older items and move durable guidance to `AGENTS.md`.
- Before a major prune, append a timestamped snapshot to `context_log.md`.

> Note: This file was pruned on {timestamp} (had {line_count} lines, budget: {max_lines})
"""

    try:
        active_context_path.write_text(minimal_content, encoding="utf-8")
        typer.echo(f"Pruned active_context.md at {active_context_path}")
    except OSError as exc:
        typer.echo(f"Failed to write {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None


@context_app.command("compact")
def pma_context_compact(
    max_lines: Optional[int] = typer.Option(
        None, "--max-lines", help="Target max lines for active_context.md"
    ),
    summary_lines: int = typer.Option(
        12, "--summary-lines", help="Max archived summary lines to keep"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Snapshot then compact active_context.md into a deterministic short form."""
    hub_root = _resolve_hub_path(path)

    resolved_max_lines = 200
    try:
        config = load_hub_config(hub_root)
        pma_cfg = getattr(config, "pma", None)
        if pma_cfg is not None:
            resolved_max_lines = int(
                getattr(pma_cfg, "active_context_max_lines", resolved_max_lines)
            )
    except (OSError, ValueError):  # intentional: config fallback
        logger.debug(
            "Failed to read active_context_max_lines from config", exc_info=True
        )
    if isinstance(max_lines, int):
        resolved_max_lines = max(1, max_lines)
    else:
        resolved_max_lines = max(1, resolved_max_lines)

    resolved_summary_lines = max(0, int(summary_lines))

    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        typer.echo(f"Failed to ensure PMA docs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    active_context_path = _pma_docs_path(hub_root, "active_context.md")
    context_log_path = _pma_docs_path(hub_root, "context_log.md")

    try:
        active_content = active_context_path.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Failed to read {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    previous_line_count = len(active_content.splitlines())
    timestamp = datetime.now(timezone.utc).isoformat()
    summary_items = _extract_compact_summary_items(
        active_content, limit=resolved_summary_lines
    )
    compacted = _render_compacted_active_context(
        timestamp=timestamp,
        previous_line_count=previous_line_count,
        max_lines=resolved_max_lines,
        summary_items=summary_items,
    )

    if dry_run:
        typer.echo(
            f"Dry run: compact active_context.md (current_lines={previous_line_count}, "
            f"target_max_lines={resolved_max_lines}, summary_lines={resolved_summary_lines})"
        )
        return

    snapshot_header = f"\n\n## Snapshot: {timestamp}\n\n"
    snapshot_content = snapshot_header + active_content
    try:
        with context_log_path.open("a", encoding="utf-8") as f:
            f.write(snapshot_content)
    except OSError as exc:
        typer.echo(f"Failed to write {context_log_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    try:
        active_context_path.write_text(compacted, encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Failed to write {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(
        f"Compacted active_context.md at {active_context_path} "
        f"(lines: {previous_line_count} -> {len(compacted.splitlines())})"
    )


@binding_app.command("list")
def pma_binding_list(
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent"),
    repo_id: Optional[str] = typer.Option(None, "--repo", help="Filter by repo id"),
    resource_kind: Optional[str] = typer.Option(
        None, "--resource-kind", help="Filter by managed resource kind"
    ),
    resource_id: Optional[str] = typer.Option(
        None, "--resource-id", help="Filter by managed resource id"
    ),
    surface_kind: Optional[str] = typer.Option(
        None, "--surface", help="Filter by surface kind (discord, telegram, etc.)"
    ),
    include_disabled: bool = typer.Option(
        False, "--include-disabled", help="Include disabled bindings"
    ),
    limit: int = typer.Option(200, "--limit", min=1, help="Maximum rows to return"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List orchestration bindings for threads."""
    hub_root = _resolve_hub_path(path)
    (
        normalized_resource_kind,
        normalized_resource_id,
        _normalized_workspace_root,
    ) = _normalize_resource_owner_options(
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    params = {
        key: value
        for key, value in {
            "agent": agent,
            "resource_kind": normalized_resource_kind,
            "resource_id": normalized_resource_id,
            "surface_kind": surface_kind,
            "include_disabled": include_disabled,
            "limit": limit,
        }.items()
        if value is not None
    }
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, "/bindings"),
            token_env=config.server_auth_token_env,
            params=params,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    bindings = data.get("bindings", []) if isinstance(data, dict) else []
    if not isinstance(bindings, list) or not bindings:
        typer.echo("No bindings found")
        return
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        disabled = " (disabled)" if binding.get("disabled_at") else ""
        typer.echo(
            " ".join(
                [
                    str(binding.get("binding_id") or "")[:12],
                    f"surface={binding.get('surface_kind') or ''}",
                    f"key={binding.get('surface_key') or ''}",
                    f"thread={binding.get('thread_target_id') or ''}"[:20],
                    f"agent={binding.get('agent_id') or ''}",
                    _format_resource_owner_label(binding),
                ]
            ).strip()
            + disabled
        )


@binding_app.command("active")
def pma_binding_active(
    surface_kind: str = typer.Option(
        ..., "--surface", help="Surface kind (discord, telegram, etc.)"
    ),
    surface_key: str = typer.Option(
        ..., "--key", help="Surface-specific key (channel id, chat id, etc.)"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Get the active thread bound to a surface key."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(
                config,
                f"/bindings/active?surface_kind={surface_kind}&surface_key={surface_key}",
            ),
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    thread_target_id = data.get("thread_target_id")
    if thread_target_id:
        typer.echo(f"Active thread: {thread_target_id}")
    else:
        typer.echo("No active thread for this surface key")


@binding_app.command("work")
def pma_binding_work(
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent"),
    repo_id: Optional[str] = typer.Option(None, "--repo", help="Filter by repo id"),
    resource_kind: Optional[str] = typer.Option(
        None, "--resource-kind", help="Filter by managed resource kind"
    ),
    resource_id: Optional[str] = typer.Option(
        None, "--resource-id", help="Filter by managed resource id"
    ),
    limit: int = typer.Option(200, "--limit", min=1, help="Maximum rows to return"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List busy-work summaries (threads with running or queued work)."""
    hub_root = _resolve_hub_path(path)
    (
        normalized_resource_kind,
        normalized_resource_id,
        _normalized_workspace_root,
    ) = _normalize_resource_owner_options(
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    params = {
        key: value
        for key, value in {
            "agent": agent,
            "resource_kind": normalized_resource_kind,
            "resource_id": normalized_resource_id,
            "limit": limit,
        }.items()
        if value is not None
    }
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, "/bindings/work"),
            token_env=config.server_auth_token_env,
            params=params,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    summaries = data.get("summaries", []) if isinstance(data, dict) else []
    if not isinstance(summaries, list) or not summaries:
        typer.echo("No busy work found")
        return
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        thread_id = summary.get("thread_target_id", "")
        agent_id = summary.get("agent_id", "")
        owner = _format_resource_owner_label(summary)
        lifecycle = summary.get("lifecycle_status", "-")
        runtime = summary.get("runtime_status", "-")
        exec_status = summary.get("execution_status", "-")
        bindings = summary.get("binding_count", 0)
        surfaces = ",".join(summary.get("surface_kinds", []))
        preview = summary.get("message_preview", "")
        if preview:
            preview = preview[:50] + "..." if len(preview) > 50 else preview
        typer.echo(
            f"{thread_id[:12]} agent={agent_id} {owner} "
            f"lifecycle={lifecycle} runtime={runtime} exec={exec_status} "
            f"bindings={bindings} surfaces={surfaces}"
        )
        if preview:
            typer.echo(f"  preview: {preview}")
