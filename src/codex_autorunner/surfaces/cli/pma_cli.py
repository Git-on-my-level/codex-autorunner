"""PMA CLI commands for Project Management Assistant."""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from ...agents.registry import get_registered_agents
from ...bootstrap import ensure_pma_docs, pma_doc_path
from ...core.car_context import (
    default_managed_thread_context_profile,
    normalize_car_context_profile,
)
from ...core.config import load_hub_config
from ...core.filebox import BOXES, list_filebox
from ...core.filebox_lifecycle import (
    LIFECYCLE_BOXES,
    consume_inbox_file,
    dismiss_inbox_file,
    list_consumed_files,
    unconsume_inbox_file,
)
from ...core.pma_audit import PmaActionType, PmaAuditEntry, PmaAuditLog
from ...core.pma_hygiene import (
    apply_pma_hygiene_report,
    build_pma_hygiene_report,
    render_pma_hygiene_report,
)
from .commands.utils import format_hub_request_error

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
pma_app.add_typer(thread_app, name="thread")
pma_app.add_typer(binding_app, name="binding")
pma_app.add_typer(file_app, name="file")


def _pma_docs_path(hub_root: Path, doc_name: str) -> Path:
    return pma_doc_path(hub_root, doc_name)


def _build_pma_url(config, path: str) -> str:
    base_path = config.server_base_path or ""
    if base_path.endswith("/") and path.startswith("/"):
        base_path = base_path[:-1]
    return f"http://{config.server_host}:{config.server_port}{base_path}/hub/pma{path}"


def _resolve_hub_path(path: Optional[Path]) -> Path:
    start = path or Path.cwd()
    try:
        return load_hub_config(start).root
    except Exception:
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


def _resolve_message_body(
    *,
    message: Optional[str],
    message_file: Optional[Path],
    message_stdin: bool,
    option_hint: str,
) -> str:
    selected_inputs = sum(
        1
        for selected in (
            message is not None,
            message_file is not None,
            message_stdin,
        )
        if selected
    )
    if selected_inputs != 1:
        raise typer.BadParameter(
            f"Provide exactly one of {option_hint}.",
            param_hint="--message / --message-file / --message-stdin",
        )

    if message_file is not None:
        try:
            raw_message = message_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(
                f"Failed to read message file: {exc}",
                param_hint="--message-file",
            ) from exc
    elif message_stdin:
        raw_message = sys.stdin.read()
    else:
        raw_message = message or ""

    if not raw_message.strip():
        raise typer.BadParameter("Message cannot be empty.")
    return raw_message


def _echo_delivered_message(message: str) -> None:
    typer.echo("delivered message:")
    typer.echo(message, nl=False)
    if not message.endswith("\n"):
        typer.echo()


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


def _auth_headers_from_env(token_env: Optional[str]) -> Optional[dict[str, str]]:
    if not token_env:
        return None
    import os

    token = os.environ.get(token_env)
    if token and token.strip():
        return {"Authorization": f"Bearer {token.strip()}"}
    return None


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


def _iter_sse_events(lines):
    event_name = "message"
    data_lines: list[str] = []
    event_id: Optional[str] = None
    for line in lines:
        if line is None:
            continue
        if line == "":
            if data_lines or event_id is not None:
                data = "\n".join(data_lines)
                yield event_name, data, event_id
            event_name = "message"
            data_lines = []
            event_id = None
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "event":
            event_name = value or "message"
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            event_id = value


def _format_seconds(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"
    value = max(0, int(seconds))
    if value < 60:
        return f"{value}s"
    minutes, sec = divmod(value, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, rem_minutes = divmod(minutes, 60)
    return f"{hours}h{rem_minutes:02d}m"


def _box_choices_text() -> str:
    return "|".join(BOXES)


def _file_lifecycle_box_choices_text() -> str:
    return "inbox|consumed|dismissed|all"


def _format_tail_event_line(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "event")
    event_id = event.get("event_id")
    summary = str(event.get("summary") or "")
    timestamp = str(event.get("received_at") or "")
    ts_out = timestamp
    if timestamp:
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            ts_out = dt.strftime("%H:%M:%S")
        except ValueError:
            ts_out = timestamp
    prefix = f"[{ts_out}] " if ts_out else ""
    id_part = f"#{event_id} " if isinstance(event_id, int) and event_id > 0 else ""
    return f"{prefix}{id_part}{event_type}: {summary}".rstrip()


def _format_received_at_label(value: Any) -> str:
    timestamp = str(value or "").strip()
    if not timestamp:
        return "-"
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    return dt.strftime("%H:%M:%S")


def _render_active_turn_diagnostics(data: dict[str, Any]) -> None:
    request_kind = str(data.get("request_kind") or "-")
    model = str(data.get("model") or "-")
    reasoning = str(data.get("reasoning") or "-")
    stalled = "yes" if bool(data.get("stalled")) else "no"
    stream = "yes" if bool(data.get("stream_available")) else "no"
    typer.echo(
        "active_turn: "
        f"kind={request_kind} model={model} reasoning={reasoning} "
        f"stream={stream} stalled={stalled}"
    )
    prompt_preview = str(data.get("prompt_preview") or "").strip()
    if prompt_preview:
        typer.echo(f"prompt: {prompt_preview}")
    last_event_type = str(data.get("last_event_type") or "").strip()
    last_event_summary = str(data.get("last_event_summary") or "").strip()
    if last_event_type or last_event_summary:
        typer.echo(
            "last_event: "
            + (last_event_type or "-")
            + " @"
            + _format_received_at_label(data.get("last_event_at"))
            + (f" {last_event_summary}" if last_event_summary else "")
        )
    backend_thread_id = str(data.get("backend_thread_id") or "").strip()
    backend_turn_id = str(data.get("backend_turn_id") or "").strip()
    if backend_thread_id or backend_turn_id:
        typer.echo(
            f"backend: thread={backend_thread_id or '-'} turn={backend_turn_id or '-'}"
        )
    stall_reason = str(data.get("stall_reason") or "").strip()
    if stall_reason:
        typer.echo(f"stall_reason: {stall_reason}")


def _render_tail_snapshot(snapshot: dict[str, Any]) -> None:
    managed_turn_id = snapshot.get("managed_turn_id") or "-"
    status = snapshot.get("turn_status") or "none"
    activity = snapshot.get("activity") or "idle"
    phase = snapshot.get("phase") or "-"
    elapsed = _format_seconds(snapshot.get("elapsed_seconds"))
    idle = _format_seconds(snapshot.get("idle_seconds"))
    typer.echo(
        f"turn={managed_turn_id} status={status} activity={activity} phase={phase} elapsed={elapsed} idle={idle}"
    )
    guidance = str(snapshot.get("guidance") or "").strip()
    if guidance:
        typer.echo(f"guidance: {guidance}")
    diagnostics = snapshot.get("active_turn_diagnostics")
    if isinstance(diagnostics, dict):
        _render_active_turn_diagnostics(diagnostics)
    last_tool = snapshot.get("last_tool")
    if isinstance(last_tool, dict) and str(last_tool.get("name") or "").strip():
        typer.echo(
            "last_tool="
            + str(last_tool.get("name") or "")
            + " status="
            + str(last_tool.get("status") or "-")
            + " in_flight="
            + ("yes" if bool(last_tool.get("in_flight")) else "no")
        )
    lifecycle = snapshot.get("lifecycle_events")
    if isinstance(lifecycle, list) and lifecycle:
        typer.echo("lifecycle: " + ", ".join(str(item) for item in lifecycle))
    events = snapshot.get("events")
    if not isinstance(events, list) or not events:
        typer.echo("No tail events.")
        if status == "running" and snapshot.get("idle_seconds") is not None:
            idle_seconds = int(snapshot.get("idle_seconds") or 0)
            if idle_seconds >= 30:
                typer.echo(f"No events for {idle_seconds}s (possibly stalled).")
        return
    for event in events:
        if isinstance(event, dict):
            typer.echo(_format_tail_event_line(event))


def _normalize_notify_on(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip().lower()
    if not text:
        return None
    if text != "terminal":
        raise typer.BadParameter("notify-on must be 'terminal'")
    return text


_CAPABILITY_REQUIREMENTS = {
    "models": "model_listing",
    "interrupt": "interrupt",
    "thread_interrupt": "interrupt",
    "thread_send": "message_turns",
    "thread_turns": "transcript_history",
    "thread_output": "transcript_history",
    "thread_tail": "event_streaming",
    "thread_compact": "message_turns",
    "thread_resume": "durable_threads",
    "thread_archive": "durable_threads",
    "thread_spawn": "durable_threads",
    "review": "review",
}


def _fetch_agent_capabilities(
    config, path: Optional[Path] = None
) -> dict[str, list[str]]:
    url = _build_pma_url(config, "/agents")
    try:
        data = _request_json("GET", url, token_env=config.server_auth_token_env)
    except Exception:
        return {}
    agents = data.get("agents", []) if isinstance(data, dict) else []
    return {
        agent.get("id", ""): agent.get("capabilities", [])
        for agent in agents
        if isinstance(agent, dict)
    }


def _check_capability(
    agent_id: str,
    capability: str,
    capabilities: dict[str, list[str]],
) -> bool:
    agent_caps = capabilities.get(agent_id, [])
    return capability in agent_caps


def _request_json_with_status(
    method: str,
    url: str,
    payload: Optional[dict[str, Any]] = None,
    token_env: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    headers = _auth_headers_from_env(token_env)
    response = httpx.request(
        method,
        url,
        json=payload,
        params=params,
        headers=headers,
        timeout=timeout,
    )
    data: dict[str, Any] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        data = {}
    return response.status_code, data


def _normalize_agent_option(agent: Optional[str]) -> Optional[str]:
    if agent is None:
        return None
    normalized = agent.strip().lower()
    if not normalized:
        return None
    registered = get_registered_agents()
    if normalized not in registered:
        available = ", ".join(sorted(registered.keys()))
        typer.echo(
            f"--agent must be a registered agent. Available: {available}",
            err=True,
        )
        raise typer.Exit(code=1) from None
    return normalized


def _normalize_resource_owner_options(
    *,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    workspace_root: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    normalized_repo_id = (
        repo_id.strip() if isinstance(repo_id, str) and repo_id.strip() else None
    )
    normalized_resource_kind = (
        resource_kind.strip().lower()
        if isinstance(resource_kind, str) and resource_kind.strip()
        else None
    )
    normalized_resource_id = (
        resource_id.strip()
        if isinstance(resource_id, str) and resource_id.strip()
        else None
    )
    normalized_workspace_root = (
        workspace_root.strip()
        if isinstance(workspace_root, str) and workspace_root.strip()
        else None
    )
    repo_present = normalized_repo_id is not None
    resource_present = (
        normalized_resource_kind is not None or normalized_resource_id is not None
    )
    workspace_present = normalized_workspace_root is not None

    if normalized_resource_id and normalized_resource_kind is None:
        typer.echo(
            "--resource-kind is required when --resource-id is provided", err=True
        )
        raise typer.Exit(code=1) from None
    if normalized_resource_kind and normalized_resource_id is None:
        typer.echo(
            "--resource-id is required when --resource-kind is provided", err=True
        )
        raise typer.Exit(code=1) from None
    if normalized_resource_kind not in {None, "repo", "agent_workspace"}:
        typer.echo("--resource-kind must be one of: repo, agent_workspace", err=True)
        raise typer.Exit(code=1) from None
    if normalized_repo_id and normalized_resource_kind not in {None, "repo"}:
        typer.echo(
            "--repo cannot be combined with a non-repo --resource-kind",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if (
        normalized_repo_id
        and normalized_resource_id
        and normalized_resource_id != normalized_repo_id
    ):
        typer.echo("--repo must match --resource-id for repo-backed requests", err=True)
        raise typer.Exit(code=1) from None
    if (
        sum(
            1
            for present in (repo_present, resource_present, workspace_present)
            if present
        )
        > 1
    ):
        typer.echo(
            "Choose exactly one of --repo, --resource-kind/--resource-id, or --workspace-root",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if normalized_repo_id and normalized_resource_kind is None:
        normalized_resource_kind = "repo"
        normalized_resource_id = normalized_repo_id

    return (
        normalized_resource_kind,
        normalized_resource_id,
        normalized_workspace_root,
    )


def _format_resource_owner_label(item: dict[str, Any]) -> str:
    resource_kind = str(item.get("resource_kind") or "").strip()
    resource_id = str(item.get("resource_id") or "").strip()
    repo_id = str(item.get("repo_id") or "").strip()
    if resource_kind and resource_id:
        if resource_kind == "repo":
            return f"repo={resource_id}"
        return f"owner={resource_kind}:{resource_id}"
    if repo_id:
        return f"repo={repo_id}"
    workspace_root = str(item.get("workspace_root") or "").strip()
    if workspace_root:
        return f"workspace={workspace_root}"
    return "owner=-"


def _normalize_thread_compact_scope(
    *,
    managed_thread_id: Optional[str],
    status: Optional[str],
    all_threads: bool,
) -> tuple[Optional[str], bool]:
    normalized_id = str(managed_thread_id or "").strip() or None
    normalized_status = str(status or "").strip().lower() or None
    scope_all = all_threads or normalized_status == "all"
    if normalized_id is not None:
        if normalized_status is not None or all_threads:
            raise typer.BadParameter(
                "Use either --id or bulk selection flags, not both.",
                param_hint="--id",
            )
        return normalized_id, False
    if normalized_status is None and not scope_all:
        raise typer.BadParameter(
            "Provide --id, --status, or --all.",
            param_hint="--id / --status / --all",
        )
    return normalized_status, scope_all


def _thread_compact_target_line(thread: dict[str, Any]) -> str:
    parts = [
        str(thread.get("managed_thread_id") or ""),
        f"agent={thread.get('agent') or ''}",
        f"status={thread.get('status') or ''}",
        f"reason={thread.get('status_reason') or '-'}",
        _format_resource_owner_label(thread),
    ]
    if bool(thread.get("chat_bound")):
        parts.append("chat_bound=yes")
    if bool(thread.get("cleanup_protected")):
        parts.append("protected=yes")
    return " ".join(part for part in parts if part).strip()


def _record_thread_compact_audit(
    *,
    hub_root: Path,
    summary: str,
    managed_thread_ids: list[str],
    mode: str,
    reset_backend: bool,
    status: Optional[str],
    scope_all: bool,
    resource_kind: Optional[str],
    resource_id: Optional[str],
    agent: Optional[str],
    errors: Optional[list[dict[str, Any]]] = None,
) -> None:
    try:
        PmaAuditLog(hub_root).append(
            PmaAuditEntry(
                action_type=PmaActionType.SESSION_COMPACT,
                thread_id=(
                    managed_thread_ids[0] if len(managed_thread_ids) == 1 else None
                ),
                agent=agent,
                status="error" if errors else "ok",
                error="; ".join(
                    str(item.get("error") or "").strip()
                    for item in (errors or [])
                    if str(item.get("error") or "").strip()
                )
                or None,
                details={
                    "command": "managed_thread_compact",
                    "mode": mode,
                    "thread_count": len(managed_thread_ids),
                    "thread_ids": managed_thread_ids,
                    "summary_length": len(summary),
                    "reset_backend": reset_backend,
                    "status_filter": status,
                    "all_threads": scope_all,
                    "resource_kind": resource_kind,
                    "resource_id": resource_id,
                    "errors": errors or [],
                },
            )
        )
    except Exception as exc:
        logger.warning("Failed to record PMA thread compact audit entry: %s", exc)


def _render_thread_status_snapshot(data: dict[str, Any]) -> None:
    from ...core.managed_thread_status import derive_managed_thread_operator_status

    raw_thread = data.get("thread")
    thread: dict[str, Any] = raw_thread if isinstance(raw_thread, dict) else {}
    raw_turn = data.get("turn")
    turn: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else {}
    managed_thread_id = str(data.get("managed_thread_id") or "")
    agent = str(thread.get("agent") or "-")
    owner = _format_resource_owner_label(thread)
    raw_thread_status = str(data.get("status") or thread.get("status") or "-")
    lifecycle_state = str(thread.get("lifecycle_status") or "-")
    operator_status = derive_managed_thread_operator_status(
        normalized_status=raw_thread_status,
        lifecycle_status=lifecycle_state,
    )
    last_turn_outcome = (
        raw_thread_status
        if raw_thread_status in {"completed", "interrupted", "failed"}
        else "-"
    )
    status_reason = str(data.get("status_reason") or thread.get("status_reason") or "-")
    managed_turn_id = str(turn.get("managed_turn_id") or "-")
    turn_state = str(turn.get("status") or "-")
    activity = str(turn.get("activity") or "-")
    phase = str(turn.get("phase") or "-")
    elapsed = _format_seconds(turn.get("elapsed_seconds"))
    idle = _format_seconds(turn.get("idle_seconds"))
    alive = "yes" if bool(data.get("is_alive")) else "no"
    typer.echo(
        " ".join(
            [
                f"id={managed_thread_id}",
                f"agent={agent}",
                owner,
                f"status={operator_status}",
                f"last_turn={last_turn_outcome}",
                f"alive={alive}",
            ]
        )
    )
    typer.echo(f"reason={status_reason}")
    typer.echo(
        f"turn={managed_turn_id} status={turn_state} activity={activity} phase={phase} elapsed={elapsed} idle={idle}"
    )
    guidance = str(turn.get("guidance") or "").strip()
    if guidance:
        typer.echo(f"guidance: {guidance}")
    diagnostics = data.get("active_turn_diagnostics")
    if isinstance(diagnostics, dict):
        _render_active_turn_diagnostics(diagnostics)
    last_tool = turn.get("last_tool")
    if isinstance(last_tool, dict) and str(last_tool.get("name") or "").strip():
        typer.echo(
            "last_tool="
            + str(last_tool.get("name") or "")
            + " status="
            + str(last_tool.get("status") or "-")
            + " in_flight="
            + ("yes" if bool(last_tool.get("in_flight")) else "no")
        )
    progress = data.get("recent_progress")
    if isinstance(progress, list) and progress:
        typer.echo("recent progress:")
        for event in progress:
            if isinstance(event, dict):
                typer.echo(_format_tail_event_line(event))
    else:
        typer.echo("No recent progress events.")
    excerpt = str(data.get("latest_output_excerpt") or "").strip()
    queue_depth = int(data.get("queue_depth") or 0)
    if queue_depth > 0:
        typer.echo(f"queued={queue_depth}")
        queued_turns = data.get("queued_turns")
        if isinstance(queued_turns, list):
            for item in queued_turns[:5]:
                if not isinstance(item, dict):
                    continue
                typer.echo(
                    "queued_turn="
                    + str(item.get("managed_turn_id") or "-")
                    + " enqueued="
                    + str(item.get("enqueued_at") or "-")
                    + " prompt="
                    + str(item.get("prompt_preview") or "")[:80]
                )
    if excerpt:
        typer.echo("latest output:")
        typer.echo(excerpt)


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

    active_url = _build_pma_url(config, "/active")
    try:
        active_data = _request_json(
            "GET", active_url, token_env=config.server_auth_token_env
        )
    except Exception:
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
        None, "--agent", help="Agent thread to reset (opencode|codex|hermes|all)"
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
    stale_threshold_seconds: Optional[int] = typer.Option(
        None,
        "--stale-threshold-seconds",
        min=1,
        help="Override the stale-age threshold used for PMA hygiene classification.",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Review PMA hygiene candidates and optionally clean only the safe ones."""
    hub_root = _resolve_hub_path(path)
    try:
        report = build_pma_hygiene_report(
            hub_root,
            categories=category or None,
            stale_threshold_seconds=stale_threshold_seconds,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"Failed to build PMA hygiene report: {exc}", err=True)
        raise typer.Exit(code=1) from None

    apply_result: Optional[dict[str, Any]] = None
    if apply:
        try:
            apply_result = apply_pma_hygiene_report(hub_root, report)
        except Exception as exc:
            typer.echo(f"Failed to apply PMA hygiene cleanup: {exc}", err=True)
            raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps({"report": report, "apply": apply_result}, indent=2))
        return

    typer.echo(render_pma_hygiene_report(report, apply=apply))
    if apply_result is not None:
        typer.echo(
            "Applied safe cleanup: "
            f"attempted={apply_result['attempted']} "
            f"applied={apply_result['applied']} "
            f"failed={apply_result['failed']}"
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
            capabilities = agent.get("capabilities", [])
            capability_str = ", ".join(sorted(capabilities)) if capabilities else "none"
            typer.echo(f"  - {agent_name} ({agent_id})")
            typer.echo(f"    Capabilities: {capability_str}")


@pma_app.command("models")
def pma_models(
    agent: str = typer.Argument(..., help="Agent ID (codex|opencode|hermes)"),
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


@thread_app.command("spawn")
@thread_app.command("create")
def pma_thread_spawn(
    agent: Optional[str] = typer.Option(
        None, "--agent", help="Thread agent to use (codex|opencode|hermes|zeroclaw)"
    ),
    repo_id: Optional[str] = typer.Option(
        None, "--repo", help="Hub repo id for the target workspace"
    ),
    resource_kind: Optional[str] = typer.Option(
        None, "--resource-kind", help="Managed resource kind (repo|agent_workspace)"
    ),
    resource_id: Optional[str] = typer.Option(
        None, "--resource-id", help="Managed resource id"
    ),
    workspace_root: Optional[str] = typer.Option(
        None, "--workspace-root", help="Absolute or hub-relative workspace path"
    ),
    name: Optional[str] = typer.Option(None, "--name", help="Optional thread label"),
    context_profile: Optional[str] = typer.Option(
        None,
        "--context-profile",
        help="CAR context profile (car_core|car_ambient|none)",
    ),
    notify_on: Optional[str] = typer.Option(
        None,
        "--notify-on",
        help="Auto-subscribe for lifecycle events (supported: terminal)",
    ),
    terminal_followup: Optional[bool] = typer.Option(
        None,
        "--terminal-followup/--no-terminal-followup",
        help="Override the default terminal follow-up subscription for new threads",
    ),
    notify_lane: Optional[str] = typer.Option(
        None, "--notify-lane", help="Lane id used for terminal notifications"
    ),
    notify_once: bool = typer.Option(
        True,
        "--notify-once/--no-notify-once",
        help="Auto-cancel notification after first fire",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Create a managed PMA thread."""
    normalized_agent = _normalize_agent_option(agent)
    (
        normalized_resource_kind,
        normalized_resource_id,
        normalized_workspace_root,
    ) = _normalize_resource_owner_options(
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        workspace_root=workspace_root,
    )
    owner_present = (
        normalized_resource_kind is not None and normalized_resource_id is not None
    )
    if (
        sum(
            1
            for present in (
                owner_present,
                normalized_workspace_root is not None,
            )
            if present
        )
        != 1
    ):
        typer.echo(
            "Exactly one of --repo, --resource-kind/--resource-id, or --workspace-root is required",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if normalized_agent is None and normalized_resource_kind != "agent_workspace":
        typer.echo(
            "--agent is required unless --resource-kind agent_workspace is used",
            err=True,
        )
        raise typer.Exit(code=1) from None
    normalized_context_profile = normalize_car_context_profile(context_profile)
    if context_profile is not None and normalized_context_profile is None:
        typer.echo(
            "--context-profile must be one of: car_core, car_ambient, none",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if normalized_context_profile is None:
        normalized_context_profile = default_managed_thread_context_profile(
            resource_kind=normalized_resource_kind
        )

    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    required_cap = _CAPABILITY_REQUIREMENTS.get("thread_spawn")
    if required_cap and normalized_agent is not None:
        capabilities = _fetch_agent_capabilities(config, path)
        if not _check_capability(normalized_agent, required_cap, capabilities):
            typer.echo(
                f"Agent '{normalized_agent}' does not support thread creation (missing capability: {required_cap})",
                err=True,
            )
            raise typer.Exit(code=1) from None

    try:
        normalized_notify_on = _normalize_notify_on(notify_on)
        if terminal_followup is False and normalized_notify_on == "terminal":
            raise typer.BadParameter(
                "--no-terminal-followup cannot be combined with --notify-on terminal"
            )
        data = _request_json(
            "POST",
            _build_pma_url(config, "/threads"),
            {
                "agent": normalized_agent,
                "resource_kind": normalized_resource_kind,
                "resource_id": normalized_resource_id,
                "workspace_root": normalized_workspace_root,
                "name": name,
                "context_profile": normalized_context_profile,
                "notify_on": normalized_notify_on,
                "terminal_followup": terminal_followup,
                "notify_lane": notify_lane,
                "notify_once": notify_once,
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
    resource_kind: Optional[str] = typer.Option(
        None, "--resource-kind", help="Filter by managed resource kind"
    ),
    resource_id: Optional[str] = typer.Option(
        None, "--resource-id", help="Filter by managed resource id"
    ),
    limit: int = typer.Option(200, "--limit", min=1, help="Maximum rows to return"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON array output"),
    output_ndjson: bool = typer.Option(
        False, "--ndjson", help="Emit newline-delimited JSON output"
    ),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """List managed PMA threads."""
    if output_json and output_ndjson:
        raise typer.BadParameter(
            "Choose only one of --json or --ndjson.",
            param_hint="--json / --ndjson",
        )

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
            "status": status,
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

    threads = data.get("threads", []) if isinstance(data, dict) else []
    if not isinstance(threads, list):
        threads = []
    normalized_threads = [thread for thread in threads if isinstance(thread, dict)]

    if output_json:
        typer.echo(json.dumps(normalized_threads, indent=2))
        return

    if output_ndjson:
        for thread in normalized_threads:
            typer.echo(json.dumps(thread))
        return

    if not normalized_threads:
        typer.echo("No managed threads found")
        return
    for thread in normalized_threads:
        typer.echo(
            " ".join(
                [
                    str(thread.get("managed_thread_id") or ""),
                    f"agent={thread.get('agent') or ''}",
                    f"status={thread.get('status') or ''}",
                    f"reason={thread.get('status_reason') or '-'}",
                    _format_resource_owner_label(thread),
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


@thread_app.command("status")
def pma_thread_status(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    limit: int = typer.Option(
        20, "--limit", min=1, help="Maximum progress events to include"
    ),
    since: Optional[str] = typer.Option(
        None, "--since", help="Only include events newer than duration (e.g. 5m)"
    ),
    level: str = typer.Option("info", "--level", help="Verbosity level (info|debug)"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Show unified managed-thread status in one view."""
    hub_root = _resolve_hub_path(path)
    params: dict[str, Any] = {"limit": limit, "level": level}
    if since:
        params["since"] = since
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, f"/threads/{managed_thread_id}/status"),
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
    _render_thread_status_snapshot(data)


@thread_app.command("send")
def pma_thread_send(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    message: Optional[str] = typer.Option(
        None, "--message", help="User message to send", show_default=False
    ),
    message_file: Optional[Path] = typer.Option(
        None, "--message-file", help="Read the user message from a file"
    ),
    message_stdin: bool = typer.Option(
        False, "--message-stdin", help="Read the user message from stdin"
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Model override"),
    reasoning: Optional[str] = typer.Option(
        None, "--reasoning", help="Reasoning override"
    ),
    if_busy: str = typer.Option(
        "queue",
        "--if-busy",
        help="Busy-thread policy: queue, interrupt, or reject",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        help="Follow tail/events until the turn reaches terminal state",
    ),
    notify_on: Optional[str] = typer.Option(
        None,
        "--notify-on",
        help="Auto-subscribe for lifecycle events (supported: terminal)",
    ),
    notify_lane: Optional[str] = typer.Option(
        None, "--notify-lane", help="Lane id used for terminal notifications"
    ),
    notify_once: bool = typer.Option(
        True,
        "--notify-once/--no-notify-once",
        help="Auto-cancel notification after first fire",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Send a message to a managed PMA thread."""
    message_body = _resolve_message_body(
        message=message,
        message_file=message_file,
        message_stdin=message_stdin,
        option_hint="--message, --message-file, or --message-stdin",
    )
    normalized_notify_on = _normalize_notify_on(notify_on)
    should_defer = watch or normalized_notify_on == "terminal"
    normalized_if_busy = (if_busy or "").strip().lower() or "queue"
    if normalized_if_busy not in {"queue", "interrupt", "reject"}:
        raise typer.BadParameter("if-busy must be queue, interrupt, or reject")
    payload: dict[str, Any] = {
        "message": message_body,
        "busy_policy": normalized_if_busy,
        "defer_execution": should_defer,
    }
    if model:
        payload["model"] = model
    if reasoning:
        payload["reasoning"] = reasoning
    if normalized_notify_on:
        payload["notify_on"] = normalized_notify_on
        payload["notify_lane"] = notify_lane
        payload["notify_once"] = notify_once

    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        status_code, data = _request_json_with_status(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/messages"),
            payload,
            token_env=config.server_auth_token_env,
            timeout=240.0 if not should_defer else 30.0,
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    send_state = str(data.get("send_state") or "").strip().lower()
    status = (data.get("status") if isinstance(data, dict) else "") or ""
    if status_code >= 400 or status != "ok":
        if output_json:
            typer.echo(json.dumps(data, indent=2))
        else:
            detail = str(
                data.get("detail") or data.get("error") or "Managed thread send failed"
            )
            next_step = str(data.get("next_step") or "").strip()
            if send_state:
                typer.echo(f"send_state={send_state} error={detail}", err=True)
            else:
                typer.echo(detail, err=True)
            if next_step:
                typer.echo(f"next: {next_step}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        if watch:
            pma_thread_tail(
                managed_thread_id=managed_thread_id,
                follow=True,
                since=None,
                level="info",
                limit=50,
                output_json=True,
                path=path,
            )
        return

    delivered_message = str(data.get("delivered_message") or message_body)
    execution_state = str(data.get("execution_state") or "").strip().lower()
    if execution_state == "queued" or (should_defer and execution_state == "running"):
        line = (
            f"send_state={send_state or 'accepted'} "
            f"managed_turn_id={data.get('managed_turn_id') or ''}"
        )
        active_turn_id = str(data.get("active_managed_turn_id") or "").strip()
        queue_depth = data.get("queue_depth")
        if active_turn_id:
            line += f" active_managed_turn_id={active_turn_id}"
        if queue_depth is not None:
            line += f" queue_depth={queue_depth}"
        typer.echo(line)
        _echo_delivered_message(delivered_message)
        if watch:
            pma_thread_tail(
                managed_thread_id=managed_thread_id,
                follow=True,
                since=None,
                level="info",
                limit=50,
                output_json=False,
                path=path,
            )
            try:
                status_data = _request_json(
                    "GET",
                    _build_pma_url(config, f"/threads/{managed_thread_id}/status"),
                    token_env=config.server_auth_token_env,
                    params={"limit": 1},
                )
            except Exception:
                status_data = {}
            excerpt = str(status_data.get("latest_output_excerpt") or "").strip()
            if excerpt:
                typer.echo("\nlatest output:")
                typer.echo(excerpt)
        return

    line = (
        f"send_state={send_state or 'accepted'} "
        f"managed_turn_id={data.get('managed_turn_id') or ''} "
        f"execution_state={execution_state or 'completed'}"
    )
    typer.echo(line.strip())
    _echo_delivered_message(delivered_message)
    assistant_text = str(data.get("assistant_text") or "")
    if assistant_text:
        typer.echo("\nassistant:")
        typer.echo(assistant_text)


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


@thread_app.command("tail")
def pma_thread_tail(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    follow: bool = typer.Option(
        False, "--follow", help="Follow live events until turn completes"
    ),
    since: Optional[str] = typer.Option(
        None, "--since", help="Only include events newer than duration (e.g. 5m)"
    ),
    level: str = typer.Option("info", "--level", help="Verbosity level (info|debug)"),
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum events to include"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Show managed-thread tail/progress events."""
    hub_root = _resolve_hub_path(path)
    params: dict[str, Any] = {"limit": limit, "level": level}
    if since:
        params["since"] = since
    try:
        config = load_hub_config(hub_root)
        if not follow:
            data = _request_json(
                "GET",
                _build_pma_url(config, f"/threads/{managed_thread_id}/tail"),
                token_env=config.server_auth_token_env,
                params=params,
            )
            if output_json:
                typer.echo(json.dumps(data, indent=2))
            else:
                _render_tail_snapshot(data)
            return

        headers = _auth_headers_from_env(config.server_auth_token_env)
        url = _build_pma_url(config, f"/threads/{managed_thread_id}/tail/events")
        with httpx.stream(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=None,
        ) as response:
            response.raise_for_status()
            for event_name, data_str, event_id in _iter_sse_events(
                response.iter_lines()
            ):
                try:
                    data = json.loads(data_str) if data_str else {}
                except json.JSONDecodeError:
                    data = {"raw": data_str}
                if output_json:
                    payload = {"event": event_name, "data": data}
                    if event_id is not None:
                        payload["id"] = event_id
                    typer.echo(json.dumps(payload))
                    continue
                if event_name == "state":
                    if isinstance(data, dict):
                        _render_tail_snapshot(data)
                    continue
                if event_name == "tail":
                    if isinstance(data, dict):
                        typer.echo(_format_tail_event_line(data))
                    continue
                if event_name == "progress" and isinstance(data, dict):
                    status = data.get("turn_status") or "running"
                    elapsed = _format_seconds(data.get("elapsed_seconds"))
                    idle = _format_seconds(data.get("idle_seconds"))
                    phase = str(data.get("phase") or "-")
                    line = (
                        f"progress: status={status} phase={phase} "
                        f"elapsed={elapsed} idle={idle}"
                    )
                    idle_seconds = data.get("idle_seconds")
                    if (
                        isinstance(idle_seconds, int)
                        and status == "running"
                        and idle_seconds >= 30
                    ):
                        line += " (possibly stalled)"
                    typer.echo(line)
                    guidance = str(data.get("guidance") or "").strip()
                    if guidance:
                        typer.echo(f"guidance: {guidance}")
                    diagnostics = data.get("active_turn_diagnostics")
                    if isinstance(diagnostics, dict):
                        _render_active_turn_diagnostics(diagnostics)
                    if status != "running":
                        return
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None


@thread_app.command("compact")
def pma_thread_compact(
    managed_thread_id: Optional[str] = typer.Option(
        None, "--id", help="Managed PMA thread id", show_default=False
    ),
    status: Optional[str] = typer.Option(
        None, "--status", help="Bulk compact threads matching a status filter"
    ),
    all_threads: bool = typer.Option(
        False,
        "--all",
        help="Compact all non-archived managed threads matching the other filters",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Required when compacting all non-archived managed threads",
    ),
    summary: str = typer.Option(..., "--summary", help="Compaction summary"),
    no_reset_backend: bool = typer.Option(
        False, "--no-reset-backend", help="Preserve backend thread/session id"
    ),
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent"),
    repo_id: Optional[str] = typer.Option(None, "--repo", help="Filter by repo id"),
    resource_kind: Optional[str] = typer.Option(
        None, "--resource-kind", help="Filter by managed resource kind"
    ),
    resource_id: Optional[str] = typer.Option(
        None, "--resource-id", help="Filter by managed resource id"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview matching threads without compacting"
    ),
    limit: int = typer.Option(
        1000, "--limit", min=1, help="Maximum bulk-selected threads to inspect"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Store a compaction seed on one or more managed PMA threads."""
    hub_root = _resolve_hub_path(path)
    try:
        scope_status, scope_all = _normalize_thread_compact_scope(
            managed_thread_id=managed_thread_id,
            status=status,
            all_threads=all_threads,
        )
    except typer.BadParameter as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if scope_all and not force:
        typer.echo(
            "Error: --force is required with --all or --status all.",
            err=True,
        )
        raise typer.Exit(code=1) from None

    (
        normalized_resource_kind,
        normalized_resource_id,
        _normalized_workspace_root,
    ) = _normalize_resource_owner_options(
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    try:
        config = load_hub_config(hub_root)
        if managed_thread_id:
            targets = [{"managed_thread_id": managed_thread_id}]
        else:
            params = {
                key: value
                for key, value in {
                    "agent": agent,
                    "status": None if scope_all else scope_status,
                    "resource_kind": normalized_resource_kind,
                    "resource_id": normalized_resource_id,
                    "limit": limit,
                }.items()
                if value is not None
            }
            data = _request_json(
                "GET",
                _build_pma_url(config, "/threads"),
                token_env=config.server_auth_token_env,
                params=params,
            )
            raw_threads = data.get("threads", []) if isinstance(data, dict) else []
            threads = [item for item in raw_threads if isinstance(item, dict)]
            targets = [
                thread
                for thread in threads
                if str(thread.get("lifecycle_status") or "").strip().lower()
                != "archived"
                and str(thread.get("status") or "").strip().lower() != "archived"
            ]

        target_ids = [
            str(item.get("managed_thread_id") or "").strip()
            for item in targets
            if str(item.get("managed_thread_id") or "").strip()
        ]
        if not target_ids:
            empty_payload = {
                "dry_run": dry_run,
                "matched": 0,
                "compacted": [],
                "errors": [],
            }
            if output_json:
                typer.echo(json.dumps(empty_payload, indent=2))
            else:
                typer.echo("No managed threads matched the compact selection")
            return

        preview_payload = {
            "dry_run": dry_run,
            "matched": len(target_ids),
            "scope": {
                "id": managed_thread_id,
                "status": scope_status,
                "all": scope_all,
                "agent": agent,
                "resource_kind": normalized_resource_kind,
                "resource_id": normalized_resource_id,
            },
            "threads": targets,
        }
        if output_json and dry_run:
            typer.echo(json.dumps(preview_payload, indent=2))
        elif not output_json:
            typer.echo(
                f"Dry run summary: {len(target_ids)} thread"
                f"{'' if len(target_ids) == 1 else 's'} would be compacted"
            )
            for thread in targets:
                typer.echo(_thread_compact_target_line(thread))

        _record_thread_compact_audit(
            hub_root=hub_root,
            summary=summary,
            managed_thread_ids=target_ids,
            mode="dry_run" if dry_run else "execute",
            reset_backend=(not no_reset_backend),
            status=scope_status,
            scope_all=scope_all,
            resource_kind=normalized_resource_kind,
            resource_id=normalized_resource_id,
            agent=agent,
        )
        if dry_run:
            return

        compacted: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for target_id in target_ids:
            try:
                data = _request_json(
                    "POST",
                    _build_pma_url(config, f"/threads/{target_id}/compact"),
                    {"summary": summary, "reset_backend": (not no_reset_backend)},
                    token_env=config.server_auth_token_env,
                )
            except httpx.HTTPError as exc:
                errors.append(
                    {"managed_thread_id": target_id, "error": f"HTTP error: {exc}"}
                )
                continue
            except Exception as exc:
                errors.append({"managed_thread_id": target_id, "error": str(exc)})
                continue
            compacted.append(
                {
                    "managed_thread_id": target_id,
                    "thread": data.get("thread") if isinstance(data, dict) else None,
                }
            )

        _record_thread_compact_audit(
            hub_root=hub_root,
            summary=summary,
            managed_thread_ids=target_ids,
            mode="result",
            reset_backend=(not no_reset_backend),
            status=scope_status,
            scope_all=scope_all,
            resource_kind=normalized_resource_kind,
            resource_id=normalized_resource_id,
            agent=agent,
            errors=errors,
        )

        result_payload = {
            "dry_run": False,
            "matched": len(target_ids),
            "compacted": compacted,
            "errors": errors,
        }
        if output_json:
            typer.echo(json.dumps(result_payload, indent=2))
        else:
            for item in compacted:
                typer.echo(f"Compacted {item['managed_thread_id']}")
            if errors:
                for item in errors:
                    typer.echo(
                        f"Failed {item['managed_thread_id']}: {item['error']}",
                        err=True,
                    )
            typer.echo(
                f"Compacted {len(compacted)} thread"
                f"{'' if len(compacted) == 1 else 's'}"
            )
        if errors:
            raise typer.Exit(code=1) from None
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None


@thread_app.command("resume")
def pma_thread_resume(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Set a managed thread active."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/resume"),
            {},
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
        archive_url = _build_pma_url(config, f"/threads/{managed_thread_id}/archive")
        data = _request_json(
            "POST",
            archive_url,
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(
            format_hub_request_error(
                action=f"Failed to archive managed PMA thread {managed_thread_id}.",
                url=archive_url,
                exc=exc,
            ),
            err=True,
        )
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
    except Exception as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    thread_url = _build_pma_url(config, f"/threads/{managed_thread_id}")
    try:
        thread_data = _request_json(
            "GET", thread_url, token_env=config.server_auth_token_env
        )
    except Exception:
        pass
    else:
        thread = thread_data.get("thread", {}) if isinstance(thread_data, dict) else {}
        if isinstance(thread, dict):
            agent = thread.get("agent", "")
            capabilities = _fetch_agent_capabilities(config, path)
            required_cap = _CAPABILITY_REQUIREMENTS.get("thread_interrupt")
            if required_cap and not _check_capability(
                agent, required_cap, capabilities
            ):
                typer.echo(
                    f"Agent '{agent}' does not support interrupt (missing capability: {required_cap})",
                    err=True,
                )
                raise typer.Exit(code=1) from None

    try:
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
        status = str(data.get("status") or "").strip().lower()
        if status == "ok":
            typer.echo(f"Interrupted {managed_thread_id}")
        else:
            detail = str(
                data.get("detail")
                or data.get("backend_error")
                or "Managed thread interrupt failed"
            )
            interrupt_state = str(data.get("interrupt_state") or "").strip()
            managed_turn_id = str(data.get("managed_turn_id") or "").strip()
            line = detail
            if interrupt_state:
                line = f"interrupt_state={interrupt_state} error={detail}"
            if managed_turn_id:
                line += f" managed_turn_id={managed_turn_id}"
            typer.echo(line, err=True)
            raise typer.Exit(code=1) from None


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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Upload files to PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
):
    """Download a file from PMA inbox or outbox (FileBox-backed via /hub/pma/files)."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
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
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
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
    except Exception as exc:
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
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
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
    except Exception as exc:
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
