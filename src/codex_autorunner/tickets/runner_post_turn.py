from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..core.file_chat_keys import ticket_instance_token
from ..core.flows.models import FlowEventType
from ..core.git_utils import git_diff_stats, run_git
from .files import read_ticket_frontmatter
from .outbox import (
    archive_dispatch,
    create_turn_summary,
)

_logger = logging.getLogger(__name__)

MAX_LINT_RETRIES = 3


def archive_dispatch_and_create_summary(
    *,
    outbox_paths: Any,
    current_ticket_id: str,
    repo_id: str,
    run_id: str,
    dispatch_seq: int,
    agent_output: str,
    agent_id: str,
    turn_number: int,
    head_before_turn: Optional[str],
    current_ticket_path: Optional[Path] = None,
    emit_event: Optional[Any] = None,
) -> tuple[Optional[Any], Optional[dict[str, Any]]]:
    """Archive DISPATCH and create turn summary."""
    dispatch_ticket_id = (
        str(current_ticket_path) if current_ticket_path else current_ticket_id
    )
    dispatch, dispatch_errors = archive_dispatch(
        outbox_paths,
        next_seq=dispatch_seq + 1,
        ticket_id=dispatch_ticket_id,
        repo_id=repo_id,
        run_id=run_id,
        origin="runner",
    )

    if dispatch_errors:
        return None, {"dispatch_errors": dispatch_errors}

    turn_summary_seq = dispatch_seq + 1

    turn_diff_stats = None
    try:
        if head_before_turn:
            turn_diff_stats = git_diff_stats(
                outbox_paths.dispatch_dir.parent.parent,
                from_ref=head_before_turn,
            )
        else:
            turn_diff_stats = git_diff_stats(
                outbox_paths.dispatch_dir.parent.parent,
                from_ref=None,
                include_staged=True,
            )
    except Exception:
        turn_diff_stats = None

    turn_summary, turn_summary_errors = create_turn_summary(
        outbox_paths,
        next_seq=turn_summary_seq,
        agent_output=agent_output or "",
        ticket_id=dispatch_ticket_id,
        agent_id=agent_id,
        turn_number=turn_number,
        diff_stats=turn_diff_stats,
    )

    if emit_event is not None and isinstance(turn_diff_stats, dict):
        try:
            event_payload = {
                "ticket_id": current_ticket_id,
                "ticket_path": None,
                "dispatch_seq": (
                    turn_summary.seq if turn_summary else turn_summary_seq
                ),
                "insertions": int(turn_diff_stats.get("insertions") or 0),
                "deletions": int(turn_diff_stats.get("deletions") or 0),
                "files_changed": int(turn_diff_stats.get("files_changed") or 0),
            }
            if current_ticket_path is not None:
                event_payload["ticket_path"] = str(current_ticket_path)
                event_payload["ticket_key"] = ticket_instance_token(current_ticket_path)
            emit_event(FlowEventType.DIFF_UPDATED, event_payload)
        except Exception:
            pass

    return dispatch, None


def check_ticket_frontmatter(
    *,
    ticket_path: Path,
) -> tuple[Optional[Any], Optional[list[str]]]:
    """Check ticket frontmatter after turn execution."""
    fm, errors = read_ticket_frontmatter(ticket_path)
    if errors and any(err.startswith("Failed to read ticket:") for err in errors):
        return None, [
            err.replace(
                "Failed to read ticket:", "Failed to read ticket after turn:", 1
            )
            for err in errors
        ]
    return fm, errors


def create_runner_pause_dispatch(
    *,
    outbox_paths: Any,
    state: dict[str, Any],
    ticket_id: str,
    ticket_path: Optional[str] = None,
    repo_id: str,
    run_id: str,
    title: str,
    body: str,
) -> Optional[Any]:
    """Create and archive a runner-generated pause dispatch."""
    try:
        outbox_paths.dispatch_path.write_text(
            f"---\nmode: pause\ntitle: {title}\n---\n\n{body}\n",
            encoding="utf-8",
        )
    except OSError:
        return None
    next_seq = int(state.get("dispatch_seq") or 0) + 1
    dispatch_ticket_id = ticket_path or ticket_id
    dispatch_record, dispatch_errors = archive_dispatch(
        outbox_paths,
        next_seq=next_seq,
        ticket_id=dispatch_ticket_id,
        repo_id=repo_id,
        run_id=run_id,
        origin="runner",
    )
    if dispatch_errors:
        return None
    if dispatch_record is not None:
        state["dispatch_seq"] = dispatch_record.seq
    return dispatch_record


def checkpoint_git(
    *,
    workspace_root: Path,
    run_id: str,
    turn: int,
    agent: str,
    checkpoint_message_template: str,
) -> Optional[str]:
    """Create a best-effort git commit checkpoint."""
    try:
        status_proc = run_git(["status", "--porcelain"], cwd=workspace_root, check=True)
        if not (status_proc.stdout or "").strip():
            return None
        run_git(["add", "-A"], cwd=workspace_root, check=True)
        msg = checkpoint_message_template.format(
            run_id=run_id,
            turn=turn,
            agent=agent,
        )
        run_git(["commit", "-m", msg], cwd=workspace_root, check=True)
        return None
    except Exception as exc:
        _logger.exception("Checkpoint commit failed")
        return str(exc)


def get_repo_fingerprint(workspace_root: Path) -> Optional[str]:
    """Return a stable snapshot of HEAD + porcelain status."""
    try:
        head_proc = run_git(["rev-parse", "HEAD"], cwd=workspace_root, check=True)
        status_proc = run_git(["status", "--porcelain"], cwd=workspace_root, check=True)
        head = (head_proc.stdout or "").strip()
        status = (status_proc.stdout or "").strip()
        if not head:
            return None
        return f"{head}\n{status}"
    except Exception:
        return None


def build_pause_result(
    *,
    state: dict[str, Any],
    reason: str,
    reason_code: str = "needs_user_fix",
    reason_details: Optional[str] = None,
    current_ticket: Optional[str] = None,
    workspace_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Build a pause result with proper state updates."""
    state = dict(state)
    state["status"] = "paused"
    state["reason"] = reason
    state["reason_code"] = reason_code

    pause_context: dict[str, Any] = {
        "paused_reply_seq": int(state.get("reply_seq") or 0),
    }

    if workspace_root is not None:
        fingerprint = get_repo_fingerprint(workspace_root)
        if isinstance(fingerprint, str):
            pause_context["repo_fingerprint"] = fingerprint

    state["pause_context"] = pause_context

    if reason_details:
        state["reason_details"] = reason_details
    else:
        state.pop("reason_details", None)

    return {
        "status": "paused",
        "state": state,
        "reason": reason,
        "reason_details": reason_details,
        "current_ticket": current_ticket
        or (
            state.get("current_ticket")
            if isinstance(state.get("current_ticket"), str)
            else None
        ),
    }


def process_commit_required(
    *,
    state: dict[str, Any],
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
        detail = (status_after_agent or "").strip()
        detail_lines = detail.splitlines()[:20]
        details_parts = [
            "Please commit manually (ensuring pre-commit hooks pass) and resume."
        ]
        if detail_lines:
            details_parts.append(
                "\n\nWorking tree status (git status --porcelain):\n- "
                + "\n- ".join(detail_lines)
            )
        reason = (
            f"Commit failed after {max_commit_retries} attempts. "
            "Manual commit required."
        )
        reason_details = "".join(details_parts)

    return commit_state, status, reason, reason_code, reason_details
