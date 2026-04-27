"""PMA CLI commands for Project Management Assistant."""

import json
import logging
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from ...core.config import load_hub_config
from ...core.filebox import BOXES
from ...core.locks import file_lock
from ...core.pma_hygiene import (
    apply_pma_hygiene_report,
    build_pma_hygiene_report,
    hygiene_lock_path,
    render_pma_hygiene_report,
)
from .commands.utils import build_hub_supervisor
from .hub_path_option import hub_root_path_option
from .pma_binding_commands import register_binding_commands
from .pma_context_commands import register_context_commands
from .pma_control_plane import (
    CAPABILITY_REQUIREMENTS,
    build_pma_url,
    check_capability,
    fetch_agent_capabilities,
    request_json,
    resolve_hub_path,
)
from .pma_docs_commands import register_docs_commands
from .pma_file_commands import box_choices_text, register_file_commands
from .pma_status_contracts import (
    PmaActiveResponse,
    PmaInterruptResponse,
    PmaResetResponse,
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

register_docs_commands(docs_app)
register_context_commands(context_app)
register_thread_commands(thread_app)
register_binding_commands(binding_app)
register_file_commands(file_app)

pma_app.add_typer(docs_app)
pma_app.add_typer(context_app)
pma_app.add_typer(thread_app, name="thread")
pma_app.add_typer(binding_app, name="binding")
pma_app.add_typer(file_app, name="file")


def _resolve_hub_path(path: Optional[Path]) -> Path:
    return resolve_hub_path(path)


def _is_json_response_error(data: dict) -> Optional[str]:
    if not isinstance(data, dict):
        return "Unexpected response format"
    if data.get("detail"):
        return str(data["detail"])
    if data.get("error"):
        return str(data["error"])
    return None


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
    hub_root = resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = build_pma_url(config, "/chat")
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
        data = request_json(
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
    hub_root = resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    active_url = build_pma_url(config, "/active")
    try:
        active_data = request_json(
            "GET", active_url, token_env=config.server_auth_token_env
        )
    except (httpx.HTTPError, ValueError, OSError):  # best-effort active data fetch
        active_data = {}
    current = active_data.get("current", {}) if isinstance(active_data, dict) else {}
    if isinstance(current, dict):
        agent = current.get("agent", "")
        if agent:
            capabilities = fetch_agent_capabilities(config, path)
            required_cap = CAPABILITY_REQUIREMENTS.get("interrupt")
            if required_cap and not check_capability(agent, required_cap, capabilities):
                typer.echo(
                    f"Agent '{agent}' does not support interrupt (missing capability: {required_cap})",
                    err=True,
                )
                raise typer.Exit(code=1) from None

    url = build_pma_url(config, "/interrupt")

    try:
        data = request_json("POST", url, token_env=config.server_auth_token_env)
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        response = PmaInterruptResponse.from_dict(data)
        if response.interrupted:
            typer.echo(f"PMA chat interrupted (agent={response.agent})")
        else:
            typer.echo("No active PMA chat to interrupt")
            if response.detail:
                typer.echo(f"Detail: {response.detail}")


@pma_app.command("reset")
def pma_reset(
    agent: Optional[str] = typer.Option(
        None, "--agent", help="Agent thread to reset (opencode|codex|hermes|all)"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Reset PMA thread state."""
    hub_root = resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = build_pma_url(config, "/thread/reset")
    payload: dict[str, Any] = {}
    if agent:
        payload["agent"] = agent

    try:
        data = request_json(
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
        response = PmaResetResponse.from_dict(data)
        if response.cleared:
            typer.echo(f"Cleared threads: {', '.join(response.cleared)}")
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
    hub_root = resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = build_pma_url(config, "/active")
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
        active_response = PmaActiveResponse.from_dict(data)
        typer.echo(f"Active: {active_response.active}")
        if active_response.current:
            snap = active_response.current
            typer.echo(
                f"Current turn: status={snap.status}, agent={snap.agent}, started={snap.started_at}"
            )
        if active_response.last_result:
            snap = active_response.last_result
            typer.echo(
                f"Last result: status={snap.status}, agent={snap.agent}, finished={snap.finished_at}"
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
            "Also apply reviewed needs-confirmation managed-thread/worktree cleanup candidates."
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
    hub_root = resolve_hub_path(path)
    apply_result: Optional[dict[str, Any]] = None
    supervisor = None

    def _cleanup_worktree(worktree_repo_id: str, archive: bool) -> dict[str, Any]:
        nonlocal supervisor
        if supervisor is None:
            supervisor = build_hub_supervisor(load_hub_config(hub_root))
        return supervisor.cleanup_worktree(
            worktree_repo_id=worktree_repo_id,
            archive=archive,
        )

    try:
        lock_ctx = file_lock(hygiene_lock_path(hub_root)) if apply else nullcontext()
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
                    cleanup_worktree=_cleanup_worktree,
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
    hub_root = resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = build_pma_url(config, "/agents")

    try:
        data = request_json("GET", url, token_env=config.server_auth_token_env)
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
    hub_root = resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    capabilities = fetch_agent_capabilities(config, path)
    required_cap = CAPABILITY_REQUIREMENTS.get("models")
    if required_cap and not check_capability(agent, required_cap, capabilities):
        typer.echo(
            f"Agent '{agent}' does not support model listing (missing capability: {required_cap})",
            err=True,
        )
        raise typer.Exit(code=1) from None

    url = build_pma_url(config, f"/agents/{agent}/models")

    try:
        data = request_json("GET", url, token_env=config.server_auth_token_env)
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
    hub_root = resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = build_pma_url(config, "/files")

    try:
        data = request_json("GET", url, token_env=config.server_auth_token_env)
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


@pma_app.command("upload")
def pma_upload(
    box: str = typer.Argument(..., help=f"Target box ({box_choices_text()})"),
    files: list[Path] = typer.Argument(..., help="Files to upload"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Upload files to PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
    hub_root = resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if box not in BOXES:
        typer.echo(f"Box must be one of: {', '.join(BOXES)}", err=True)
        raise typer.Exit(code=1) from None

    url = build_pma_url(config, f"/files/{box}")

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
    box: str = typer.Argument(..., help=f"Source box ({box_choices_text()})"),
    filename: str = typer.Argument(..., help="File to download"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Output path (default: current directory)"
    ),
    path: Optional[Path] = hub_root_path_option(),
):
    """Download a file from PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
    hub_root = resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if box not in BOXES:
        typer.echo(f"Box must be one of: {', '.join(BOXES)}", err=True)
        raise typer.Exit(code=1) from None

    url = build_pma_url(config, f"/files/{box}/{filename}")

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
        None, help=f"Target box ({box_choices_text()})"
    ),
    filename: Optional[str] = typer.Argument(None, help="File to delete"),
    all_files: bool = typer.Option(False, "--all", help="Delete all files in the box"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Delete files from PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
    hub_root = resolve_hub_path(path)
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
        url = build_pma_url(config, f"/files/{box}")
        method = "DELETE"
        payload = None
    else:
        if not box or not filename:
            typer.echo("Box and filename are required (or use --all)", err=True)
            raise typer.Exit(code=1) from None
        if box not in BOXES:
            typer.echo(f"Box must be one of: {', '.join(BOXES)}", err=True)
            raise typer.Exit(code=1) from None
        url = build_pma_url(config, f"/files/{box}/{filename}")
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
