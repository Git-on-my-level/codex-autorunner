from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .models import TicketResult
from .runner_execution import capture_git_state_after
from .runner_post_turn import build_pause_result


def _build_manual_commit_required_details(
    *,
    status_after_agent: Optional[str],
    error: Optional[str] = None,
) -> str:
    detail = (status_after_agent or "").strip()
    detail_lines = detail.splitlines()[:20]
    details_parts = [
        "Please commit manually (ensuring pre-commit hooks pass) and resume."
    ]
    if error:
        details_parts.append(f"\n\nLast commit attempt error:\n{error.strip()}")
    if detail_lines:
        details_parts.append(
            "\n\nWorking tree status (git status --porcelain):\n- "
            + "\n- ".join(detail_lines)
        )
    return "".join(details_parts)


def process_commit_required(
    *,
    clean_after_agent: Optional[bool],
    commit_pending: bool,
    commit_retries: int,
    head_before_turn: Optional[str],
    head_after_agent: Optional[str],
    agent_committed_this_turn: Optional[bool],
    status_after_agent: Optional[str],
    max_commit_retries: int,
) -> tuple[dict[str, Any], str, Optional[str], str, Optional[str]]:
    """Process commit-required logic after successful turn."""
    commit_state = {}
    status = "continue"
    reason = None
    reason_code = "needs_user_fix"
    reason_details = None

    commit_required_now = clean_after_agent is False

    if not commit_pending and not commit_required_now:
        return {}, status, reason, reason_code, reason_details

    if commit_pending:
        next_failed_attempts = commit_retries + 1
    else:
        next_failed_attempts = 0

    commit_state = {
        "pending": True,
        "retries": next_failed_attempts,
        "head_before": head_before_turn,
        "head_after": head_after_agent,
        "agent_committed_this_turn": agent_committed_this_turn,
        "status_porcelain": status_after_agent,
    }

    if commit_pending and next_failed_attempts >= max_commit_retries:
        reason = (
            f"Commit failed after {max_commit_retries} attempts. "
            "Manual commit required."
        )
        reason_details = _build_manual_commit_required_details(
            status_after_agent=status_after_agent
        )

    return commit_state, status, reason, reason_code, reason_details


def process_failed_commit_attempt(
    *,
    commit_retries: int,
    head_before_turn: Optional[str],
    head_after_agent: Optional[str],
    status_after_agent: Optional[str],
    error: Optional[str],
    max_commit_retries: int,
) -> tuple[dict[str, Any], str, Optional[str], str, Optional[str]]:
    """Process a failed commit-resolution turn."""
    next_failed_attempts = commit_retries + 1
    commit_state = {
        "pending": True,
        "retries": next_failed_attempts,
        "head_before": head_before_turn,
        "head_after": head_after_agent,
        "agent_committed_this_turn": False,
        "status_porcelain": status_after_agent,
        "last_error": error,
    }
    status = "continue"
    reason = None
    reason_code = "needs_user_fix"
    reason_details = None

    if next_failed_attempts >= max_commit_retries:
        reason = (
            f"Commit failed after {max_commit_retries} attempts. "
            "Manual commit required."
        )
        reason_details = _build_manual_commit_required_details(
            status_after_agent=status_after_agent,
            error=error,
        )

    return commit_state, status, reason, reason_code, reason_details


def handle_failed_commit_turn(
    *,
    state: dict[str, Any],
    workspace_root: Path,
    commit_pending: bool,
    commit_retries: int,
    head_before_turn: Optional[str],
    max_commit_retries: int,
    current_ticket_path: str,
    result_error: Optional[str],
    result_text: Optional[str],
    result_agent_id: Optional[str],
    result_conversation_id: Optional[str],
    result_turn_id: Optional[str],
) -> Optional[TicketResult]:
    """Return a retry/pause result when a commit-resolution turn fails."""
    failure_git_state = capture_git_state_after(
        workspace_root=workspace_root,
        head_before_turn=head_before_turn,
    )
    if not commit_pending or failure_git_state["clean_after_turn"] is True:
        return None

    (
        commit_state_update,
        _commit_status,
        commit_reason,
        commit_reason_code,
        commit_reason_details,
    ) = process_failed_commit_attempt(
        commit_retries=commit_retries,
        head_before_turn=head_before_turn,
        head_after_agent=failure_git_state["head_after_turn"],
        status_after_agent=failure_git_state["status_after_turn"],
        error=result_error,
        max_commit_retries=max_commit_retries,
    )
    next_state = dict(state)
    next_state["commit"] = commit_state_update

    if commit_reason is not None:
        paused = build_pause_result(
            state=next_state,
            reason=commit_reason,
            reason_code=commit_reason_code,
            reason_details=commit_reason_details,
            current_ticket=current_ticket_path,
            workspace_root=workspace_root,
        )
        return TicketResult(
            status="paused",
            state=paused["state"],
            reason=paused["reason"],
            reason_details=paused["reason_details"],
            current_ticket=paused["current_ticket"],
            agent_output=result_text,
            agent_id=result_agent_id,
            agent_conversation_id=result_conversation_id,
            agent_turn_id=result_turn_id,
        )

    return TicketResult(
        status="continue",
        state=next_state,
        reason="Commit attempt failed; retrying agent commit resolution.",
        current_ticket=current_ticket_path,
        agent_output=result_text,
        agent_id=result_agent_id,
        agent_conversation_id=result_conversation_id,
        agent_turn_id=result_turn_id,
    )
