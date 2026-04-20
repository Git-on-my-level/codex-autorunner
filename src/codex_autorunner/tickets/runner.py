from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from ..agents.hermes_identity import canonicalize_hermes_identity
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
    capture_git_state_after,
    is_network_error,
)
from .runner_prompt import (
    CAR_HUD_MAX_CHARS,  # noqa: F401  # re-exported for backwards compatibility
    CAR_HUD_MAX_LINES,  # noqa: F401  # re-exported for backwards compatibility
    _preserve_ticket_structure,  # noqa: F401  # re-exported for backwards compatibility
    _shrink_prompt,  # noqa: F401  # re-exported for backwards compatibility
)
from .runner_prompt_support import (
    TRUNCATION_MARKER,  # noqa: F401  # re-exported for backwards compatibility
)
from .runner_selection import (  # noqa: F401  # re-exported for backwards compatibility
    TICKET_CONTEXT_TOTAL_MAX_BYTES,
)
from .runner_step_support import (
    capture_pre_turn_git_state,
    execute_turn_with_thread_binding_retry,
    increment_turn_counters,
    record_successful_turn_state,
    record_turn_runtime_state,
)
from .runner_thread_bindings import (
    clear_previous_ticket_binding,
    clear_ticket_thread_binding,
    normalize_profile,
    validate_lint_retry_conversation_id,
)

_is_network_error = is_network_error


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

        _commit_raw = state.get("commit")
        commit_state: dict[str, Any] = (
            _commit_raw if isinstance(_commit_raw, dict) else {}
        )
        commit_pending = bool(commit_state.get("pending"))
        commit_retries = int(commit_state.get("retries") or 0)
        previous_ticket_id = (
            state.get("current_ticket_id")
            if isinstance(state.get("current_ticket_id"), str)
            else None
        )
        # Global counters.
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
            if previous_ticket_id:
                clear_ticket_thread_binding(
                    state,
                    ticket_id=previous_ticket_id,
                    reason="ticket_completed_before_selection",
                )
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
        _commit_raw = state.get("commit")
        commit_state = _commit_raw if isinstance(_commit_raw, dict) else {}
        commit_pending = bool(commit_state.get("pending"))
        commit_retries = int(commit_state.get("retries") or 0)

        # Determine lint-retry mode early. When lint state is present, we allow the
        # agent to fix the ticket frontmatter even if the ticket is currently
        # unparsable by the strict lint rules.
        if state.get("status") == "paused":
            # Clear stale pause markers so upgraded logic can proceed without manual DB edits.
            state["status"] = "running"
            state.pop("reason", None)
            state.pop("reason_details", None)
            state.pop("reason_code", None)
            state.pop("pause_context", None)
        _lint_raw = state.get("lint")
        lint_state: dict[str, Any] = _lint_raw if isinstance(_lint_raw, dict) else {}
        pre_turn_plan = runner_selection.plan_pre_turn(
            selection_result=selection_result,
            workspace_root=self._workspace_root,
            ticket_dir=ticket_dir,
            config=self._config,
            state=state,
            run_id=self._run_id,
            outbox_paths=outbox_paths,
            reply_paths=reply_paths,
        )
        for key, value in pre_turn_plan.state_updates.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value

        current_ticket_path = pre_turn_plan.current_ticket_path or safe_relpath(
            current_path, self._workspace_root
        )
        current_ticket_id = (
            pre_turn_plan.current_ticket_id
            if isinstance(pre_turn_plan.current_ticket_id, str)
            else (
                state.get("current_ticket_id")
                if isinstance(state.get("current_ticket_id"), str)
                else None
            )
        )
        if current_ticket_id:
            clear_previous_ticket_binding(
                state,
                previous_ticket_id=previous_ticket_id,
                current_ticket_id=current_ticket_id,
            )

        if pre_turn_plan.status == "paused":
            return self._pause(
                state,
                reason=pre_turn_plan.pause_reason or "Ticket validation failed.",
                reason_details=pre_turn_plan.pause_reason_details,
                current_ticket=current_ticket_path,
                reason_code=pre_turn_plan.pause_reason_code or "needs_user_fix",
            )
        if pre_turn_plan.status == "failed":
            return TicketResult(
                status="failed",
                state=state,
                reason=pre_turn_plan.pause_reason or "Ticket pre-turn planning failed.",
                reason_details=pre_turn_plan.pause_reason_details,
                current_ticket=current_ticket_path,
            )
        if pre_turn_plan.status == "skip":
            return TicketResult(status="continue", state=state)
        if pre_turn_plan.status != "ready" or current_ticket_id is None:
            return self._pause(
                state,
                reason="Ticket pre-turn planning failed unexpectedly.",
                current_ticket=current_ticket_path,
                reason_code="infra_error",
            )

        ticket_doc = pre_turn_plan.ticket_doc
        if ticket_doc is None or pre_turn_plan.prompt is None:
            return self._pause(
                state,
                reason="Ticket pre-turn plan was incomplete.",
                current_ticket=current_ticket_path,
                reason_code="infra_error",
            )
        raw_profile = normalize_profile(ticket_doc.frontmatter.profile)
        canonical = canonicalize_hermes_identity(
            ticket_doc.frontmatter.agent,
            raw_profile,
            context=self._workspace_root,
        )
        current_ticket_profile = canonical.profile
        canonical_agent_id = canonical.agent
        lint_retry_conversation_id = validate_lint_retry_conversation_id(
            lint_state=lint_state,
            conversation_id=pre_turn_plan.conversation_id,
            current_ticket_path=current_ticket_path,
            current_ticket_id=current_ticket_id,
            canonical_agent_id=canonical_agent_id,
            current_ticket_profile=current_ticket_profile,
        )

        ticket_turns = int(state.get("ticket_turns") or 0)
        reply_seq = pre_turn_plan.reply_seq
        reply_max_seq = pre_turn_plan.reply_max_seq
        lint_errors = list(pre_turn_plan.lint_errors or [])
        lint_retries = pre_turn_plan.lint_retries
        commit_pending = pre_turn_plan.commit_pending
        commit_retries = pre_turn_plan.commit_retries
        prompt = pre_turn_plan.prompt
        turn_options = dict(pre_turn_plan.turn_options or {})
        turn_options["ticket_flow_run_id"] = self._run_id
        turn_options["ticket_id"] = current_ticket_id
        turn_options["ticket_path"] = current_ticket_path
        if current_ticket_profile and "profile" not in turn_options:
            turn_options["profile"] = current_ticket_profile
        total_turns, ticket_turns = increment_turn_counters(
            state=state,
            ticket_turns=ticket_turns,
        )
        repo_fingerprint_before_turn, head_before_turn = capture_pre_turn_git_state(
            workspace_root=self._workspace_root
        )
        result, binding_decision = await execute_turn_with_thread_binding_retry(
            agent_pool=self._agent_pool,
            workspace_root=self._workspace_root,
            state=state,
            ticket_id=current_ticket_id,
            ticket_path=current_ticket_path,
            agent_id=canonical_agent_id,
            profile=current_ticket_profile,
            prompt=prompt,
            lint_retry_conversation_id=lint_retry_conversation_id,
            turn_options=turn_options if turn_options else None,
            emit_event=emit_event,
            max_network_retries=self._config.max_network_retries,
            current_network_retries=network_retries,
        )
        if not result.success:
            record_turn_runtime_state(
                state=state,
                result=result,
                ticket_id=current_ticket_id,
                ticket_path=current_ticket_path,
                agent_id=canonical_agent_id,
                profile=current_ticket_profile,
                binding_decision=binding_decision,
            )
            return self._handle_failed_turn(
                state,
                result=result,
                current_ticket_path=current_ticket_path,
                commit_pending=commit_pending,
                commit_retries=commit_retries,
                head_before_turn=head_before_turn,
            )

        record_successful_turn_state(
            state=state,
            reply_seq=reply_seq,
            reply_max_seq=reply_max_seq,
            result=result,
            ticket_id=current_ticket_id,
            ticket_path=current_ticket_path,
            agent_id=canonical_agent_id,
            profile=current_ticket_profile,
            binding_decision=binding_decision,
        )

        git_state_after = capture_git_state_after(
            workspace_root=self._workspace_root,
            head_before_turn=head_before_turn,
        )

        return runner_post_turn.reconcile_post_turn(
            state=state,
            workspace_root=self._workspace_root,
            run_id=self._run_id,
            repo_id=self._repo_id,
            outbox_paths=outbox_paths,
            current_ticket_id=current_ticket_id,
            current_ticket_path=current_ticket_path,
            current_ticket_path_obj=current_path,
            canonical_agent_id=canonical_agent_id,
            current_ticket_profile=current_ticket_profile,
            result=result,
            total_turns=total_turns,
            head_before_turn=head_before_turn,
            repo_fingerprint_before_turn=repo_fingerprint_before_turn,
            git_state_after=git_state_after,
            lint_errors=lint_errors,
            lint_retries=lint_retries,
            commit_pending=commit_pending,
            commit_retries=commit_retries,
            max_lint_retries=self._config.max_lint_retries,
            max_commit_retries=self._config.max_commit_retries,
            auto_commit=self._config.auto_commit,
            checkpoint_message_template=self._config.checkpoint_message_template,
            emit_event=emit_event,
        )

    def _handle_failed_turn(
        self,
        state: dict[str, Any],
        *,
        result,
        current_ticket_path: str,
        commit_pending: bool,
        commit_retries: int,
        head_before_turn: Optional[str],
    ) -> TicketResult:
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

    def _pause(
        self,
        state: dict[str, Any],
        *,
        reason: str,
        reason_code: str = "needs_user_fix",
        reason_details: Optional[str] = None,
        current_ticket: Optional[str] = None,
    ) -> TicketResult:
        state = dict(state)
        state["status"] = "paused"
        state["reason"] = reason
        state["reason_code"] = reason_code
        pause_context: dict[str, Any] = {
            "paused_reply_seq": int(state.get("reply_seq") or 0),
        }
        fingerprint = self._repo_fingerprint()
        if isinstance(fingerprint, str):
            pause_context["repo_fingerprint"] = fingerprint
        state["pause_context"] = pause_context
        if reason_details:
            state["reason_details"] = reason_details
        else:
            state.pop("reason_details", None)
        return TicketResult(
            status="paused",
            state=state,
            reason=reason,
            reason_details=reason_details,
            current_ticket=current_ticket
            or (
                state.get("current_ticket")
                if isinstance(state.get("current_ticket"), str)
                else None
            ),
        )

    def _repo_fingerprint(self) -> Optional[str]:
        """Return a stable snapshot of HEAD + porcelain status."""
        return runner_post_turn.get_repo_fingerprint(self._workspace_root)
