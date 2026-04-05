from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..core.flows.models import FlowEventType
from .files import (
    list_ticket_paths,
    read_ticket,
    safe_relpath,
    ticket_is_done,
)
from .frontmatter import parse_markdown_frontmatter
from .lint import lint_ticket_directory
from .models import TicketDoc, TicketFrontmatter, TicketRunConfig
from .runner_types import (
    SelectedTicket,
    TicketSelectionResult,
    TicketValidationResult,
    ValidatedTicket,
)

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
) -> TicketSelectionResult:
    """Select and validate the next ticket to run.

    Returns TicketSelectionResult with selected ticket, state updates, and status.
    """
    ticket_paths = list_ticket_paths(ticket_dir)
    if not ticket_paths:
        return TicketSelectionResult(
            status="paused",
            pause_reason=f"No tickets found. Create tickets under {safe_relpath(ticket_dir, workspace_root)} and resume.",
            pause_reason_code="no_tickets",
        )

    dir_lint_errors = lint_ticket_directory(ticket_dir)
    if dir_lint_errors:
        return TicketSelectionResult(
            status="paused",
            pause_reason="Duplicate ticket indices detected.",
            pause_reason_code="needs_user_fix",
            pause_reason_details="Errors:\n- " + "\n- ".join(dir_lint_errors),
        )

    current_ticket = state.get("current_ticket")
    current_path: Optional[Path] = (
        (workspace_root / current_ticket)
        if isinstance(current_ticket, str) and current_ticket
        else None
    )

    state_updates: dict[str, Any] = {}

    def _clear_per_ticket_state() -> None:
        state_updates["current_ticket"] = None
        state_updates["current_ticket_id"] = None
        state_updates["ticket_turns"] = None
        state_updates["last_agent_output"] = None
        state_updates["lint"] = None
        state_updates["commit"] = None

    reset_commit_state = False
    if current_path is not None and not current_path.exists():
        _logger.warning(
            "Current ticket file no longer exists at %s; clearing stale current_ticket state.",
            safe_relpath(current_path, workspace_root),
        )
        current_path = None
        state_updates = {}
        _clear_per_ticket_state()
        reset_commit_state = True
    else:
        state_updates = {}

    commit_raw = state.get("commit")
    commit_state: dict[str, Any] = commit_raw if isinstance(commit_raw, dict) else {}
    commit_pending = bool(commit_state.get("pending"))

    if current_path and ticket_is_done(current_path) and not commit_pending:
        current_path = None
        _clear_per_ticket_state()

    if current_path is None:
        next_path = _find_next_ticket(ticket_paths)
        if next_path is None:
            return TicketSelectionResult(
                status="completed",
                state_updates={"status": "completed"},
                pause_reason="All tickets done.",
                pause_reason_code="all_done",
            )
        current_path = next_path
        rel_path = safe_relpath(current_path, workspace_root)
        state_updates["current_ticket"] = rel_path
        if emit_event is not None:
            emit_event(
                FlowEventType.STEP_PROGRESS,
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

    return TicketSelectionResult(
        selected=SelectedTicket(
            path=current_path,
            rel_path=safe_relpath(current_path, workspace_root),
            frontmatter=TicketFrontmatter(ticket_id="", agent="", done=False),
        ),
        status="continue",
        state_updates=state_updates,
        reset_commit_state=reset_commit_state,
    )


def validate_ticket_for_execution(
    *,
    ticket_path: Path,
    workspace_root: Path,
    state: dict[str, Any],
    lint_errors: Optional[list[str]] = None,
) -> TicketValidationResult:
    """Validate ticket for execution, handling lint-retry mode.

    Returns TicketValidationResult with validated ticket or pause reason.
    """
    if lint_errors:
        return _validate_ticket_lint_retry(
            ticket_path=ticket_path,
            workspace_root=workspace_root,
            lint_errors=lint_errors,
        )

    ticket_doc, ticket_errors = read_ticket(ticket_path)
    if ticket_errors or ticket_doc is None:
        return TicketValidationResult(
            status="paused",
            pause_reason=f"Ticket frontmatter invalid: {safe_relpath(ticket_path, workspace_root)}",
            pause_reason_code="needs_user_fix",
            errors=ticket_errors,
        )

    if ticket_doc.frontmatter.agent == "user":
        if ticket_doc.frontmatter.done:
            return TicketValidationResult(
                validated=ValidatedTicket(
                    path=ticket_path,
                    rel_path=safe_relpath(ticket_path, workspace_root),
                    ticket_doc=ticket_doc,
                    skip_execution=True,
                ),
                status="continue",
            )
        return TicketValidationResult(
            status="paused",
            pause_reason=f"Paused for user input. Mark ticket as done when ready: {safe_relpath(ticket_path, workspace_root)}",
            pause_reason_code="user_pause",
        )

    return TicketValidationResult(
        validated=ValidatedTicket(
            path=ticket_path,
            rel_path=safe_relpath(ticket_path, workspace_root),
            ticket_doc=ticket_doc,
            skip_execution=False,
        ),
        status="continue",
    )


def _validate_ticket_lint_retry(
    *,
    ticket_path: Path,
    workspace_root: Path,
    lint_errors: list[str],
) -> TicketValidationResult:
    """Handle lint-retry mode for ticket validation."""
    try:
        raw = ticket_path.read_text(encoding="utf-8")
    except OSError as exc:
        return TicketValidationResult(
            status="paused",
            pause_reason=f"Ticket unreadable during lint retry for {safe_relpath(ticket_path, workspace_root)}: {exc}",
            pause_reason_code="infra_error",
        )

    data, _ = parse_markdown_frontmatter(raw)
    agent = data.get("agent")
    agent_id = agent.strip() if isinstance(agent, str) else None
    if not agent_id:
        return TicketValidationResult(
            status="paused",
            pause_reason="Cannot determine ticket agent during lint retry (missing frontmatter.agent). Fix the ticket frontmatter manually and resume.",
            pause_reason_code="needs_user_fix",
        )

    if agent_id != "user":
        try:
            from ..agents.registry import validate_agent_id

            agent_id = validate_agent_id(agent_id, workspace_root)
        except Exception as exc:
            return TicketValidationResult(
                status="paused",
                pause_reason=f"Cannot determine valid agent during lint retry for {safe_relpath(ticket_path, workspace_root)}: {exc}",
                pause_reason_code="needs_user_fix",
            )

    return TicketValidationResult(
        validated=ValidatedTicket(
            path=ticket_path,
            rel_path=safe_relpath(ticket_path, workspace_root),
            ticket_doc=TicketDoc(
                path=ticket_path,
                index=0,
                frontmatter=TicketFrontmatter(
                    ticket_id="lint-retry-ticket",
                    agent=agent_id,
                    done=False,
                ),
                body="",
            ),
            skip_execution=False,
        ),
        status="continue",
    )


def _find_next_ticket(ticket_paths: list[Path]) -> Optional[Path]:
    for path in ticket_paths:
        if ticket_is_done(path):
            continue
        return path
    return None
