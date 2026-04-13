from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from ..core.flows.models import FlowEventType
from . import runner_commit, runner_post_turn, runner_selection
from .agent_pool import AgentPool
from .files import safe_relpath
from .models import TicketResult, TicketRunConfig
from .outbox import (
    ensure_outbox_dirs,
    resolve_outbox_paths,
)
from .replies import (
    dispatch_reply,
    ensure_reply_dirs,
    next_reply_seq,
    resolve_reply_paths,
)
from .runner_execution import (
    capture_git_state,
    capture_git_state_after,
    compute_loop_guard,
    execute_turn,
    is_network_error,
    should_pause_for_loop,
)
from .runner_prompt import (
    CAR_HUD_MAX_CHARS,  # noqa: F401  # re-exported for backwards compatibility
    CAR_HUD_MAX_LINES,  # noqa: F401  # re-exported for backwards compatibility
    TRUNCATION_MARKER,  # noqa: F401  # re-exported for backwards compatibility
    _preserve_ticket_structure,  # noqa: F401  # re-exported for backwards compatibility
    _shrink_prompt,  # noqa: F401  # re-exported for backwards compatibility
    _truncate_text_by_bytes,  # noqa: F401  # re-exported for backwards compatibility
    build_prompt,
)

_is_network_error = is_network_error

_logger = logging.getLogger(__name__)
TICKET_CONTEXT_TOTAL_MAX_BYTES = runner_selection.TICKET_CONTEXT_TOTAL_MAX_BYTES
load_ticket_context_block = runner_selection.load_ticket_context_block
_load_ticket_context_block = (
    runner_selection.load_ticket_context_block
)  # noqa: F401  # backward compat re-export


class TicketRunner:
    """Execute a ticket directory one agent turn at a time.

    This runner is intentionally small and file-backed:
    - Tickets are markdown files under `config.ticket_dir`.
    - User messages + optional attachments are written under `.codex-autorunner/runs/<run_id>/`.
    - The orchestrator is stateless aside from the `state` dict passed into step().
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        run_id: str,
        config: TicketRunConfig,
        agent_pool: AgentPool,
        repo_id: str = "",
    ):
        self._workspace_root = workspace_root
        self._run_id = run_id
        self._config = config
        self._agent_pool = agent_pool
        self._repo_id = repo_id

    async def step(
        self,
        state: dict[str, Any],
        *,
        emit_event: Optional[Callable[[FlowEventType, dict[str, Any]], None]] = None,
    ) -> TicketResult:
        """Execute exactly one orchestration step.

        A step is either:
        - run one agent turn for the current ticket, or
        - pause because prerequisites are missing, or
        - mark the whole run completed (no remaining tickets).
        """

        state = dict(state or {})
        # Clear transient reason from previous pause/resume cycles.
        state.pop("reason", None)

        total_turns = int(state.get("total_turns") or 0)

        _network_raw = state.get("network_retry")
        network_retry_state: dict[str, Any] = (
            _network_raw if isinstance(_network_raw, dict) else {}
        )
        network_retries = int(network_retry_state.get("retries") or 0)
        if total_turns >= self._config.max_total_turns:
            return self._pause(
                state,
                reason=f"Max turns reached ({self._config.max_total_turns}). Review tickets and resume.",
                reason_code="max_turns",
            )

        ticket_dir = self._workspace_root / self._config.ticket_dir
        # Ensure outbox dirs exist.
        outbox_paths = resolve_outbox_paths(
            workspace_root=self._workspace_root,
            run_id=self._run_id,
        )
        ensure_outbox_dirs(outbox_paths)

        # Ensure reply inbox dirs exist (human -> agent messages).
        reply_paths = resolve_reply_paths(
            workspace_root=self._workspace_root,
            run_id=self._run_id,
        )
        ensure_reply_dirs(reply_paths)
        if reply_paths.user_reply_path.exists():
            next_seq = next_reply_seq(reply_paths.reply_history_dir)
            archived, errors = dispatch_reply(reply_paths, next_seq=next_seq)
            if errors:
                return self._pause(
                    state,
                    reason="Failed to archive USER_REPLY.md.",
                    reason_details="Errors:\n- " + "\n- ".join(errors),
                    reason_code="needs_user_fix",
                )
            if archived is None:
                return self._pause(
                    state,
                    reason="Failed to archive USER_REPLY.md.",
                    reason_details="Errors:\n- Failed to archive reply",
                    reason_code="needs_user_fix",
                )

        selection_result = runner_selection.select_ticket(
            workspace_root=self._workspace_root,
            ticket_dir=ticket_dir,
            config=self._config,
            state=state,
            emit_event=emit_event,
        )
        for key, value in selection_result.state_updates.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        if selection_result.status == "paused":
            return self._pause(
                state,
                reason=selection_result.pause_reason or "Paused",
                reason_details=selection_result.pause_reason_details,
                reason_code=selection_result.pause_reason_code or "needs_user_fix",
            )
        if selection_result.status == "completed":
            state["status"] = "completed"
            return TicketResult(
                status="completed",
                state=state,
                reason=selection_result.pause_reason or "All tickets done.",
            )

        if not selection_result.selected:
            return self._pause(
                state,
                reason="Ticket selection failed unexpectedly.",
                reason_code="infra_error",
            )

        current_path = selection_result.selected.path

        # Pre-turn planning: validate, load context, build prompt.
        plan = runner_selection.plan_pre_turn(
            selection_result=selection_result,
            workspace_root=self._workspace_root,
            ticket_dir=ticket_dir,
            config=self._config,
            state=state,
            run_id=self._run_id,
            outbox_paths=outbox_paths,
            reply_paths=reply_paths,
        )
        for key, value in plan.state_updates.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value

        if plan.status == "paused":
            return self._pause(
                state,
                reason=plan.pause_reason or "Paused",
                reason_details=plan.pause_reason_details,
                reason_code=plan.pause_reason_code or "needs_user_fix",
                current_ticket=plan.current_ticket_path,
            )
        if plan.status == "failed":
            return TicketResult(
                status="failed",
                state=state,
                reason=plan.pause_reason or "Required ticket context file missing.",
                reason_details=plan.pause_reason_details,
                current_ticket=plan.current_ticket_path,
            )
        if plan.status == "skip":
            return TicketResult(status="continue", state=state)

        # Extract plan outputs for execution and post-turn phases.
        commit_pending = plan.commit_pending
        commit_retries = plan.commit_retries
        lint_errors = plan.lint_errors
        lint_retries = plan.lint_retries
        reuse_conversation_id = plan.conversation_id
        reply_seq = plan.reply_seq
        reply_max_seq = plan.reply_max_seq
        current_ticket_id: str = plan.current_ticket_id  # type: ignore[assignment]
        current_ticket_path: str = plan.current_ticket_path  # type: ignore[assignment]
        prompt: str = plan.prompt  # type: ignore[assignment]

        # Increment turn counters.
        total_turns += 1
        ticket_turns = int(state.get("ticket_turns") or 0) + 1
        state["total_turns"] = total_turns
        state["ticket_turns"] = ticket_turns

        git_state_before = capture_git_state(workspace_root=self._workspace_root)
        repo_fingerprint_before_turn = git_state_before["repo_fingerprint_before"]
        head_before_turn = git_state_before["head_before_turn"]

        result = await execute_turn(
            agent_pool=self._agent_pool,
            agent_id=plan.ticket_doc.frontmatter.agent,
            prompt=prompt,
            workspace_root=self._workspace_root,
            conversation_id=reuse_conversation_id,
            options=plan.turn_options,
            emit_event=emit_event,
            max_network_retries=self._config.max_network_retries,
            current_network_retries=network_retries,
        )
        if not result.success:
            state["last_agent_output"] = result.text
            state["last_agent_id"] = result.agent_id
            state["last_agent_conversation_id"] = result.conversation_id
            state["last_agent_turn_id"] = result.turn_id

            if result.should_retry:
                state["network_retry"] = {
                    "retries": result.network_retries,
                    "last_error": result.error,
                }
                return TicketResult(
                    status="continue",
                    state=state,
                    reason=(
                        f"Network error detected (attempt {result.network_retries}/{self._config.max_network_retries}): {result.error}\n"
                        "Retrying automatically..."
                    ),
                    current_ticket=current_ticket_path,
                    agent_output=result.text,
                    agent_id=result.agent_id,
                    agent_conversation_id=result.conversation_id,
                    agent_turn_id=result.turn_id,
                )

            state.pop("network_retry", None)
            commit_failure_result = runner_commit.handle_failed_commit_turn(
                state=state,
                workspace_root=self._workspace_root,
                commit_pending=commit_pending,
                commit_retries=commit_retries,
                head_before_turn=head_before_turn,
                max_commit_retries=self._config.max_commit_retries,
                current_ticket_path=current_ticket_path,
                result_error=result.error,
                result_text=result.text,
                result_agent_id=result.agent_id,
                result_conversation_id=result.conversation_id,
                result_turn_id=result.turn_id,
            )
            if commit_failure_result is not None:
                return commit_failure_result
            return self._pause(
                state,
                reason="Agent turn failed. Fix the issue and resume.",
                reason_details=f"Error: {result.error}",
                current_ticket=current_ticket_path,
                reason_code="infra_error",
            )

        # Mark replies as consumed only after a successful agent turn.
        if reply_max_seq > reply_seq:
            state["reply_seq"] = reply_max_seq
        state["last_agent_output"] = result.text
        state.pop("network_retry", None)
        state["last_agent_id"] = result.agent_id
        state["last_agent_conversation_id"] = result.conversation_id
        state["last_agent_turn_id"] = result.turn_id

        git_state_after = capture_git_state_after(
            workspace_root=self._workspace_root,
            head_before_turn=head_before_turn,
        )
        repo_fingerprint_after_turn = git_state_after["repo_fingerprint_after"]
        head_after_agent = git_state_after["head_after_turn"]
        clean_after_agent = git_state_after["clean_after_turn"]
        status_after_agent = git_state_after["status_after_turn"]
        agent_committed_this_turn = git_state_after["agent_committed_this_turn"]

        # Post-turn: archive dispatch and create turn summary via centralized helper.
        dispatch_seq = int(state.get("dispatch_seq") or 0)
        archival_result = runner_post_turn.archive_dispatch_and_create_summary(
            workspace_root=self._workspace_root,
            outbox_paths=outbox_paths,
            current_ticket_id=current_ticket_id,
            current_ticket_path=current_ticket_path,
            current_ticket_path_obj=current_path,
            repo_id=self._repo_id,
            run_id=self._run_id,
            dispatch_seq=dispatch_seq,
            agent_output=result.text or "",
            agent_id=result.agent_id or "",
            turn_number=total_turns,
            head_before_turn=head_before_turn,
            emit_event=emit_event,
        )
        if archival_result.dispatch_errors:
            state["outbox_lint"] = archival_result.dispatch_errors
            return self._pause(
                state,
                reason="Invalid DISPATCH.md frontmatter.",
                reason_details="Errors:\n- "
                + "\n- ".join(archival_result.dispatch_errors),
                current_ticket=current_ticket_path,
                reason_code="needs_user_fix",
            )
        dispatch = archival_result.dispatch
        if dispatch is not None:
            state["dispatch_seq"] = dispatch.seq
            state.pop("outbox_lint", None)
        if archival_result.turn_summary is not None:
            state["dispatch_seq"] = archival_result.turn_summary.seq

        # Loop guard: if the same ticket runs with no repository state change for
        # LOOP_NO_CHANGE_THRESHOLD consecutive successful turns, pause and ask for
        # user intervention instead of spinning.
        lint_retry_mode = bool(lint_errors)
        loop_guard_result = compute_loop_guard(
            state=state,
            current_ticket_id=current_ticket_id,
            repo_fingerprint_before=repo_fingerprint_before_turn,
            repo_fingerprint_after=repo_fingerprint_after_turn,
            lint_retry_mode=lint_retry_mode,
        )
        loop_guard_updates = loop_guard_result.get("loop_guard_updates", {})
        if "loop_guard" in loop_guard_result:
            state["loop_guard"] = loop_guard_result["loop_guard"]

        if should_pause_for_loop(loop_guard_updates=loop_guard_updates):
            no_change_count = loop_guard_updates.get("no_change_count", 0)
            reason = "Ticket appears stuck: same ticket ran twice with no repository diff changes."
            details = (
                "Runner paused to avoid repeated no-op work.\n\n"
                f"Ticket: {current_ticket_path}\n"
                f"Consecutive no-change turns: {no_change_count}\n\n"
                "Please provide unblock guidance via reply, or change repository state, then resume. "
                "Use force resume only if you intentionally want to retry unchanged."
            )
            dispatch_record = self._create_runner_pause_dispatch(
                outbox_paths=outbox_paths,
                state=state,
                title="Ticket loop detected (no repo diff change)",
                body=details,
                ticket_id=current_ticket_id,
                ticket_path=current_ticket_path,
            )
            paused = runner_post_turn.build_pause_result(
                state=state,
                reason=reason,
                reason_code="loop_no_diff",
                reason_details=details,
                current_ticket=current_ticket_path,
                workspace_root=self._workspace_root,
            )
            return TicketResult(
                status="paused",
                state=paused["state"],
                reason=paused["reason"],
                reason_details=paused["reason_details"],
                dispatch=dispatch_record,
                current_ticket=paused["current_ticket"],
                agent_output=result.text,
                agent_id=result.agent_id,
                agent_conversation_id=result.conversation_id,
                agent_turn_id=result.turn_id,
            )

        # Post-turn: ticket frontmatter must remain valid.
        fm_result = runner_post_turn.handle_frontmatter_recheck(
            ticket_path=current_path,
            lint_errors=lint_errors,
            lint_retries=lint_retries,
            max_lint_retries=self._config.max_lint_retries,
            agent_conversation_id=result.conversation_id,
        )
        if fm_result.should_pause:
            return self._pause(
                state,
                reason=fm_result.pause_reason or "Ticket frontmatter invalid.",
                reason_details=fm_result.pause_reason_details,
                current_ticket=current_ticket_path,
                reason_code=fm_result.pause_reason_code,
            )
        if fm_result.should_retry:
            state["lint"] = fm_result.lint_state
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

        updated_fm = fm_result.updated_frontmatter
        if state.get("lint"):
            state.pop("lint", None)

        # Optional: auto-commit checkpoint (best-effort).
        checkpoint_error = None
        commit_required_now = bool(
            updated_fm and updated_fm.done and clean_after_agent is False
        )
        if self._config.auto_commit and not commit_pending and not commit_required_now:
            checkpoint_error = self._checkpoint_git(
                turn=total_turns, agent=result.agent_id or "unknown"
            )

        # If we dispatched a pause message, pause regardless of ticket completion.
        if dispatch is not None and dispatch.dispatch.mode == "pause":
            reason = dispatch.dispatch.title or "Paused for user input."
            if checkpoint_error:
                reason += f"\n\nNote: checkpoint commit failed: {checkpoint_error}"
            state["status"] = "paused"
            state["reason"] = reason
            state["reason_code"] = "user_pause"
            return TicketResult(
                status="paused",
                state=state,
                reason=reason,
                dispatch=dispatch,
                current_ticket=safe_relpath(current_path, self._workspace_root),
                agent_output=result.text,
                agent_id=result.agent_id,
                agent_conversation_id=result.conversation_id,
                agent_turn_id=result.turn_id,
            )

        # If ticket is marked done, require a clean working tree (i.e., changes
        # committed) before advancing. This is bounded by max_commit_retries.
        if updated_fm and updated_fm.done:
            if clean_after_agent is False:
                (
                    commit_state_update,
                    commit_status,
                    commit_reason,
                    commit_reason_code,
                    commit_reason_details,
                ) = runner_commit.process_commit_required(
                    clean_after_agent=clean_after_agent,
                    commit_pending=commit_pending,
                    commit_retries=commit_retries,
                    head_before_turn=head_before_turn,
                    head_after_agent=head_after_agent,
                    agent_committed_this_turn=agent_committed_this_turn,
                    status_after_agent=status_after_agent,
                    max_commit_retries=self._config.max_commit_retries,
                )
                if commit_state_update:
                    state["commit"] = commit_state_update
                if commit_reason is not None:
                    return self._pause(
                        state,
                        reason=commit_reason,
                        reason_details=commit_reason_details,
                        current_ticket=current_ticket_path,
                        reason_code=commit_reason_code,
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

            # Clean (or unknown) → commit satisfied (or no changes / cannot check).
            state.pop("commit", None)
            state.pop("current_ticket", None)
            state.pop("current_ticket_id", None)
            state.pop("ticket_turns", None)
            state.pop("last_agent_output", None)
            state.pop("lint", None)
        else:
            # If the ticket is no longer done, clear any pending commit gating.
            state.pop("commit", None)

        if checkpoint_error:
            # Non-fatal, but surface in state for UI.
            state["last_checkpoint_error"] = checkpoint_error
        else:
            state.pop("last_checkpoint_error", None)

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

    def _checkpoint_git(self, *, turn: int, agent: str) -> Optional[str]:
        """Create a best-effort git commit checkpoint.

        Returns an error string if the checkpoint failed, else None.
        """
        return runner_post_turn.checkpoint_git(
            workspace_root=self._workspace_root,
            run_id=self._run_id,
            turn=turn,
            agent=agent,
            checkpoint_message_template=self._config.checkpoint_message_template,
        )

    def _pause(
        self,
        state: dict[str, Any],
        *,
        reason: str,
        reason_code: str = "needs_user_fix",
        reason_details: Optional[str] = None,
        current_ticket: Optional[str] = None,
    ) -> TicketResult:
        paused = runner_post_turn.build_pause_result(
            state=state,
            reason=reason,
            reason_code=reason_code,
            reason_details=reason_details,
            current_ticket=current_ticket,
            workspace_root=self._workspace_root,
        )
        return TicketResult(
            status="paused",
            state=paused["state"],
            reason=paused["reason"],
            reason_details=paused["reason_details"],
            current_ticket=paused["current_ticket"],
        )

    def _create_runner_pause_dispatch(
        self,
        *,
        outbox_paths,
        state: dict[str, Any],
        title: str,
        body: str,
        ticket_id: str,
        ticket_path: Optional[str] = None,
    ):
        """Create and archive a runner-generated pause dispatch."""
        return runner_post_turn.create_runner_pause_dispatch(
            outbox_paths=outbox_paths,
            state=state,
            ticket_id=ticket_id,
            ticket_path=ticket_path,
            repo_id=self._repo_id,
            run_id=self._run_id,
            title=title,
            body=body,
        )

    def _build_prompt(
        self,
        *,
        ticket_path: Path,
        ticket_doc,
        last_agent_output: Optional[str],
        last_checkpoint_error: Optional[str] = None,
        commit_required: bool = False,
        commit_attempt: int = 0,
        commit_max_attempts: int = 2,
        outbox_paths,
        lint_errors: Optional[list[str]],
        reply_context: Optional[str] = None,
        requested_context: Optional[str] = None,
        previous_ticket_content: Optional[str] = None,
        prior_no_change_turns: int = 0,
    ) -> str:
        return build_prompt(
            ticket_path=ticket_path,
            workspace_root=self._workspace_root,
            ticket_doc=ticket_doc,
            last_agent_output=last_agent_output,
            last_checkpoint_error=last_checkpoint_error,
            commit_required=commit_required,
            commit_attempt=commit_attempt,
            commit_max_attempts=commit_max_attempts,
            outbox_paths=outbox_paths,
            lint_errors=lint_errors,
            reply_context=reply_context,
            requested_context=requested_context,
            previous_ticket_content=previous_ticket_content,
            prior_no_change_turns=prior_no_change_turns,
            prompt_max_bytes=self._config.prompt_max_bytes,
        )
