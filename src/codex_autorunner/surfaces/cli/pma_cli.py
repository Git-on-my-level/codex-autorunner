"""PMA CLI commands for Project Management Assistant."""

import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from ...core.config import load_hub_config

logger = logging.getLogger(__name__)

pma_app = typer.Typer(add_completion=False)


def _build_pma_url(config, path: str) -> str:
    base_path = config.server_base_path or ""
    if base_path.endswith("/") and path.startswith("/"):
        base_path = base_path[:-1]
    return f"http://{config.server_host}:{config.server_port}{base_path}/hub/pma{path}"


def _resolve_hub_path(path: Optional[Path]) -> Path:
    if path:
        candidate = path
        if candidate.is_dir():
            candidate = candidate / "codex-autorunner.yml"
        if candidate.exists():
            return candidate.parent.parent.resolve()
    return Path.cwd()


def _request_json(
    method: str,
    url: str,
    payload: Optional[dict] = None,
    token_env: Optional[str] = None,
) -> dict:
    import os

    headers = None
    if token_env:
        token = os.environ.get(token_env)
        if token and token.strip():
            headers = {"Authorization": f"Bearer {token.strip()}"}
    response = httpx.request(method, url, json=payload, timeout=30.0, headers=headers)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def _is_json_response_error(data: dict) -> Optional[str]:
    if not isinstance(data, dict):
        return "Unexpected response format"
    if data.get("detail"):
        return str(data["detail"])
    if data.get("error"):
        return str(data["error"])
    return None


def _handle_pma_error(
    data: dict, output_json: bool = False, message: str = "PMA request failed"
) -> None:
    error = _is_json_response_error(data)
    if error:
        if output_json:
            typer.echo(json.dumps({"error": error, "detail": data}, indent=2))
        else:
            typer.echo(f"{message}: {error}", err=True)
        raise typer.Exit(code=1) from None


def _format_output(data: dict, output_json: bool = False) -> None:
    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(json.dumps(data, indent=2))


@pma_app.command("chat")
def pma_chat(
    message: str = typer.Argument(..., help="Message to send to PMA"),
    agent: Optional[str] = typer.Option(
        None, "--agent", help="Agent to use (codex|opencode)"
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Model override"),
    reasoning: Optional[str] = typer.Option(
        None, "--reasoning", help="Reasoning effort override"
    ),
    stream: bool = typer.Option(False, "--stream", help="Stream response tokens"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Send a message to the Project Management Assistant."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
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

        from ...integrations.app_server.event_buffer import parse_sse_line

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
        except Exception as exc:
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
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Interrupt a running PMA chat."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, "/interrupt")

    try:
        data = _request_json("POST", url, token_env=config.server_auth_token_env)
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
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
        None, "--agent", help="Agent thread to reset (opencode|codex|all)"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Reset PMA thread state."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
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
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Show active PMA chat status."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
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
    except Exception as exc:
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


@pma_app.command("agents")
def pma_agents(
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """List available PMA agents."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, "/agents")

    try:
        data = _request_json("GET", url, token_env=config.server_auth_token_env)
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
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
            available = agent.get("available", False)
            status = "available" if available else "unavailable"
            typer.echo(f"  - {agent_name} ({agent_id}): {status}")


@pma_app.command("models")
def pma_models(
    agent: str = typer.Argument(..., help="Agent ID (codex|opencode)"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """List available models for an agent."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, f"/agents/{agent}/models")

    try:
        data = _request_json("GET", url, token_env=config.server_auth_token_env)
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """List files in PMA inbox and outbox."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = _build_pma_url(config, "/files")

    try:
        data = _request_json("GET", url, token_env=config.server_auth_token_env)
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        inbox = data.get("inbox", []) if isinstance(data, dict) else []
        outbox = data.get("outbox", []) if isinstance(data, dict) else []

        typer.echo(f"Inbox ({len(inbox)}):")
        for file in inbox:
            if not isinstance(file, dict):
                continue
            name = file.get("name", "")
            size = file.get("size", 0)
            modified = file.get("modified_at", "")
            typer.echo(f"  - {name} ({size} bytes, {modified})")

        typer.echo(f"\nOutbox ({len(outbox)}):")
        for file in outbox:
            if not isinstance(file, dict):
                continue
            name = file.get("name", "")
            size = file.get("size", 0)
            modified = file.get("modified_at", "")
            typer.echo(f"  - {name} ({size} bytes, {modified})")


@pma_app.command("upload")
def pma_upload(
    box: str = typer.Argument(..., help="Target box (inbox|outbox)"),
    files: list[Path] = typer.Argument(..., help="Files to upload"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Upload files to PMA inbox or outbox."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if box not in ("inbox", "outbox"):
        typer.echo("Box must be 'inbox' or 'outbox'", err=True)
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

    saved_files = []
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
    box: str = typer.Argument(..., help="Source box (inbox|outbox)"),
    filename: str = typer.Argument(..., help="File to download"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Output path (default: current directory)"
    ),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Download a file from PMA inbox or outbox."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if box not in ("inbox", "outbox"):
        typer.echo("Box must be 'inbox' or 'outbox'", err=True)
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
    box: Optional[str] = typer.Argument(None, help="Target box (inbox|outbox)"),
    filename: Optional[str] = typer.Argument(None, help="File to delete"),
    all_files: bool = typer.Option(False, "--all", help="Delete all files in the box"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Delete files from PMA inbox or outbox."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if all_files:
        if not box or box not in ("inbox", "outbox"):
            typer.echo("Box must be 'inbox' or 'outbox' when using --all", err=True)
            raise typer.Exit(code=1) from None
        url = _build_pma_url(config, f"/files/{box}")
        method = "DELETE"
        payload = None
    else:
        if not box or not filename:
            typer.echo("Box and filename are required (or use --all)", err=True)
            raise typer.Exit(code=1) from None
        if box not in ("inbox", "outbox"):
            typer.echo("Box must be 'inbox' or 'outbox'", err=True)
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
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        if all_files:
            typer.echo(f"Deleted all files in {box}")
        else:
            typer.echo(f"Deleted {filename} from {box}")
