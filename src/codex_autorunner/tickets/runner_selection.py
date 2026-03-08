from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .files import (
    list_ticket_paths,
    read_ticket,
    safe_relpath,
    ticket_is_done,
)
from .frontmatter import parse_markdown_frontmatter
from .lint import lint_ticket_directory
from .models import TicketFrontmatter, TicketRunConfig

_logger = logging.getLogger(__name__)


class TicketSelectionError(Exception):
    """Error during ticket selection."""

    pass


def select_ticket(
    *,
    workspace_root: Path,
    ticket_dir: Path,
    config: TicketRunConfig,
    state: dict[str, Any],
    emit_event: Optional[Any] = None,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], str, str, str]:
    """Select and validate the next ticket to run.

    Returns (selected_ticket_info, state_updates, status, reason_code, message).
    - selected_ticket_info: dict with path, rel_path, frontmatter, or None if completed
    - state_updates: dict of state changes to apply
    - status: "continue" | "paused" | "completed"
    - reason_code: pause/completion reason code
    - message: human-readable message
    """
    ticket_paths = list_ticket_paths(ticket_dir)
    if not ticket_paths:
        return (
            None,
            {"status": "completed"},
            "completed",
            "no_tickets",
            f"No tickets found. Create tickets under {safe_relpath(ticket_dir, workspace_root)} and resume.",
        )

    dir_lint_errors = lint_ticket_directory(ticket_dir)
    if dir_lint_errors:
        return (
            None,
            {"status": "paused"},
            "paused",
            "needs_user_fix",
            "Duplicate ticket indices detected.",
            "Errors:\n- " + "\n- ".join(dir_lint_errors),
        )

    current_ticket = state.get("current_ticket")
    current_path: Optional[Path] = (
        (workspace_root / current_ticket)
        if isinstance(current_ticket, str) and current_ticket
        else None
    )

    if current_path is not None and not current_path.exists():
        _logger.warning(
            "Current ticket file no longer exists at %s; clearing stale current_ticket state.",
            safe_relpath(current_path, workspace_root),
        )
        current_path = None
        state_updates = {
            "current_ticket": None,
            "ticket_turns": None,
            "last_agent_output": None,
            "lint": None,
            "commit": None,
        }
    else:
        state_updates = {}

    commit_raw = state.get("commit")
    commit_state: dict[str, Any] = commit_raw if isinstance(commit_raw, dict) else {}
    commit_pending = bool(commit_state.get("pending"))

    if current_path and ticket_is_done(current_path) and not commit_pending:
        current_path = None

    if current_path is None:
        next_path = _find_next_ticket(ticket_paths)
        if next_path is None:
            return (
                None,
                {"status": "completed"},
                "completed",
                "all_done",
                "All tickets done.",
            )
        current_path = next_path
        rel_path = safe_relpath(current_path, workspace_root)
        state_updates["current_ticket"] = rel_path
        if emit_event is not None:
            emit_event(
                "step_progress",
                {
                    "message": "Selected ticket",
                    "current_ticket": rel_path,
                },
            )
        state_updates["ticket_turns"] = 0
        state_updates["last_agent_output"] = None
        state_updates["lint"] = None
        state_updates["loop_guard"] = None

    state_updates["commit"] = None

    return (
        {"path": current_path, "rel_path": safe_relpath(current_path, workspace_root)},
        state_updates,
        "continue",
        "",
        "",
    )


def validate_ticket_for_execution(
    *,
    ticket_path: Path,
    workspace_root: Path,
    state: dict[str, Any],
    lint_errors: Optional[list[str]] = None,
) -> tuple[Optional[dict[str, Any]], str, str, Optional[str], list[str]]:
    """Validate ticket for execution, handling lint-retry mode.

    Returns (ticket_info, status, reason_code, pause_message, errors).
    """
    if lint_errors:
        return _validate_ticket_lint_retry(
            ticket_path=ticket_path,
            workspace_root=workspace_root,
            lint_errors=lint_errors,
        )

    ticket_doc, ticket_errors = read_ticket(ticket_path)
    if ticket_errors or ticket_doc is None:
        return (
            None,
            "paused",
            "needs_user_fix",
            f"Ticket frontmatter invalid: {safe_relpath(ticket_path, workspace_root)}",
            ticket_errors,
        )

    if ticket_doc.frontmatter.agent == "user":
        if ticket_doc.frontmatter.done:
            return (
                {
                    "path": ticket_path,
                    "frontmatter": ticket_doc.frontmatter,
                    "rel_path": safe_relpath(ticket_path, workspace_root),
                },
                "continue",
                "",
                None,
                [],
            )
        return (
            None,
            "paused",
            "user_pause",
            f"Paused for user input. Mark ticket as done when ready: {safe_relpath(ticket_path, workspace_root)}",
            [],
        )

    return (
        {
            "path": ticket_path,
            "frontmatter": ticket_doc.frontmatter,
            "rel_path": safe_relpath(ticket_path, workspace_root),
        },
        "continue",
        "",
        None,
        [],
    )


def _validate_ticket_lint_retry(
    *,
    ticket_path: Path,
    workspace_root: Path,
    lint_errors: list[str],
) -> tuple[Optional[dict[str, Any]], str, str, Optional[str], list[str]]:
    """Handle lint-retry mode for ticket validation."""
    try:
        raw = ticket_path.read_text(encoding="utf-8")
    except OSError as exc:
        return (
            None,
            "paused",
            "infra_error",
            f"Ticket unreadable during lint retry for {safe_relpath(ticket_path, workspace_root)}: {exc}",
            [],
        )

    data, _ = parse_markdown_frontmatter(raw)
    agent = data.get("agent")
    agent_id = agent.strip() if isinstance(agent, str) else None
    if not agent_id:
        return (
            None,
            "paused",
            "needs_user_fix",
            "Cannot determine ticket agent during lint retry (missing frontmatter.agent). Fix the ticket frontmatter manually and resume.",
            [],
        )

    if agent_id != "user":
        try:
            from ..agents.registry import validate_agent_id

            agent_id = validate_agent_id(agent_id)
        except Exception as exc:
            return (
                None,
                "paused",
                "needs_user_fix",
                f"Cannot determine valid agent during lint retry for {safe_relpath(ticket_path, workspace_root)}: {exc}",
                [],
            )

    return (
        {
            "path": ticket_path,
            "frontmatter": TicketFrontmatter(agent=agent_id, done=False),
            "rel_path": safe_relpath(ticket_path, workspace_root),
        },
        "continue",
        "",
        None,
        [],
    )


def _find_next_ticket(ticket_paths: list[Path]) -> Optional[Path]:
    for path in ticket_paths:
        if ticket_is_done(path):
            continue
        return path
    return None
