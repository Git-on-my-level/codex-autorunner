from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from ..agents.hermes_identity import canonicalize_hermes_identity
from ..contextspace.paths import contextspace_doc_path
from ..core.file_chat_keys import ticket_instance_token
from ..core.flows.models import FlowEventType
from ..core.git_utils import git_diff_stats
from . import runner_commit, runner_post_turn, runner_prompt, runner_selection
from .agent_pool import AgentPool
from .files import list_ticket_paths, safe_relpath
from .models import TicketContextEntry, TicketResult, TicketRunConfig
from .outbox import (
    archive_dispatch,
    create_turn_summary,
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
    compute_loop_guard,
    is_network_error,
    should_pause_for_loop,
)
from .runner_prompt import (
    TRUNCATION_MARKER,  # noqa: F401  # re-exported for backwards compatibility
    _build_car_hud,
    _preserve_ticket_structure,  # noqa: F401  # re-exported for backwards compatibility
    _shrink_prompt,
    _truncate_text_by_bytes,
)
from .runner_step_support import (
    build_reply_context,
    build_turn_options,
    capture_pre_turn_git_state,
    execute_turn_with_thread_binding_retry,
    increment_turn_counters,
    load_previous_ticket_content,
    record_successful_turn_state,
)
from .runner_thread_bindings import (
    clear_ticket_thread_binding,
    normalize_profile,
)

_is_network_error = is_network_error

_logger = logging.getLogger(__name__)

WORKSPACE_DOC_MAX_CHARS = 4000
CAR_HUD_MAX_LINES = 14
CAR_HUD_MAX_CHARS = 900
TICKET_CONTEXT_DEFAULT_MAX_BYTES = 4096
TICKET_CONTEXT_TOTAL_MAX_BYTES = 16384


def _load_ticket_context_block(
    *,
    workspace_root: Path,
    entries: tuple[TicketContextEntry, ...],
) -> tuple[str, list[str]]:
    """Resolve requested ticket context entries into a bounded prompt block."""

    if not entries:
        return "", []

    missing_required: list[str] = []
    blocks: list[str] = []
    remaining_total = TICKET_CONTEXT_TOTAL_MAX_BYTES

    for entry in entries:
        rel_path = entry.path
        absolute = workspace_root / rel_path
        block_prefix = f"- path: {rel_path}\n- required: {str(entry.required).lower()}"
        cap = min(
            (
                entry.max_bytes
                if entry.max_bytes is not None
                else TICKET_CONTEXT_DEFAULT_MAX_BYTES
            ),
            TICKET_CONTEXT_TOTAL_MAX_BYTES,
        )
        cap = min(cap, max(remaining_total, 0))

        if not absolute.exists():
            if entry.required:
                missing_required.append(rel_path)
            blocks.append(
                "<CAR_CONTEXT_ENTRY>\n"
                f"{block_prefix}\n"
                "- status: missing\n"
                "</CAR_CONTEXT_ENTRY>"
            )
            continue

        if not absolute.is_file():
            if entry.required:
                missing_required.append(rel_path)
            blocks.append(
                "<CAR_CONTEXT_ENTRY>\n"
                f"{block_prefix}\n"
                "- status: not_a_file\n"
                "</CAR_CONTEXT_ENTRY>"
            )
            continue

        try:
            raw = absolute.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            if entry.required:
                missing_required.append(rel_path)
            blocks.append(
                "<CAR_CONTEXT_ENTRY>\n"
                f"{block_prefix}\n"
                f"- status: read_error ({exc})\n"
                "</CAR_CONTEXT_ENTRY>"
            )
            continue

        content = (raw or "").strip()
        if cap <= 0:
            blocks.append(
                "<CAR_CONTEXT_ENTRY>\n"
                f"{block_prefix}\n"
                "- status: skipped_budget_exhausted\n"
                "</CAR_CONTEXT_ENTRY>"
            )
            continue
        truncated = _truncate_text_by_bytes(content, cap)
        payload = (
            "<CAR_CONTEXT_ENTRY>\n"
            f"{block_prefix}\n"
            f"- max_bytes: {cap}\n"
            "- status: included\n"
            "CONTENT:\n"
            f"{truncated}\n"
            "</CAR_CONTEXT_ENTRY>"
        )
        payload_bytes = len(payload.encode("utf-8"))
        if payload_bytes > remaining_total:
            # Final strict clamp for total boundedness.
            trimmed = _truncate_text_by_bytes(payload, max(remaining_total, 0))
            blocks.append(trimmed)
            remaining_total = 0
            break
        blocks.append(payload)
        remaining_total -= payload_bytes

    header = (
        "Requested ticket context includes "
        f"(bounded total bytes={TICKET_CONTEXT_TOTAL_MAX_BYTES}):"
    )
    rendered = "\n\n".join([header] + blocks)
    rendered = _truncate_text_by_bytes(rendered, TICKET_CONTEXT_TOTAL_MAX_BYTES)
    return rendered, missing_required


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
        _lint_errors_raw = lint_state.get("errors")
        lint_errors: list[str] = (
            _lint_errors_raw if isinstance(_lint_errors_raw, list) else []
        )
        lint_retries = int(lint_state.get("retries") or 0)
        _conv_id_raw = lint_state.get("conversation_id")
        lint_retry_conversation_id: Optional[str] = (
            _conv_id_raw if isinstance(_conv_id_raw, str) else None
        )

        validation_result = runner_selection.validate_ticket_for_execution(
            ticket_path=current_path,
            workspace_root=self._workspace_root,
            state=state,
            lint_errors=lint_errors if lint_errors else None,
        )
        current_ticket_path = safe_relpath(current_path, self._workspace_root)
        if validation_result.status == "paused":
            reason_details = (
                "Errors:\n- " + "\n- ".join(validation_result.errors)
                if validation_result.errors
                else None
            )
            return self._pause(
                state,
                reason=validation_result.pause_reason or "Ticket validation failed.",
                reason_details=reason_details,
                current_ticket=current_ticket_path,
                reason_code=validation_result.pause_reason_code or "needs_user_fix",
            )
        if not validation_result.validated:
            return self._pause(
                state,
                reason="Ticket validation failed unexpectedly.",
                current_ticket=current_ticket_path,
                reason_code="infra_error",
            )

        ticket_doc = validation_result.validated.ticket_doc
        current_ticket_id = ticket_doc.frontmatter.ticket_id
        state["current_ticket_id"] = current_ticket_id
        raw_profile = normalize_profile(ticket_doc.frontmatter.profile)
        canonical = canonicalize_hermes_identity(
            ticket_doc.frontmatter.agent,
            raw_profile,
            context=self._workspace_root,
        )
        current_ticket_profile = canonical.profile
        canonical_agent_id = canonical.agent
        lint_retry_ticket_id = lint_state.get("ticket_id")
        lint_retry_ticket_path = lint_state.get("ticket_path")
        lint_retry_agent_id = normalize_profile(lint_state.get("agent_id"))
        lint_retry_profile = normalize_profile(lint_state.get("profile"))
        if lint_retry_conversation_id is not None:
            if (
                (
                    isinstance(lint_retry_ticket_path, str)
                    and lint_retry_ticket_path != current_ticket_path
                )
                or (
                    isinstance(lint_retry_ticket_id, str)
                    and current_ticket_id != "lint-retry-ticket"
                    and lint_retry_ticket_id != current_ticket_id
                )
                or (
                    lint_retry_agent_id is not None
                    and lint_retry_agent_id != canonical_agent_id
                )
                or (
                    "profile" in lint_state
                    and lint_retry_profile != current_ticket_profile
                )
            ):
                lint_retry_conversation_id = None
        if previous_ticket_id and previous_ticket_id != current_ticket_id:
            clear_ticket_thread_binding(
                state,
                ticket_id=previous_ticket_id,
                reason="ticket_changed",
            )
        if validation_result.validated.skip_execution:
            return TicketResult(status="continue", state=state)

        ticket_turns = int(state.get("ticket_turns") or 0)
        reply_seq = int(state.get("reply_seq") or 0)
        reply_context, reply_max_seq = build_reply_context(
            reply_paths=reply_paths,
            last_seq=reply_seq,
            workspace_root=self._workspace_root,
        )
        ticket_paths = list_ticket_paths(ticket_dir)
        requested_context_block, missing_required_context = _load_ticket_context_block(
            workspace_root=self._workspace_root,
            entries=ticket_doc.frontmatter.context,
        )
        if missing_required_context:
            details = "Missing required ticket context files:\n- " + "\n- ".join(
                missing_required_context
            )
            state["status"] = "failed"
            state["reason_code"] = "missing_required_context"
            state["reason"] = "Required ticket context file missing."
            state["reason_details"] = details
            return TicketResult(
                status="failed",
                state=state,
                reason="Required ticket context file missing.",
                reason_details=details,
                current_ticket=safe_relpath(current_path, self._workspace_root),
            )
        previous_ticket_content = load_previous_ticket_content(
            current_path=current_path,
            ticket_paths=ticket_paths,
            include_previous_ticket_context=self._config.include_previous_ticket_context,
        )
        prompt = runner_prompt.build_prompt(
            ticket_path=current_path,
            workspace_root=self._workspace_root,
            ticket_doc=ticket_doc,
            last_agent_output=(
                state.get("last_agent_output")
                if isinstance(state.get("last_agent_output"), str)
                else None
            ),
            last_checkpoint_error=(
                state.get("last_checkpoint_error")
                if isinstance(state.get("last_checkpoint_error"), str)
                else None
            ),
            commit_required=commit_pending,
            commit_attempt=commit_retries + 1 if commit_pending else 0,
            commit_max_attempts=self._config.max_commit_retries,
            outbox_paths=outbox_paths,
            lint_errors=lint_errors if lint_errors else None,
            reply_context=reply_context,
            requested_context=requested_context_block,
            previous_ticket_content=previous_ticket_content,
            prior_no_change_turns=self._prior_no_change_turns(state, current_ticket_id),
            prompt_max_bytes=self._config.prompt_max_bytes,
        )
        turn_options = build_turn_options(ticket_doc=ticket_doc)
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
        repo_fingerprint_after_turn = git_state_after["repo_fingerprint_after"]
        head_after_agent = git_state_after["head_after_turn"]
        clean_after_agent = git_state_after["clean_after_turn"]
        status_after_agent = git_state_after["status_after_turn"]
        agent_committed_this_turn = git_state_after["agent_committed_this_turn"]

        # Post-turn: archive outbox if DISPATCH.md exists.
        dispatch_seq = int(state.get("dispatch_seq") or 0)
        current_ticket_key = ticket_instance_token(current_path)
        dispatch_ticket_id = current_ticket_path or current_ticket_id
        dispatch, dispatch_errors = archive_dispatch(
            outbox_paths,
            next_seq=dispatch_seq + 1,
            ticket_id=dispatch_ticket_id,
            repo_id=self._repo_id,
            run_id=self._run_id,
            origin="runner",
        )
        if dispatch_errors:
            # Treat as pause: user should fix DISPATCH.md frontmatter. Keep outbox
            # lint separate from ticket frontmatter lint to avoid mixing behaviors.
            state["outbox_lint"] = dispatch_errors
            return self._pause(
                state,
                reason="Invalid DISPATCH.md frontmatter.",
                reason_details="Errors:\n- " + "\n- ".join(dispatch_errors),
                current_ticket=current_ticket_path,
                reason_code="needs_user_fix",
            )

        if dispatch is not None:
            state["dispatch_seq"] = dispatch.seq
            state.pop("outbox_lint", None)

        turn_summary_seq = int(state.get("dispatch_seq") or 0) + 1
        self._record_turn_summary(
            state=state,
            outbox_paths=outbox_paths,
            turn_summary_seq=turn_summary_seq,
            result=result,
            dispatch_ticket_id=dispatch_ticket_id,
            total_turns=total_turns,
            head_before_turn=head_before_turn,
            current_ticket_id=current_ticket_id,
            current_ticket_key=current_ticket_key,
            current_ticket_path=current_ticket_path,
            emit_event=emit_event,
        )

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
            pause_context: dict[str, Any] = {
                "paused_reply_seq": int(state.get("reply_seq") or 0),
            }
            fingerprint = self._repo_fingerprint()
            if isinstance(fingerprint, str):
                pause_context["repo_fingerprint"] = fingerprint
            state["pause_context"] = pause_context
            state["status"] = "paused"
            state["reason"] = reason
            state["reason_code"] = "loop_no_diff"
            state["reason_details"] = details
            return TicketResult(
                status="paused",
                state=state,
                reason=reason,
                reason_details=details,
                dispatch=dispatch_record,
                current_ticket=current_ticket_path,
                agent_output=result.text,
                agent_id=result.agent_id,
                agent_conversation_id=result.conversation_id,
                agent_turn_id=result.turn_id,
            )

        # Post-turn: ticket frontmatter must remain valid.
        updated_fm, fm_errors = self._recheck_ticket_frontmatter(current_path)
        if fm_errors:
            lint_retries += 1
            if lint_retries > self._config.max_lint_retries:
                return self._pause(
                    state,
                    reason="Ticket frontmatter invalid. Manual fix required.",
                    reason_details=(
                        "Exceeded lint retry limit. Fix the ticket frontmatter manually and resume.\n\n"
                        "Errors:\n- " + "\n- ".join(fm_errors)
                    ),
                    current_ticket=current_ticket_path,
                    reason_code="needs_user_fix",
                )

            state["lint"] = {
                "errors": fm_errors,
                "retries": lint_retries,
                "conversation_id": result.conversation_id,
                "ticket_id": current_ticket_id,
                "ticket_path": current_ticket_path,
                "agent_id": canonical_agent_id,
                "profile": current_ticket_profile,
            }
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

        # Clear lint state if previously set.
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
            clear_ticket_thread_binding(
                state,
                ticket_id=current_ticket_id,
                reason="ticket_completed",
            )
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

    def _recheck_ticket_frontmatter(self, ticket_path: Path):
        return runner_post_turn.check_ticket_frontmatter(ticket_path=ticket_path)

    def _record_turn_summary(
        self,
        *,
        state: dict[str, Any],
        outbox_paths: Any,
        turn_summary_seq: int,
        result: Any,
        dispatch_ticket_id: str,
        total_turns: int,
        head_before_turn: Optional[str],
        current_ticket_id: str,
        current_ticket_key: str,
        current_ticket_path: str,
        emit_event: Optional[Callable[..., None]],
    ) -> None:
        turn_diff_stats: Optional[dict[str, Any]] = None
        try:
            if head_before_turn:
                turn_diff_stats = git_diff_stats(
                    self._workspace_root, from_ref=head_before_turn
                )
            else:
                turn_diff_stats = git_diff_stats(
                    self._workspace_root, from_ref=None, include_staged=True
                )
        except (OSError, ValueError, RuntimeError):
            turn_diff_stats = None

        turn_summary, _turn_summary_errors = create_turn_summary(
            outbox_paths,
            next_seq=turn_summary_seq,
            agent_output=result.text or "",
            ticket_id=dispatch_ticket_id,
            agent_id=result.agent_id,
            turn_number=total_turns,
            diff_stats=turn_diff_stats,
        )
        if turn_summary is not None:
            state["dispatch_seq"] = turn_summary.seq
            if emit_event is not None and isinstance(turn_diff_stats, dict):
                try:
                    emit_event(
                        FlowEventType.DIFF_UPDATED,
                        {
                            "ticket_id": current_ticket_id,
                            "ticket_key": current_ticket_key,
                            "ticket_path": current_ticket_path,
                            "dispatch_seq": turn_summary.seq,
                            "insertions": int(turn_diff_stats.get("insertions") or 0),
                            "deletions": int(turn_diff_stats.get("deletions") or 0),
                            "files_changed": int(
                                turn_diff_stats.get("files_changed") or 0
                            ),
                        },
                    )
                except Exception:
                    pass

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
        rel_ticket = safe_relpath(ticket_path, self._workspace_root)
        rel_dispatch_dir = safe_relpath(outbox_paths.dispatch_dir, self._workspace_root)
        rel_dispatch_path = safe_relpath(
            outbox_paths.dispatch_path, self._workspace_root
        )

        checkpoint_block = ""
        if last_checkpoint_error:
            checkpoint_block = (
                "<CAR_CHECKPOINT_WARNING>\n"
                "WARNING: The previous checkpoint git commit failed (often due to pre-commit hooks).\n"
                "Resolve this before proceeding, or future turns may fail to checkpoint.\n\n"
                "Checkpoint error:\n"
                f"{last_checkpoint_error}\n"
                "</CAR_CHECKPOINT_WARNING>"
            )

        commit_block = ""
        if commit_required:
            attempts_remaining = max(commit_max_attempts - commit_attempt + 1, 0)
            commit_block = (
                "<CAR_COMMIT_REQUIRED>\n"
                "ACTION REQUIRED: The repo is dirty but the ticket is marked done.\n"
                "Commit your changes (ensuring any pre-commit hooks pass) so the flow can advance.\n\n"
                f"Attempts remaining before user intervention: {attempts_remaining}\n"
                "</CAR_COMMIT_REQUIRED>"
            )

        if lint_errors:
            lint_block = (
                "<CAR_TICKET_FRONTMATTER_LINT_REPAIR>\n"
                "Ticket frontmatter lint failed. Fix ONLY the ticket YAML frontmatter to satisfy:\n- "
                + "\n- ".join(lint_errors)
                + "\n</CAR_TICKET_FRONTMATTER_LINT_REPAIR>"
            )
        else:
            lint_block = ""

        loop_guard_block = ""
        if prior_no_change_turns > 0:
            loop_guard_block = (
                "<CAR_LOOP_GUARD>\n"
                "Previous turn(s) on this ticket produced no repository diff change.\n"
                f"Consecutive no-change turns so far: {prior_no_change_turns}\n"
                "If you are still blocked, write DISPATCH.md with mode: pause instead of retrying unchanged steps.\n"
                "</CAR_LOOP_GUARD>"
            )

        reply_block = ""
        if reply_context:
            reply_block = reply_context
        requested_context_block = ""
        if requested_context:
            requested_context_block = requested_context

        workspace_block = ""
        workspace_docs: list[tuple[str, str, str]] = []
        for key, label in (
            ("active_context", "Active context"),
            ("decisions", "Decisions"),
            ("spec", "Spec"),
        ):
            path = contextspace_doc_path(self._workspace_root, key)
            try:
                if not path.exists():
                    continue
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                _logger.debug("contextspace doc read failed for %s: %s", path, exc)
                continue
            snippet = (content or "").strip()
            if not snippet:
                continue
            workspace_docs.append(
                (
                    label,
                    safe_relpath(path, self._workspace_root),
                    snippet[:WORKSPACE_DOC_MAX_CHARS],
                )
            )

        if workspace_docs:
            blocks = ["Contextspace docs (truncated; skip if not relevant):"]
            for label, rel, body in workspace_docs:
                blocks.append(f"{label} [{rel}]:\n{body}")
            workspace_block = "\n\n".join(blocks)

        prev_ticket_block = ""
        if previous_ticket_content:
            prev_ticket_block = (
                "PREVIOUS TICKET CONTEXT (truncated to 16KB; for reference only; do not edit):\n"
                "Cross-ticket context should flow through contextspace docs (active_context.md, decisions.md, spec.md) "
                "rather than implicit previous ticket content. This is included only for legacy compatibility.\n"
                + previous_ticket_content
            )

        ticket_raw_content = ticket_path.read_text(encoding="utf-8")
        ticket_block = (
            "<CAR_CURRENT_TICKET_FILE>\n"
            f"PATH: {rel_ticket}\n"
            "<TICKET_MARKDOWN>\n"
            f"{ticket_raw_content}\n"
            "</TICKET_MARKDOWN>\n"
            "</CAR_CURRENT_TICKET_FILE>"
        )

        prev_block = ""
        if last_agent_output:
            prev_block = last_agent_output

        sections = {
            "prev_block": prev_block,
            "prev_ticket_block": prev_ticket_block,
            "workspace_block": workspace_block,
            "reply_block": reply_block,
            "requested_context_block": requested_context_block,
            "ticket_block": ticket_block,
        }
        car_hud = _build_car_hud()

        def render() -> str:
            return (
                "<CAR_TICKET_FLOW_PROMPT>\n\n"
                "<CAR_TICKET_FLOW_INSTRUCTIONS>\n"
                "You are running inside Codex Autorunner (CAR) in a ticket-based workflow.\n\n"
                "Your job in this turn:\n"
                "- Read the current ticket file.\n"
                "- Make the required repo changes.\n"
                "- Update the ticket file to reflect progress.\n"
                "- Set `done: true` in the ticket YAML frontmatter only when the ticket is truly complete.\n\n"
                "CAR orientation (80/20):\n"
                "- `.codex-autorunner/tickets/` is the queue that drives the flow (files named `TICKET-###*.md`, processed in numeric order).\n"
                "- `.codex-autorunner/contextspace/` holds durable context shared across ticket turns (especially `active_context.md` and `spec.md`).\n"
                "- `.codex-autorunner/ABOUT_CAR.md` is the repo-local briefing (what CAR auto-generates + helper scripts) if you need operational details.\n\n"
                "Communicating with the user (optional):\n"
                "- To send a message or request input, write to the dispatch directory:\n"
                "  1) write any attachments to the dispatch directory\n"
                "  2) write `DISPATCH.md` last\n"
                "- `DISPATCH.md` YAML supports `mode: notify|pause`.\n"
                "  - `pause` waits for user input; `notify` continues without waiting.\n"
                "  - Example:\n"
                "    ---\n"
                "    mode: pause\n"
                "    ---\n"
                "    Need clarification on X before proceeding.\n"
                "- You do not need a “final” dispatch when you finish; the runner will archive your turn output automatically. Dispatch only if you want something to stand out or you need user input.\n\n"
                "If blocked:\n"
                "- Dispatch with `mode: pause` rather than guessing.\n\n"
                "Creating follow-up tickets (optional):\n"
                "- New tickets live under `.codex-autorunner/tickets/` and follow the `TICKET-###*.md` naming pattern.\n"
                "- If present, `.codex-autorunner/bin/ticket_tool.py` can create/insert/move tickets; `.codex-autorunner/bin/lint_tickets.py` lints ticket frontmatter (see `.codex-autorunner/ABOUT_CAR.md`).\n"
                "Using ticket templates (optional):\n"
                "- If you need a standard ticket pattern, prefer: `car templates fetch <repo_id>:<path>[@<ref>]`\n"
                "  - Trusted repos skip scanning; untrusted repos are scanned (cached by blob SHA).\n\n"
                "Workspace docs:\n"
                "- You may update or add context under `.codex-autorunner/contextspace/` so future ticket turns have durable context.\n"
                "- Prefer referencing these docs instead of creating duplicate “shadow” docs elsewhere.\n\n"
                "Repo hygiene:\n"
                "- Do not add new `.codex-autorunner/` artifacts to git unless they are already tracked.\n"
                "</CAR_TICKET_FLOW_INSTRUCTIONS>\n\n"
                "<CAR_RUNTIME_PATHS>\n"
                f"Current ticket file: {rel_ticket}\n"
                f"Dispatch directory: {rel_dispatch_dir}\n"
                f"DISPATCH.md path: {rel_dispatch_path}\n"
                "</CAR_RUNTIME_PATHS>\n\n"
                "<CAR_HUD>\n"
                f"{car_hud}\n"
                "</CAR_HUD>\n\n"
                f"{checkpoint_block}\n\n"
                f"{commit_block}\n\n"
                f"{lint_block}\n\n"
                f"{loop_guard_block}\n\n"
                "<CAR_REQUESTED_CONTEXT>\n"
                f"{sections['requested_context_block']}\n"
                "</CAR_REQUESTED_CONTEXT>\n\n"
                "<CAR_WORKSPACE_DOCS>\n"
                f"{sections['workspace_block']}\n"
                "</CAR_WORKSPACE_DOCS>\n\n"
                "<CAR_HUMAN_REPLIES>\n"
                f"{sections['reply_block']}\n"
                "</CAR_HUMAN_REPLIES>\n\n"
                "<CAR_PREVIOUS_TICKET_REFERENCE>\n"
                f"{sections['prev_ticket_block']}\n"
                "</CAR_PREVIOUS_TICKET_REFERENCE>\n\n"
                f"{sections['ticket_block']}\n\n"
                "<CAR_PREVIOUS_AGENT_OUTPUT>\n"
                f"{sections['prev_block']}\n"
                "</CAR_PREVIOUS_AGENT_OUTPUT>\n\n"
                "</CAR_TICKET_FLOW_PROMPT>"
            )

        return _shrink_prompt(
            max_bytes=self._config.prompt_max_bytes,
            render=render,
            sections=sections,
            order=[
                "prev_block",
                "prev_ticket_block",
                "reply_block",
                "requested_context_block",
                "workspace_block",
                "ticket_block",
            ],
        )

    def _prior_no_change_turns(self, state: dict[str, Any], ticket_id: str) -> int:
        loop_guard_raw = state.get("loop_guard")
        loop_guard_state = (
            dict(loop_guard_raw) if isinstance(loop_guard_raw, dict) else {}
        )
        if loop_guard_state.get("ticket") != ticket_id:
            return 0
        return int(loop_guard_state.get("no_change_count") or 0)
