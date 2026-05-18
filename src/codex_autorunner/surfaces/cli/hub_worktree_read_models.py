from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any


def _status_value(status: Any) -> str | None:
    if hasattr(status, "value") and status is not None:
        return str(status.value)
    if status is None:
        return None
    return str(status)


def command_with_hub_path(command: str, hub_root: Path | None) -> str:
    if hub_root is None:
        return command
    return f"{command} --path {shlex.quote(str(hub_root))}"


def recommended_lifecycle_actions(
    repo_id: str,
    *,
    repo_kind: str,
    hub_path: Path | None = None,
) -> list[str]:
    normalized_kind = str(repo_kind or "").strip().lower()
    if normalized_kind == "worktree":
        actions = [
            f"car hub worktree retire {repo_id}",
            f"car hub destination show {repo_id}",
        ]
    else:
        actions = [f"car hub destination show {repo_id}"]
    return [command_with_hub_path(action, hub_path) for action in actions]


def repo_listing_payload(snapshot: Any) -> dict[str, Any]:
    raw_status = getattr(snapshot, "status", None)
    status_text = (
        str(raw_status.value)
        if hasattr(raw_status, "value") and raw_status is not None
        else str(raw_status or "-")
    )
    return {
        "repo_id": str(getattr(snapshot, "id", "") or ""),
        "branch": str(getattr(snapshot, "branch", "") or "-"),
        "status": status_text,
        "enabled": bool(getattr(snapshot, "enabled", False)),
    }


def worktree_snapshot_payload(
    snapshot: Any, *, hub_path: Path | None = None
) -> dict[str, Any]:
    recommended_actions = recommended_lifecycle_actions(
        str(getattr(snapshot, "id", "") or ""),
        repo_kind="worktree",
        hub_path=hub_path,
    )
    return {
        "id": getattr(snapshot, "id", None),
        "worktree_of": getattr(snapshot, "worktree_of", None),
        "branch": getattr(snapshot, "branch", None),
        "path": str(getattr(snapshot, "path", "")),
        "initialized": getattr(snapshot, "initialized", None),
        "exists_on_disk": getattr(snapshot, "exists_on_disk", None),
        "status": _status_value(getattr(snapshot, "status", None)),
        "recommended_command": recommended_actions[0],
        "recommended_actions": recommended_actions,
    }


def scan_row_payload(snapshot: Any, *, hub_path: Path | None = None) -> dict[str, Any]:
    repo_id = str(getattr(snapshot, "id", "") or "")
    repo_kind = str(getattr(snapshot, "kind", "") or "")
    recommended_actions = recommended_lifecycle_actions(
        repo_id,
        repo_kind=repo_kind,
        hub_path=hub_path,
    )
    return {
        "id": repo_id,
        "kind": repo_kind,
        "status": _status_value(getattr(snapshot, "status", None)),
        "recommended_command": recommended_actions[0] if recommended_actions else None,
        "recommended_actions": recommended_actions,
    }


def destination_summary_payload(
    *,
    repo: Any,
    resolution: Any,
    issues: list[str],
    include_kind: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repo_id": repo.id,
        "configured_destination": repo.destination,
        "effective_destination": resolution.to_dict(),
        "source": resolution.source,
        "issues": issues,
    }
    if include_kind:
        payload.update({"kind": repo.kind, "worktree_of": repo.worktree_of})
    return payload


def cleanup_status_lines(result: object) -> list[str]:
    lines = ["ok"]
    if not isinstance(result, dict):
        return lines
    docker_cleanup = result.get("docker_cleanup")
    if not isinstance(docker_cleanup, dict):
        return lines
    status = str(docker_cleanup.get("status", "unknown")).strip() or "unknown"
    parts = [f"docker_cleanup={status}"]
    container_name = docker_cleanup.get("container_name")
    if isinstance(container_name, str) and container_name.strip():
        parts.append(f"container={container_name.strip()}")
    message = docker_cleanup.get("message")
    if isinstance(message, str) and message.strip():
        parts.append(f"detail={message.strip()}")
    lines.append(" ".join(parts))
    return lines


def truncate_table_cell(value: Any, *, width: int) -> str:
    if isinstance(value, list):
        text = ",".join(str(item) for item in value if item is not None) or "-"
    else:
        text = str(value).strip() if value is not None else "-"
        if not text:
            text = "-"
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3]}..."


def format_text_table_lines(
    columns: list[tuple[str, str, int]], rows: list[dict[str, str]]
) -> list[str]:
    widths: dict[str, int] = {}
    for header, _key, max_width in columns:
        cell_lengths = [len(row[header]) for row in rows] if rows else [0]
        widths[header] = min(max(max(cell_lengths), len(header)), max_width)
    header_line = "  ".join(header.ljust(widths[header]) for header, _, _ in columns)
    separator_line = "  ".join("-" * widths[header] for header, _, _ in columns)
    lines = [header_line, separator_line]
    for row in rows:
        lines.append(
            "  ".join(row[header].ljust(widths[header]) for header, _, _ in columns)
        )
    return lines


def render_repo_table_lines(repos: list[dict[str, Any]]) -> list[str]:
    columns = [
        ("REPO_ID", "repo_id", 32),
        ("BRANCH", "branch", 24),
        ("STATUS", "status", 16),
        ("ENABLED", "enabled", 7),
    ]
    rows: list[dict[str, str]] = []
    for repo in repos:
        rows.append(
            {
                "REPO_ID": truncate_table_cell(repo.get("repo_id"), width=32),
                "BRANCH": truncate_table_cell(repo.get("branch"), width=24),
                "STATUS": truncate_table_cell(repo.get("status"), width=16),
                "ENABLED": "yes" if bool(repo.get("enabled")) else "no",
            }
        )
    return format_text_table_lines(columns, rows)


def render_worktree_summary_lines(payload: list[dict[str, Any]]) -> list[str]:
    if not payload:
        return ["No worktrees."]
    lines = [f"Worktrees ({len(payload)}):"]
    for item in payload:
        cmd = item.get("recommended_command", "")
        line = "  {id} base={worktree_of} branch={branch} status={status}".format(
            **item
        )
        lines.append(line + (f" cmd={cmd}" if cmd else ""))
    return lines


def summarize_snapshot_repo(repo: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(repo, dict):
        return {}
    ticket_flow = (
        repo.get("ticket_flow") if isinstance(repo.get("ticket_flow"), dict) else {}
    )
    failure = ticket_flow.get("failure") if isinstance(ticket_flow, dict) else None
    failure_summary = (
        ticket_flow.get("failure_summary") if isinstance(ticket_flow, dict) else None
    )
    pr_url = ticket_flow.get("pr_url") if isinstance(ticket_flow, dict) else None
    final_review_status = (
        ticket_flow.get("final_review_status")
        if isinstance(ticket_flow, dict)
        else None
    )
    run_state = repo.get("run_state")
    if not isinstance(run_state, dict):
        run_state = {}
    canonical = repo.get("canonical_state_v1")
    if not isinstance(canonical, dict):
        canonical = {}
    return {
        "id": repo.get("id"),
        "display_name": repo.get("display_name"),
        "status": repo.get("status"),
        "initialized": repo.get("initialized"),
        "exists_on_disk": repo.get("exists_on_disk"),
        "last_run_id": repo.get("last_run_id"),
        "last_run_started_at": repo.get("last_run_started_at"),
        "last_run_finished_at": repo.get("last_run_finished_at"),
        "failure": failure,
        "failure_summary": failure_summary,
        "pr_url": pr_url,
        "final_review_status": final_review_status,
        "run_state": {
            "state": run_state.get("state"),
            "blocking_reason": run_state.get("blocking_reason"),
            "current_ticket": run_state.get("current_ticket"),
            "last_progress_at": run_state.get("last_progress_at"),
            "recommended_action": run_state.get("recommended_action"),
        },
        "freshness": canonical.get("freshness"),
    }


def summarize_snapshot_message(msg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(msg, dict):
        return {}
    dispatch = msg.get("dispatch", {})
    if not isinstance(dispatch, dict):
        dispatch = {}
    body = dispatch.get("body", "")
    title = dispatch.get("title", "")
    truncated_body = (body[:200] + "...") if len(body) > 200 else body
    run_state = msg.get("run_state")
    if not isinstance(run_state, dict):
        run_state = {}
    canonical = msg.get("canonical_state_v1")
    if not isinstance(canonical, dict):
        canonical = {}
    return {
        "item_type": msg.get("item_type"),
        "next_action": msg.get("next_action"),
        "repo_id": msg.get("repo_id"),
        "repo_display_name": msg.get("repo_display_name"),
        "run_id": msg.get("run_id"),
        "run_created_at": msg.get("run_created_at"),
        "status": msg.get("status"),
        "seq": msg.get("seq"),
        "dispatch": {
            "mode": dispatch.get("mode"),
            "title": title,
            "body": truncated_body,
            "is_handoff": dispatch.get("is_handoff"),
        },
        "files_count": (
            len(msg.get("files", [])) if isinstance(msg.get("files"), list) else 0
        ),
        "reason": msg.get("reason"),
        "run_state": {
            "state": run_state.get("state"),
            "blocking_reason": run_state.get("blocking_reason"),
            "current_ticket": run_state.get("current_ticket"),
            "last_progress_at": run_state.get("last_progress_at"),
            "recommended_action": run_state.get("recommended_action"),
        },
        "freshness": canonical.get("freshness"),
    }
