from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..core.file_chat_keys import ticket_instance_token
from ..core.flows.models import FlowEventType
from ..core.git_utils import GitError, git_diff_stats, run_git
from .files import read_ticket_frontmatter
from .models import TicketResult
from .outbox import (
    archive_dispatch,
    create_turn_summary,
)

_logger = logging.getLogger(__name__)

MAX_LINT_RETRIES = 3


@dataclass
class PostTurnArchivalResult:
    dispatch: Optional[Any] = None
    turn_summary: Optional[Any] = None
    dispatch_errors: Optional[list[str]] = None


@dataclass
class FrontmatterRecheckResult:
    updated_frontmatter: Any = None
    fm_errors: Optional[list[str]] = None
    should_pause: bool = False
    should_retry: bool = False
    lint_state: Optional[dict[str, Any]] = None
    pause_reason: Optional[str] = None
    pause_reason_details: Optional[str] = None
    pause_reason_code: str = "needs_user_fix"


def archive_dispatch_and_create_summary(
    *,
    workspace_root: Path,
    outbox_paths: Any,
    current_ticket_id: str,
    current_ticket_path: Optional[str] = None,
    current_ticket_path_obj: Optional[Path] = None,
    repo_id: str,
    run_id: str,
    dispatch_seq: int,
    agent_output: str,
    agent_id: str,
    turn_number: int,
    head_before_turn: Optional[str] = None,
    emit_event: Optional[Any] = None,
) -> PostTurnArchivalResult:
    dispatch_ticket_id = current_ticket_path or current_ticket_id
    dispatch, dispatch_errors = archive_dispatch(
        outbox_paths,
        next_seq=dispatch_seq + 1,
        ticket_id=dispatch_ticket_id,
        repo_id=repo_id,
        run_id=run_id,
        origin="runner",
    )

    if dispatch_errors:
        return PostTurnArchivalResult(dispatch_errors=dispatch_errors)

    after_dispatch_seq = dispatch.seq if dispatch is not None else dispatch_seq
    turn_summary_seq = after_dispatch_seq + 1

    turn_diff_stats = None
    try:
        if head_before_turn:
            turn_diff_stats = git_diff_stats(
                workspace_root,
                from_ref=head_before_turn,
            )
        else:
            turn_diff_stats = git_diff_stats(
                workspace_root,
                from_ref=None,
                include_staged=True,
            )
    except (OSError, ValueError, RuntimeError):
        turn_diff_stats = None

    turn_summary, _turn_summary_errors = create_turn_summary(
        outbox_paths,
        next_seq=turn_summary_seq,
        agent_output=agent_output or "",
        ticket_id=dispatch_ticket_id,
        agent_id=agent_id,
        turn_number=turn_number,
    )

    if emit_event is not None and isinstance(turn_diff_stats, dict):
        try:
            event_payload = {
                "ticket_id": current_ticket_id,
                "ticket_path": current_ticket_path,
                "dispatch_seq": (
                    turn_summary.seq if turn_summary else turn_summary_seq
                ),
                "insertions": int(turn_diff_stats.get("insertions") or 0),
                "deletions": int(turn_diff_stats.get("deletions") or 0),
                "files_changed": int(turn_diff_stats.get("files_changed") or 0),
            }
            if current_ticket_path_obj is not None:
                event_payload["ticket_key"] = ticket_instance_token(
                    current_ticket_path_obj,
                )
            emit_event(FlowEventType.DIFF_UPDATED, event_payload)
        except Exception:
            _logger.debug("failed to emit diff_updated event", exc_info=True)

    return PostTurnArchivalResult(
        dispatch=dispatch,
        turn_summary=turn_summary,
    )


def check_ticket_frontmatter(
    *,
    ticket_path: Path,
) -> tuple[Optional[Any], Optional[list[str]]]:
    fm, errors = read_ticket_frontmatter(ticket_path)
    if errors and any(err.startswith("Failed to read ticket:") for err in errors):
        return None, [
            err.replace(
                "Failed to read ticket:", "Failed to read ticket after turn:", 1
            )
            for err in errors
        ]
    return fm, errors


def handle_frontmatter_recheck(
    *,
    ticket_path: Path,
    lint_errors: Optional[list[str]],
    lint_retries: int,
    max_lint_retries: int,
    agent_conversation_id: Optional[str],
) -> FrontmatterRecheckResult:
    updated_fm, fm_errors = check_ticket_frontmatter(ticket_path=ticket_path)

    if not fm_errors:
        return FrontmatterRecheckResult(updated_frontmatter=updated_fm)

    next_retries = lint_retries + 1
    if next_retries > max_lint_retries:
        return FrontmatterRecheckResult(
            updated_frontmatter=updated_fm,
            fm_errors=fm_errors,
            should_pause=True,
            pause_reason="Ticket frontmatter invalid. Manual fix required.",
            pause_reason_details=(
                "Exceeded lint retry limit. Fix the ticket frontmatter manually and resume.\n\n"
                "Errors:\n- " + "\n- ".join(fm_errors)
            ),
            pause_reason_code="needs_user_fix",
        )

    return FrontmatterRecheckResult(
        updated_frontmatter=updated_fm,
        fm_errors=fm_errors,
        should_retry=True,
        lint_state={
            "errors": fm_errors,
            "retries": next_retries,
            "conversation_id": agent_conversation_id,
        },
    )


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
    except GitError as exc:
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
    except GitError:
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
    clean_after_agent: Optional[bool],
    commit_pending: bool,
    commit_retries: int,
    head_before_turn: Optional[str],
    head_after_agent: Optional[str],
    agent_committed_this_turn: Optional[bool],
    status_after_agent: Optional[str],
    max_commit_retries: int,
) -> tuple[dict[str, Any], str, Optional[str], str, Optional[str]]:
    from .runner_commit import process_commit_required as _process_commit_required

    return _process_commit_required(
        clean_after_agent=clean_after_agent,
        commit_pending=commit_pending,
        commit_retries=commit_retries,
        head_before_turn=head_before_turn,
        head_after_agent=head_after_agent,
        agent_committed_this_turn=agent_committed_this_turn,
        status_after_agent=status_after_agent,
        max_commit_retries=max_commit_retries,
    )


def apply_successful_turn_state(
    *,
    state: dict[str, Any],
    agent_text: Optional[str],
    agent_id: Optional[str],
    agent_conversation_id: Optional[str],
    agent_turn_id: Optional[str],
    reply_max_seq: int,
    reply_seq: int,
) -> dict[str, Any]:
    state = dict(state)
    if reply_max_seq > reply_seq:
        state["reply_seq"] = reply_max_seq
    state["last_agent_output"] = agent_text
    state.pop("network_retry", None)
    state["last_agent_id"] = agent_id
    state["last_agent_conversation_id"] = agent_conversation_id
    state["last_agent_turn_id"] = agent_turn_id
    return state


def apply_loop_guard_state(
    *,
    state: dict[str, Any],
    loop_guard_result: dict[str, Any],
) -> dict[str, Any]:
    state = dict(state)
    if "loop_guard" in loop_guard_result:
        state["loop_guard"] = loop_guard_result["loop_guard"]
    return state


def build_loop_guard_pause_result(
    *,
    state: dict[str, Any],
    current_ticket_path: str,
    loop_guard_updates: dict[str, Any],
    dispatch_record: Any,
    agent_text: Optional[str],
    agent_id: Optional[str],
    agent_conversation_id: Optional[str],
    agent_turn_id: Optional[str],
    workspace_root: Path,
) -> TicketResult:
    no_change_count = loop_guard_updates.get("no_change_count", 0)
    reason = (
        "Ticket appears stuck: same ticket ran twice with no repository diff changes."
    )
    details = (
        "Runner paused to avoid repeated no-op work.\n\n"
        f"Ticket: {current_ticket_path}\n"
        f"Consecutive no-change turns: {no_change_count}\n\n"
        "Please provide unblock guidance via reply, or change repository state, then resume. "
        "Use force resume only if you intentionally want to retry unchanged."
    )
    paused = build_pause_result(
        state=state,
        reason=reason,
        reason_code="loop_no_diff",
        reason_details=details,
        current_ticket=current_ticket_path,
        workspace_root=workspace_root,
    )
    return TicketResult(
        status="paused",
        state=paused["state"],
        reason=paused["reason"],
        reason_details=paused["reason_details"],
        dispatch=dispatch_record,
        current_ticket=paused["current_ticket"],
        agent_output=agent_text,
        agent_id=agent_id,
        agent_conversation_id=agent_conversation_id,
        agent_turn_id=agent_turn_id,
    )


def build_dispatch_pause_result(
    *,
    state: dict[str, Any],
    dispatch: Any,
    checkpoint_error: Optional[str],
    current_ticket_rel_path: str,
    agent_text: Optional[str],
    agent_id: Optional[str],
    agent_conversation_id: Optional[str],
    agent_turn_id: Optional[str],
    workspace_root: Path,
) -> TicketResult:
    reason = dispatch.dispatch.title or "Paused for user input."
    if checkpoint_error:
        reason += f"\n\nNote: checkpoint commit failed: {checkpoint_error}"
    paused = build_pause_result(
        state=state,
        reason=reason,
        reason_code="user_pause",
        current_ticket=current_ticket_rel_path,
        workspace_root=workspace_root,
    )
    return TicketResult(
        status="paused",
        state=paused["state"],
        reason=paused["reason"],
        dispatch=dispatch,
        current_ticket=current_ticket_rel_path,
        agent_output=agent_text,
        agent_id=agent_id,
        agent_conversation_id=agent_conversation_id,
        agent_turn_id=agent_turn_id,
    )


def apply_completion_cleanup(
    *,
    state: dict[str, Any],
    updated_fm: Any,
) -> dict[str, Any]:
    state = dict(state)
    if updated_fm and updated_fm.done:
        state.pop("commit", None)
        state.pop("current_ticket", None)
        state.pop("current_ticket_id", None)
        state.pop("ticket_turns", None)
        state.pop("last_agent_output", None)
        state.pop("lint", None)
    else:
        state.pop("commit", None)
    return state


def apply_checkpoint_error_state(
    *,
    state: dict[str, Any],
    checkpoint_error: Optional[str],
) -> dict[str, Any]:
    state = dict(state)
    if checkpoint_error:
        state["last_checkpoint_error"] = checkpoint_error
    else:
        state.pop("last_checkpoint_error", None)
    return state


@dataclass
class _ReconcileInputs:
    state: dict[str, Any]
    workspace_root: Path
    run_id: str
    repo_id: str
    outbox_paths: Any
    current_ticket_id: str
    current_ticket_path: str
    current_ticket_path_obj: Path
    canonical_agent_id: str
    current_ticket_profile: Optional[str]
    result: Any
    total_turns: int
    head_before_turn: Optional[str]
    repo_fingerprint_before_turn: Optional[str]
    git_state_after: dict[str, Any]
    lint_errors: list[str]
    lint_retries: int
    commit_pending: bool
    commit_retries: int
    max_lint_retries: int
    max_commit_retries: int
    auto_commit: bool
    checkpoint_message_template: str
    emit_event: Optional[Any]


def reconcile_post_turn(
    *,
    state: dict[str, Any],
    workspace_root: Path,
    run_id: str,
    repo_id: str,
    outbox_paths: Any,
    current_ticket_id: str,
    current_ticket_path: str,
    current_ticket_path_obj: Path,
    canonical_agent_id: str,
    current_ticket_profile: Optional[str],
    result: Any,
    total_turns: int,
    head_before_turn: Optional[str],
    repo_fingerprint_before_turn: Optional[str],
    git_state_after: dict[str, Any],
    lint_errors: list[str],
    lint_retries: int,
    commit_pending: bool,
    commit_retries: int,
    max_lint_retries: int,
    max_commit_retries: int,
    auto_commit: bool,
    checkpoint_message_template: str,
    emit_event: Optional[Any],
) -> TicketResult:
    from .runner_commit import process_commit_required as _process_commit_required
    from .runner_execution import compute_loop_guard, should_pause_for_loop
    from .runner_thread_bindings import clear_ticket_thread_binding

    dispatch_seq = int(state.get("dispatch_seq") or 0)

    archival = archive_dispatch_and_create_summary(
        workspace_root=workspace_root,
        outbox_paths=outbox_paths,
        current_ticket_id=current_ticket_id,
        current_ticket_path=current_ticket_path,
        current_ticket_path_obj=current_ticket_path_obj,
        repo_id=repo_id,
        run_id=run_id,
        dispatch_seq=dispatch_seq,
        agent_output=result.text or "",
        agent_id=result.agent_id or "",
        turn_number=total_turns,
        head_before_turn=head_before_turn,
        emit_event=emit_event,
    )

    if archival.dispatch_errors:
        state["outbox_lint"] = archival.dispatch_errors
        paused = build_pause_result(
            state=state,
            reason="Invalid DISPATCH.md frontmatter.",
            reason_details="Errors:\n- " + "\n- ".join(archival.dispatch_errors),
            current_ticket=current_ticket_path,
            workspace_root=workspace_root,
        )
        return TicketResult(
            status="paused",
            state=paused["state"],
            reason=paused["reason"],
            reason_details=paused["reason_details"],
            current_ticket=paused["current_ticket"],
        )

    dispatch = archival.dispatch
    if dispatch is not None:
        state["dispatch_seq"] = dispatch.seq
        state.pop("outbox_lint", None)
    if archival.turn_summary is not None:
        state["dispatch_seq"] = archival.turn_summary.seq

    repo_fingerprint_after = git_state_after["repo_fingerprint_after"]
    lint_retry_mode = bool(lint_errors)
    loop_guard_result = compute_loop_guard(
        state=state,
        current_ticket_id=current_ticket_id,
        repo_fingerprint_before=repo_fingerprint_before_turn,
        repo_fingerprint_after=repo_fingerprint_after,
        lint_retry_mode=lint_retry_mode,
    )
    state = dict(
        apply_loop_guard_state(state=state, loop_guard_result=loop_guard_result)
    )
    loop_guard_updates = loop_guard_result.get("loop_guard_updates", {})

    if should_pause_for_loop(loop_guard_updates=loop_guard_updates):
        no_change_count = loop_guard_updates.get("no_change_count", 0)
        details = (
            "Runner paused to avoid repeated no-op work.\n\n"
            f"Ticket: {current_ticket_path}\n"
            f"Consecutive no-change turns: {no_change_count}\n\n"
            "Please provide unblock guidance via reply, or change repository state, then resume. "
            "Use force resume only if you intentionally want to retry unchanged."
        )
        dispatch_record = create_runner_pause_dispatch(
            outbox_paths=outbox_paths,
            state=state,
            ticket_id=current_ticket_id,
            ticket_path=current_ticket_path,
            repo_id=repo_id,
            run_id=run_id,
            title="Ticket loop detected (no repo diff change)",
            body=details,
        )
        return build_loop_guard_pause_result(
            state=state,
            current_ticket_path=current_ticket_path,
            loop_guard_updates=loop_guard_updates,
            dispatch_record=dispatch_record,
            agent_text=result.text,
            agent_id=result.agent_id,
            agent_conversation_id=result.conversation_id,
            agent_turn_id=result.turn_id,
            workspace_root=workspace_root,
        )

    fm_result = handle_frontmatter_recheck(
        ticket_path=current_ticket_path_obj,
        lint_errors=lint_errors if lint_errors else None,
        lint_retries=lint_retries,
        max_lint_retries=max_lint_retries,
        agent_conversation_id=result.conversation_id,
    )

    if fm_result.should_pause:
        paused = build_pause_result(
            state=state,
            reason=fm_result.pause_reason or "Ticket frontmatter invalid.",
            reason_details=fm_result.pause_reason_details,
            current_ticket=current_ticket_path,
            workspace_root=workspace_root,
        )
        return TicketResult(
            status="paused",
            state=paused["state"],
            reason=paused["reason"],
            reason_details=paused["reason_details"],
            current_ticket=paused["current_ticket"],
        )

    if fm_result.should_retry:
        full_lint_state = dict(fm_result.lint_state or {})
        full_lint_state.update(
            {
                "ticket_id": current_ticket_id,
                "ticket_path": current_ticket_path,
                "agent_id": canonical_agent_id,
                "profile": current_ticket_profile,
            }
        )
        state["lint"] = full_lint_state
        return TicketResult(
            status="continue",
            state=state,
            reason="Ticket frontmatter invalid; requesting agent fix.",
            current_ticket=current_ticket_path,
            agent_output=result.text,
            agent_id=result.agent_id,
            agent_conversation_id=result.conversation_id,
            agent_turn_id=result.turn_id,
        )

    if state.get("lint"):
        state.pop("lint", None)

    updated_fm = fm_result.updated_frontmatter
    head_after_agent = git_state_after["head_after_turn"]
    clean_after_agent = git_state_after["clean_after_turn"]
    status_after_agent = git_state_after["status_after_turn"]
    agent_committed_this_turn = git_state_after["agent_committed_this_turn"]

    commit_required_now = bool(
        updated_fm and updated_fm.done and clean_after_agent is False
    )
    checkpoint_error = None
    if auto_commit and not commit_pending and not commit_required_now:
        checkpoint_error = checkpoint_git(
            workspace_root=workspace_root,
            run_id=run_id,
            turn=total_turns,
            agent=result.agent_id or "unknown",
            checkpoint_message_template=checkpoint_message_template,
        )

    if dispatch is not None and dispatch.dispatch.mode == "pause":
        return build_dispatch_pause_result(
            state=state,
            dispatch=dispatch,
            checkpoint_error=checkpoint_error,
            current_ticket_rel_path=current_ticket_path,
            agent_text=result.text,
            agent_id=result.agent_id,
            agent_conversation_id=result.conversation_id,
            agent_turn_id=result.turn_id,
            workspace_root=workspace_root,
        )

    if updated_fm and updated_fm.done:
        if clean_after_agent is False:
            (
                commit_state_update,
                commit_status,
                commit_reason,
                commit_reason_code,
                commit_reason_details,
            ) = _process_commit_required(
                clean_after_agent=clean_after_agent,
                commit_pending=commit_pending,
                commit_retries=commit_retries,
                head_before_turn=head_before_turn,
                head_after_agent=head_after_agent,
                agent_committed_this_turn=agent_committed_this_turn,
                status_after_agent=status_after_agent,
                max_commit_retries=max_commit_retries,
            )
            if commit_state_update:
                state["commit"] = commit_state_update
            if commit_reason is not None:
                paused = build_pause_result(
                    state=state,
                    reason=commit_reason,
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
                )

            return TicketResult(
                status=commit_status or "continue",
                state=state,
                reason="Ticket done but commit required; requesting agent commit.",
                current_ticket=current_ticket_path,
                agent_output=result.text,
                agent_id=result.agent_id,
                agent_conversation_id=result.conversation_id,
                agent_turn_id=result.turn_id,
            )

        state.pop("commit", None)
        clear_ticket_thread_binding(
            state,
            ticket_id=current_ticket_id,
            reason="ticket_completed",
        )
        from ..core.apps.hooks import execute_matching_installed_app_hooks

        hook_result = execute_matching_installed_app_hooks(
            workspace_root,
            "after_ticket_done",
            workspace_root=workspace_root,
            flow_run_id=run_id,
            ticket_id=current_ticket_id,
            ticket_path=current_ticket_path_obj,
            ticket_frontmatter=updated_fm,
            emit_event=emit_event,
        )
        if hook_result.paused:
            paused = build_pause_result(
                state=state,
                reason=hook_result.reason or "App hook requested pause.",
                reason_code="app_hook_failed",
                reason_details=hook_result.reason_details,
                current_ticket=current_ticket_path,
                workspace_root=workspace_root,
            )
            return TicketResult(
                status="paused",
                state=paused["state"],
                reason=paused["reason"],
                reason_details=paused["reason_details"],
                current_ticket=paused["current_ticket"],
                agent_output=result.text,
                agent_id=result.agent_id,
                agent_conversation_id=result.conversation_id,
                agent_turn_id=result.turn_id,
            )
        if hook_result.failed:
            return TicketResult(
                status="failed",
                state=state,
                reason=hook_result.reason or "App hook failed.",
                reason_details=hook_result.reason_details,
                current_ticket=current_ticket_path,
                agent_output=result.text,
                agent_id=result.agent_id,
                agent_conversation_id=result.conversation_id,
                agent_turn_id=result.turn_id,
            )
        state = dict(apply_completion_cleanup(state=state, updated_fm=updated_fm))
    else:
        state.pop("commit", None)

    state = dict(
        apply_checkpoint_error_state(state=state, checkpoint_error=checkpoint_error)
    )

    return TicketResult(
        status="continue",
        state=state,
        reason="Turn complete.",
        dispatch=dispatch,
        current_ticket=current_ticket_path,
        agent_output=result.text,
        agent_id=result.agent_id,
        agent_conversation_id=result.conversation_id,
        agent_turn_id=result.turn_id,
    )
