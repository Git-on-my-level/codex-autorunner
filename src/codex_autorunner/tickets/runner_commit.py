from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import TicketResult
from .runner_execution import capture_git_state_after
from .runner_post_turn import build_pause_result


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_barrier_epoch(
    *,
    current_ticket: Optional[str],
    first_seen_at: str,
    head_before_turn: Optional[str],
) -> str:
    raw = f"{current_ticket or ''}\n{first_seen_at}\n{head_before_turn or ''}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"commit-barrier:{digest}"


def _commit_barrier_state(
    *,
    existing: Optional[dict[str, Any]] = None,
    pending: bool = True,
    current_ticket: Optional[str] = None,
    retries: int,
    head_before_turn: Optional[str],
    head_after_agent: Optional[str],
    agent_committed_this_turn: Optional[bool],
    status_after_agent: Optional[str],
    max_commit_retries: int,
    last_error: Optional[str] = None,
    last_attempt_at: Optional[str] = None,
    resolution_state: str = "pending",
) -> dict[str, Any]:
    prior = existing if isinstance(existing, dict) else {}
    now = _now_iso()
    first_seen_at = str(prior.get("first_seen_at") or now)
    barrier_epoch = str(
        prior.get("barrier_epoch")
        or prior.get("epoch")
        or _stable_barrier_epoch(
            current_ticket=current_ticket,
            first_seen_at=first_seen_at,
            head_before_turn=head_before_turn,
        )
    )
    exhausted = bool(
        resolution_state == "exhausted"
        or (max_commit_retries >= 0 and retries >= max_commit_retries)
    )
    state = {
        "pending": pending,
        "barrier_epoch": barrier_epoch,
        "epoch": barrier_epoch,
        "current_ticket": current_ticket or prior.get("current_ticket"),
        "retries": retries,
        "max_retries": max_commit_retries,
        "first_seen_at": first_seen_at,
        "last_attempt_at": last_attempt_at or prior.get("last_attempt_at"),
        "last_error": last_error,
        "head_before": head_before_turn,
        "head_after": head_after_agent,
        "agent_committed_this_turn": agent_committed_this_turn,
        "status_porcelain": status_after_agent,
        "worktree_summary": {
            "status_porcelain": status_after_agent,
            "head_before": head_before_turn,
            "head_after": head_after_agent,
        },
        "resolution_state": "exhausted" if exhausted else resolution_state,
        "exhausted": exhausted,
    }
    if exhausted and not prior.get("exhausted_at"):
        state["exhausted_at"] = now
    elif prior.get("exhausted_at"):
        state["exhausted_at"] = prior.get("exhausted_at")
    return state


def resolved_commit_barrier_state(
    existing: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return a resolved barrier marker for projections/history."""
    if not isinstance(existing, dict) or not existing:
        return None
    resolved = dict(existing)
    resolved["pending"] = False
    resolved["resolution_state"] = "resolved"
    resolved["resolved_at"] = _now_iso()
    return resolved


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
    current_ticket: Optional[str] = None,
    existing_commit_state: Optional[dict[str, Any]] = None,
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

    commit_state = _commit_barrier_state(
        existing=existing_commit_state,
        current_ticket=current_ticket,
        retries=next_failed_attempts,
        head_before_turn=head_before_turn,
        head_after_agent=head_after_agent,
        agent_committed_this_turn=agent_committed_this_turn,
        status_after_agent=status_after_agent,
        max_commit_retries=max_commit_retries,
        last_attempt_at=_now_iso() if commit_pending else None,
    )

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
    current_ticket: Optional[str] = None,
    existing_commit_state: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], str, Optional[str], str, Optional[str]]:
    """Process a failed commit-resolution turn."""
    next_failed_attempts = commit_retries + 1
    commit_state = _commit_barrier_state(
        existing=existing_commit_state,
        current_ticket=current_ticket,
        retries=next_failed_attempts,
        head_before_turn=head_before_turn,
        head_after_agent=head_after_agent,
        agent_committed_this_turn=False,
        status_after_agent=status_after_agent,
        max_commit_retries=max_commit_retries,
        last_error=error,
        last_attempt_at=_now_iso(),
    )
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
        current_ticket=current_ticket_path,
        existing_commit_state=state.get("commit") if isinstance(state, dict) else None,
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
