from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..core.git_utils import run_git
from .agent_pool import AgentPool, AgentTurnRequest
from .runner_types import TurnExecutionResult

_logger = logging.getLogger(__name__)

LOOP_NO_CHANGE_THRESHOLD = 2


def is_network_error(error_message: str) -> bool:
    """Check if an error message indicates a transient network issue."""
    if not error_message:
        return False
    error_lower = error_message.lower()
    network_indicators = [
        "network error",
        "connection",
        "timeout",
        "transport error",
        "disconnected",
        "unreachable",
        "reconnecting",
        "connection refused",
        "connection reset",
        "connection broken",
        "temporary failure",
    ]
    return any(indicator in error_lower for indicator in network_indicators)


async def execute_turn(
    *,
    agent_pool: AgentPool,
    agent_id: str,
    prompt: str,
    workspace_root: Path,
    conversation_id: Optional[str] = None,
    options: Optional[dict[str, Any]] = None,
    emit_event: Optional[Any] = None,
    max_network_retries: int = 5,
    current_network_retries: int = 0,
) -> TurnExecutionResult:
    """Execute an agent turn and return structured result."""
    turn_options = options if options else {}
    req = AgentTurnRequest(
        agent_id=agent_id,
        prompt=prompt,
        workspace_root=workspace_root,
        conversation_id=conversation_id,
        emit_event=emit_event,
        options=turn_options if turn_options else None,
    )

    result = await agent_pool.run_turn(req)

    if result.error:
        is_net_err = is_network_error(result.error)
        should_retry = is_net_err and current_network_retries < max_network_retries
        return TurnExecutionResult(
            success=False,
            error=result.error,
            text=result.text,
            agent_id=result.agent_id,
            conversation_id=result.conversation_id,
            turn_id=result.turn_id,
            is_network_error=is_net_err,
            should_retry=should_retry,
            network_retries=current_network_retries + (1 if is_net_err else 0),
        )

    return TurnExecutionResult(
        success=True,
        error=None,
        text=result.text,
        agent_id=result.agent_id,
        conversation_id=result.conversation_id,
        turn_id=result.turn_id,
        is_network_error=False,
        should_retry=False,
        network_retries=0,
    )


def capture_git_state(*, workspace_root: Path) -> dict[str, Any]:
    """Capture git HEAD and status before turn execution."""
    head_before_turn: Optional[str] = None
    repo_fingerprint_before: Optional[str] = None

    try:
        head_proc = run_git(["rev-parse", "HEAD"], cwd=workspace_root, check=True)
        head_before_turn = (head_proc.stdout or "").strip() or None
        status_proc = run_git(["status", "--porcelain"], cwd=workspace_root, check=True)
        status_before = (status_proc.stdout or "").strip() or ""
        if head_before_turn:
            repo_fingerprint_before = f"{head_before_turn}\n{status_before}"
    except Exception:
        head_before_turn = None
        repo_fingerprint_before = None

    return {
        "head_before_turn": head_before_turn,
        "repo_fingerprint_before": repo_fingerprint_before,
    }


def capture_git_state_after(
    *,
    workspace_root: Path,
    head_before_turn: Optional[str],
) -> dict[str, Any]:
    """Capture git HEAD and status after turn execution."""
    head_after_turn: Optional[str] = None
    clean_after_turn: Optional[bool] = None
    status_after_turn: Optional[str] = None
    agent_committed_this_turn: Optional[bool] = None
    repo_fingerprint_after: Optional[str] = None

    try:
        head_proc = run_git(["rev-parse", "HEAD"], cwd=workspace_root, check=True)
        head_after_turn = (head_proc.stdout or "").strip() or None
        status_proc = run_git(["status", "--porcelain"], cwd=workspace_root, check=True)
        status_after_turn = (status_proc.stdout or "").strip()
        clean_after_turn = not bool(status_after_turn)
        if head_before_turn and head_after_turn:
            agent_committed_this_turn = head_after_turn != head_before_turn
        if head_after_turn:
            repo_fingerprint_after = f"{head_after_turn}\n{status_after_turn}"
    except Exception:
        head_after_turn = None
        clean_after_turn = None
        status_after_turn = None
        agent_committed_this_turn = None
        repo_fingerprint_after = None

    return {
        "head_after_turn": head_after_turn,
        "clean_after_turn": clean_after_turn,
        "status_after_turn": status_after_turn,
        "agent_committed_this_turn": agent_committed_this_turn,
        "repo_fingerprint_after": repo_fingerprint_after,
    }


def compute_loop_guard(
    *,
    state: dict[str, Any],
    current_ticket_id: str,
    repo_fingerprint_before: Optional[str],
    repo_fingerprint_after: Optional[str],
    lint_retry_mode: bool,
) -> dict[str, Any]:
    """Compute loop guard state after turn execution."""
    loop_guard_raw = state.get("loop_guard")
    loop_guard_state = dict(loop_guard_raw) if isinstance(loop_guard_raw, dict) else {}

    no_repo_change_this_turn = (
        isinstance(repo_fingerprint_before, str)
        and isinstance(repo_fingerprint_after, str)
        and repo_fingerprint_before == repo_fingerprint_after
    )

    if lint_retry_mode:
        return {"loop_guard": None}

    prev_ticket = loop_guard_state.get("ticket")
    prev_count = int(loop_guard_state.get("no_change_count") or 0)

    if (
        no_repo_change_this_turn
        and isinstance(prev_ticket, str)
        and prev_ticket == current_ticket_id
    ):
        no_change_count = prev_count + 1
    elif no_repo_change_this_turn:
        no_change_count = 1
    else:
        no_change_count = 0

    return {
        "loop_guard": {
            "ticket": current_ticket_id,
            "no_change_count": no_change_count,
        },
        "loop_guard_updates": {
            "ticket": current_ticket_id,
            "no_change_count": no_change_count,
            "no_repo_change_this_turn": no_repo_change_this_turn,
        },
    }


def should_pause_for_loop(
    *,
    loop_guard_updates: dict[str, Any],
) -> bool:
    """Check if we should pause due to loop guard."""
    no_change_count = int(loop_guard_updates.get("no_change_count") or 0)
    return no_change_count >= LOOP_NO_CHANGE_THRESHOLD
