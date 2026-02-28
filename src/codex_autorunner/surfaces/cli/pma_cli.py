"""PMA CLI commands for Project Management Assistant."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from ...bootstrap import ensure_pma_docs, pma_doc_path
from ...core.config import load_hub_config
from ...core.pma_delivery_targets import (
    PmaDeliveryTargetsStore,
    parse_delivery_target_ref,
    pma_delivery_target_ref_usage,
    target_key,
)

logger = logging.getLogger(__name__)

pma_app = typer.Typer(add_completion=False, rich_markup_mode=None)
docs_app = typer.Typer(add_completion=False, rich_markup_mode=None, name="docs")
context_app = typer.Typer(add_completion=False, rich_markup_mode=None, name="context")
thread_app = typer.Typer(add_completion=False, rich_markup_mode=None, name="thread")
targets_app = typer.Typer(add_completion=False, rich_markup_mode=None, name="targets")
pma_app.add_typer(docs_app)
pma_app.add_typer(context_app)
pma_app.add_typer(thread_app, name="thread")
pma_app.add_typer(targets_app, name="targets")

_TARGET_REF_ARG_HELP = (
    "Target ref (e.g. here, telegram:-100123:777, discord:123456789012345678, "
    "chat:telegram:-100123:777, local:./notes/pma.md, web). "
    "Local path refs must resolve within the hub root."
)


def _pma_docs_path(hub_root: Path, doc_name: str) -> Path:
    return pma_doc_path(hub_root, doc_name)


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
            if not candidate.exists():
                candidate = path / ".codex-autorunner" / "config.yml"
        if candidate.exists():
            if (
                candidate.name == "config.yml"
                and candidate.parent.name == ".codex-autorunner"
            ):
                return candidate.parent.parent.resolve()
            return candidate.parent.resolve()
    return Path.cwd()


def _parse_pma_target_ref(ref: str) -> Optional[dict[str, Any]]:
    return parse_delivery_target_ref(ref)


def _format_pma_target_label(target: dict[str, Any]) -> str:
    label = target.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    kind = target.get("kind")
    if kind == "web":
        return "web target"
    if kind == "local":
        path = target.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()
        return ""
    if kind == "chat":
        platform = target.get("platform")
        chat_id = target.get("chat_id")
        thread_id = target.get("thread_id")
        if isinstance(platform, str) and isinstance(chat_id, str):
            if isinstance(thread_id, str) and thread_id:
                return f"{platform}:{chat_id}:{thread_id}"
            return f"{platform}:{chat_id}"
    return ""


def _pma_targets_usage() -> str:
    return "\n".join(
        [
            "Usage:",
            "targets add <ref>",
            "targets rm <ref>",
            "targets clear",
            "Local path refs must resolve within the hub root.",
            pma_delivery_target_ref_usage(multiline=True),
        ]
    )


def _request_json(
    method: str,
    url: str,
    payload: Optional[dict] = None,
    token_env: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
) -> dict:
    import os

    headers = None
    if token_env:
        token = os.environ.get(token_env)
        if token and token.strip():
            headers = {"Authorization": f"Bearer {token.strip()}"}
    response = httpx.request(
        method,
        url,
        json=payload,
        params=params,
        timeout=30.0,
        headers=headers,
    )
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
                    try:
                        data = {"raw": data_str}
                    except Exception:
                        data = {}
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


@targets_app.command("list")
def pma_targets_list(
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """List PMA delivery targets."""
    hub_root = _resolve_hub_path(path)
    store = PmaDeliveryTargetsStore(hub_root)
    state = store.load()
    targets = [
        target for target in state.get("targets", []) if isinstance(target, dict)
    ]

    rows: list[dict[str, Any]] = []
    for target in targets:
        key = target_key(target)
        if not isinstance(key, str):
            continue
        rows.append(
            {
                "key": key,
                "label": _format_pma_target_label(target),
                "target": target,
            }
        )

    if output_json:
        typer.echo(json.dumps({"targets": rows}, indent=2))
        return

    if not rows:
        typer.echo("PMA delivery targets: (none)")
        return
    lines = ["PMA delivery targets:"]
    for row in rows:
        key = row["key"]
        label = row.get("label")
        if isinstance(label, str) and label:
            lines.append(f"- {key} ({label})")
        else:
            lines.append(f"- {key}")
    typer.echo("\n".join(lines))


@targets_app.command("add")
def pma_targets_add(
    ref: str = typer.Argument(..., help=_TARGET_REF_ARG_HELP),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Add a PMA delivery target.

    Local path refs must resolve within the hub root.
    """
    target = _parse_pma_target_ref(ref)
    if target is None:
        typer.echo(f"Invalid target ref '{ref}'.\n{_pma_targets_usage()}", err=True)
        raise typer.Exit(code=1) from None

    key = target_key(target)
    if not isinstance(key, str):
        typer.echo(f"Invalid target ref '{ref}'.", err=True)
        raise typer.Exit(code=1) from None

    hub_root = _resolve_hub_path(path)
    store = PmaDeliveryTargetsStore(hub_root)
    store.add_target(target)
    typer.echo(f"Added PMA delivery target: {key}")


@targets_app.command("rm")
def pma_targets_rm(
    ref: str = typer.Argument(..., help=_TARGET_REF_ARG_HELP),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Remove a PMA delivery target.

    Local path refs must resolve within the hub root.
    """
    target = _parse_pma_target_ref(ref)
    if target is None:
        typer.echo(f"Invalid target ref '{ref}'.\n{_pma_targets_usage()}", err=True)
        raise typer.Exit(code=1) from None

    key = target_key(target)
    if not isinstance(key, str):
        typer.echo(f"Invalid target ref '{ref}'.", err=True)
        raise typer.Exit(code=1) from None

    hub_root = _resolve_hub_path(path)
    store = PmaDeliveryTargetsStore(hub_root)
    removed = store.remove_target(target)
    if removed:
        typer.echo(f"Removed PMA delivery target: {key}")
    else:
        typer.echo(f"PMA delivery target not found: {key}")


@targets_app.command("clear")
def pma_targets_clear(
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Clear all PMA delivery targets."""
    hub_root = _resolve_hub_path(path)
    store = PmaDeliveryTargetsStore(hub_root)
    store.set_targets([])
    typer.echo("Cleared PMA delivery targets.")


@thread_app.command("spawn")
@thread_app.command("create")
def pma_thread_spawn(
    agent: str = typer.Option(
        ..., "--agent", help="Thread agent to use (codex|opencode)"
    ),
    repo_id: Optional[str] = typer.Option(
        None, "--repo", help="Hub repo id for the target workspace"
    ),
    workspace_root: Optional[str] = typer.Option(
        None, "--workspace-root", help="Absolute or hub-relative workspace path"
    ),
    name: Optional[str] = typer.Option(None, "--name", help="Optional thread label"),
    backend_id: Optional[str] = typer.Option(
        None, "--backend-id", help="Optional existing backend thread/session id"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Create a managed PMA thread."""
    if agent not in {"codex", "opencode"}:
        typer.echo("--agent must be one of: codex, opencode", err=True)
        raise typer.Exit(code=1) from None
    if bool(repo_id) == bool(workspace_root):
        typer.echo("Exactly one of --repo or --workspace-root is required", err=True)
        raise typer.Exit(code=1) from None

    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "POST",
            _build_pma_url(config, "/threads"),
            {
                "agent": agent,
                "repo_id": repo_id,
                "workspace_root": workspace_root,
                "name": name,
                "backend_thread_id": backend_id,
            },
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    thread = data.get("thread", {}) if isinstance(data, dict) else {}
    if not isinstance(thread, dict) or not thread.get("managed_thread_id"):
        typer.echo("Failed to create managed thread", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(str(thread.get("managed_thread_id")))


@thread_app.command("list")
def pma_thread_list(
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent"),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status"),
    repo_id: Optional[str] = typer.Option(None, "--repo", help="Filter by repo id"),
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum rows to return"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """List managed PMA threads."""
    hub_root = _resolve_hub_path(path)
    limit = min(limit, 50)
    params = {
        key: value
        for key, value in {
            "agent": agent,
            "status": status,
            "repo_id": repo_id,
            "limit": limit,
        }.items()
        if value is not None
    }
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, "/threads"),
            token_env=config.server_auth_token_env,
            params=params,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    threads = data.get("threads", []) if isinstance(data, dict) else []
    if not isinstance(threads, list) or not threads:
        typer.echo("No managed threads found")
        return
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        typer.echo(
            " ".join(
                [
                    str(thread.get("managed_thread_id") or ""),
                    f"agent={thread.get('agent') or ''}",
                    f"status={thread.get('status') or ''}",
                    f"repo={thread.get('repo_id') or '-'}",
                ]
            ).strip()
        )


@thread_app.command("info")
def pma_thread_info(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Show managed PMA thread details."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, f"/threads/{managed_thread_id}"),
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    thread = data.get("thread", {}) if isinstance(data, dict) else {}
    if not isinstance(thread, dict):
        typer.echo("Thread not found", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(thread, indent=2))


@thread_app.command("send")
def pma_thread_send(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    message: str = typer.Option(
        ..., "--message", help="User message to send", show_default=False
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Model override"),
    reasoning: Optional[str] = typer.Option(
        None, "--reasoning", help="Reasoning override"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Send a message to a managed PMA thread."""
    payload: dict[str, Any] = {"message": message}
    if model:
        payload["model"] = model
    if reasoning:
        payload["reasoning"] = reasoning

    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/messages"),
            payload,
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    status = (data.get("status") if isinstance(data, dict) else "") or ""
    if status != "ok":
        if output_json:
            typer.echo(json.dumps(data, indent=2))
        else:
            detail = (
                data.get("error")
                if isinstance(data, dict)
                else "Managed thread send failed"
            ) or "Managed thread send failed"
            typer.echo(str(detail), err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(str(data.get("assistant_text") or ""))


@thread_app.command("turns")
def pma_thread_turns(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum rows to return"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """List managed PMA thread turns."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, f"/threads/{managed_thread_id}/turns"),
            token_env=config.server_auth_token_env,
            params={"limit": limit},
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    turns = data.get("turns", []) if isinstance(data, dict) else []
    if not isinstance(turns, list) or not turns:
        typer.echo("No turns found")
        return
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        typer.echo(
            " ".join(
                [
                    str(turn.get("managed_turn_id") or ""),
                    f"status={turn.get('status') or ''}",
                    f"started={turn.get('started_at') or ''}",
                    f"finished={turn.get('finished_at') or ''}",
                ]
            ).strip()
        )


@thread_app.command("output")
def pma_thread_output(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Print assistant_text for the latest turn of a managed PMA thread."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        turns_data = _request_json(
            "GET",
            _build_pma_url(config, f"/threads/{managed_thread_id}/turns"),
            token_env=config.server_auth_token_env,
            params={"limit": 1},
        )
        turns = turns_data.get("turns", []) if isinstance(turns_data, dict) else []
        if not isinstance(turns, list) or not turns:
            typer.echo("No turns found", err=True)
            raise typer.Exit(code=1) from None
        latest_turn = turns[0] if isinstance(turns[0], dict) else {}
        latest_turn_id = latest_turn.get("managed_turn_id") if latest_turn else None
        if not isinstance(latest_turn_id, str) or not latest_turn_id:
            typer.echo("Failed to resolve latest turn id", err=True)
            raise typer.Exit(code=1) from None
        turn_data = _request_json(
            "GET",
            _build_pma_url(
                config, f"/threads/{managed_thread_id}/turns/{latest_turn_id}"
            ),
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    turn = turn_data.get("turn", {}) if isinstance(turn_data, dict) else {}
    assistant_text = turn.get("assistant_text") if isinstance(turn, dict) else ""
    typer.echo(str(assistant_text or ""))


@thread_app.command("compact")
def pma_thread_compact(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    summary: str = typer.Option(..., "--summary", help="Compaction summary"),
    no_reset_backend: bool = typer.Option(
        False, "--no-reset-backend", help="Preserve backend thread/session id"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Store a compaction seed on a managed PMA thread."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/compact"),
            {"summary": summary, "reset_backend": (not no_reset_backend)},
            token_env=config.server_auth_token_env,
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
        typer.echo(f"Compacted {managed_thread_id}")


@thread_app.command("resume")
def pma_thread_resume(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    backend_id: str = typer.Option(
        ..., "--backend-id", help="Backend thread/session id to bind"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Bind a backend thread/session id and set managed thread active."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/resume"),
            {"backend_thread_id": backend_id},
            token_env=config.server_auth_token_env,
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
        typer.echo(f"Resumed {managed_thread_id}")


@thread_app.command("archive")
def pma_thread_archive(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Archive a managed PMA thread."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/archive"),
            token_env=config.server_auth_token_env,
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
        typer.echo(f"Archived {managed_thread_id}")


@thread_app.command("interrupt")
def pma_thread_interrupt(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Interrupt a running managed PMA thread turn."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/interrupt"),
            token_env=config.server_auth_token_env,
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
        typer.echo(f"Interrupted {managed_thread_id}")


@pma_app.command("files")
def pma_files(
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """List files in PMA inbox and outbox (FileBox-backed via /hub/pma/files)."""
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
            source = file.get("source", "")
            source_str = f", source={source}" if source else ""
            typer.echo(f"  - {name} ({size} bytes, {modified}{source_str})")

        typer.echo(f"\nOutbox ({len(outbox)}):")
        for file in outbox:
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
    box: str = typer.Argument(..., help="Target box (inbox|outbox)"),
    files: list[Path] = typer.Argument(..., help="Files to upload"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Upload files to PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
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
    box: str = typer.Argument(..., help="Source box (inbox|outbox)"),
    filename: str = typer.Argument(..., help="File to download"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Output path (default: current directory)"
    ),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Download a file from PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
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
    """Delete files from PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
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


@docs_app.command("show")
def pma_docs_show(
    doc_type: str = typer.Argument(..., help="Document type: agents, active, or log"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Show PMA docs content to stdout."""
    hub_root = _resolve_hub_path(path)
    try:
        ensure_pma_docs(hub_root)
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Reset active_context.md to a minimal header."""
    hub_root = _resolve_hub_path(path)
    try:
        ensure_pma_docs(hub_root)
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Snapshot active_context.md into context_log.md with ISO timestamp."""
    hub_root = _resolve_hub_path(path)
    try:
        ensure_pma_docs(hub_root)
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Prune active_context.md if over budget (snapshot first)."""
    hub_root = _resolve_hub_path(path)

    max_lines = 200
    try:
        config = load_hub_config(hub_root)
        pma_cfg = getattr(config, "pma", None)
        if pma_cfg is not None:
            max_lines = int(getattr(pma_cfg, "active_context_max_lines", max_lines))
    except Exception:
        pass

    try:
        ensure_pma_docs(hub_root)
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
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
    except Exception:
        pass
    if isinstance(max_lines, int):
        resolved_max_lines = max(1, max_lines)
    else:
        resolved_max_lines = max(1, resolved_max_lines)

    resolved_summary_lines = max(0, int(summary_lines))

    try:
        ensure_pma_docs(hub_root)
    except Exception as exc:
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
