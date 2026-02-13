from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from ..contextspace.paths import contextspace_doc_path
from ..core.flows.models import FlowEventType
from ..core.git_utils import git_diff_stats, run_git
from .agent_pool import AgentPool, AgentTurnRequest
from .files import list_ticket_paths, read_ticket, safe_relpath, ticket_is_done
from .frontmatter import parse_markdown_frontmatter
from .lint import lint_ticket_directory, lint_ticket_frontmatter
from .models import TicketFrontmatter, TicketResult, TicketRunConfig
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
    parse_user_reply,
    resolve_reply_paths,
)

_logger = logging.getLogger(__name__)

WORKSPACE_DOC_MAX_CHARS = 4000
TRUNCATION_MARKER = "\n\n[... TRUNCATED ...]\n\n"
LOOP_NO_CHANGE_THRESHOLD = 2


def _truncate_text_by_bytes(text: str, max_bytes: int) -> str:
    """Truncate text to fit within max_bytes UTF-8 encoded size."""
    if max_bytes <= 0:
        return ""
    normalized = text or ""
    encoded = normalized.encode("utf-8")
    if len(encoded) <= max_bytes:
        return normalized
    marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
    if max_bytes <= marker_bytes:
        return TRUNCATION_MARKER.encode("utf-8")[:max_bytes].decode(
            "utf-8", errors="ignore"
        )
    target_bytes = max_bytes - marker_bytes
    truncated = encoded[:target_bytes].decode("utf-8", errors="ignore")
    return truncated + TRUNCATION_MARKER


def _is_network_error(error_message: str) -> bool:
    """Check if an error message indicates a transient network issue.

    Returns True if the error appears to be network-related and retryable.
    This includes connection errors, timeouts, and transport failures.
    """
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


def _preserve_ticket_structure(ticket_block: str, max_bytes: int) -> str:
    """Truncate ticket block while preserving prefix and ticket frontmatter.

    ticket_block format:
        "\\n\\n<CAR_CURRENT_TICKET_FILE>\\nPATH: ...\\n<TICKET_MARKDOWN>\\n"
        "{ticket_raw_content}\\n</TICKET_MARKDOWN>\\n</CAR_CURRENT_TICKET_FILE>\\n"
    where ticket_raw_content itself contains markdown frontmatter.
    """
    if len(ticket_block.encode("utf-8")) <= max_bytes:
        return ticket_block

    # ticket_block structure:
    #   "<CAR_CURRENT_TICKET_FILE>\n"
    #   "PATH: {rel_ticket}\n"
    #   "<TICKET_MARKDOWN>\n"
    #   "---\n" - ticket frontmatter start
    #   "agent: ...\n"
    #   "done: ...\n"
    #   "title: ...\n"
    #   "goal: ...\n"
    #   "---\n" - ticket frontmatter end (what we want to preserve)
    #   ticket body...
    #   "</TICKET_MARKDOWN>\n"
    #   "</CAR_CURRENT_TICKET_FILE>\n"

    # Find the frontmatter markers after <TICKET_MARKDOWN>.
    marker = "\n---\n"
    ticket_md_idx = ticket_block.find("<TICKET_MARKDOWN>")
    if ticket_md_idx == -1:
        return _truncate_text_by_bytes(ticket_block, max_bytes)

    first_marker_idx = ticket_block.find(marker, ticket_md_idx)
    if first_marker_idx == -1:
        return _truncate_text_by_bytes(ticket_block, max_bytes)

    second_marker_idx = ticket_block.find(marker, first_marker_idx + 1)
    if second_marker_idx == -1:
        return _truncate_text_by_bytes(ticket_block, max_bytes)

    # Preserve everything up to and including the second marker
    preserve_end = second_marker_idx + len(marker)
    preserved_part = ticket_block[:preserve_end]

    # Check if we still have room (account for truncation marker that will be added)
    preserved_bytes = len(preserved_part.encode("utf-8"))
    marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
    remaining_bytes = max(max_bytes - preserved_bytes, 0)

    if remaining_bytes > 0:
        body = ticket_block[preserve_end:]
        # Account for marker in the body budget
        body_budget = max(remaining_bytes - marker_bytes, 0)
        truncated_body = _truncate_text_by_bytes(body, body_budget)
        return preserved_part + truncated_body

    # Not enough room even for preserved part, fall back to simple truncation
    return _truncate_text_by_bytes(ticket_block, max_bytes)


def _shrink_prompt(
    *,
    max_bytes: int,
    render: Callable[[], str],
    sections: dict[str, str],
    order: list[str],
) -> str:
    """Shrink prompt by truncating sections in order of priority."""
    prompt = render()
    if len(prompt.encode("utf-8")) <= max_bytes:
        return prompt

    for key in order:
        if len(prompt.encode("utf-8")) <= max_bytes:
            break
        value = sections.get(key, "")
        if not value:
            continue
        overflow = len(prompt.encode("utf-8")) - max_bytes
        value_bytes = len(value.encode("utf-8"))
        new_limit = max(value_bytes - overflow, 0)

        if key == "ticket_block":
            sections[key] = _preserve_ticket_structure(value, new_limit)
        else:
            sections[key] = _truncate_text_by_bytes(value, new_limit)
        prompt = render()

    if len(prompt.encode("utf-8")) > max_bytes:
        prompt = _truncate_text_by_bytes(prompt, max_bytes)

    return prompt


class TicketRunner:
    """Execute a ticket directory one agent turn at a time.

    This runner is intentionally small and file-backed:
    - Tickets are markdown files under `config.ticket_dir`.
    - User messages + optional attachments are written to an outbox under `config.runs_dir`.
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
        runs_dir = self._config.runs_dir

        # Ensure outbox dirs exist.
        outbox_paths = resolve_outbox_paths(
            workspace_root=self._workspace_root,
            runs_dir=runs_dir,
            run_id=self._run_id,
        )
        ensure_outbox_dirs(outbox_paths)

        # Ensure reply inbox dirs exist (human -> agent messages).
        reply_paths = resolve_reply_paths(
            workspace_root=self._workspace_root,
            runs_dir=runs_dir,
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

        ticket_paths = list_ticket_paths(ticket_dir)
        if not ticket_paths:
            return self._pause(
                state,
                reason=(
                    "No tickets found. Create tickets under "
                    f"{safe_relpath(ticket_dir, self._workspace_root)} and resume."
                ),
                reason_code="no_tickets",
            )

        # Check for duplicate ticket indices before proceeding.
        dir_lint_errors = lint_ticket_directory(ticket_dir)
        if dir_lint_errors:
            return self._pause(
                state,
                reason="Duplicate ticket indices detected.",
                reason_details="Errors:\n- " + "\n- ".join(dir_lint_errors),
                reason_code="needs_user_fix",
            )

        current_ticket = state.get("current_ticket")
        current_path: Optional[Path] = (
            (self._workspace_root / current_ticket)
            if isinstance(current_ticket, str) and current_ticket
            else None
        )

        # The agent may rename/delete the current ticket file. If persisted state
        # points at a path that no longer exists, clear stale per-ticket fields and
        # reselect from current on-disk tickets.
        if current_path is not None and not current_path.exists():
            _logger.warning(
                "Current ticket file no longer exists at %s; clearing stale current_ticket state.",
                safe_relpath(current_path, self._workspace_root),
            )
            current_path = None
            state.pop("current_ticket", None)
            state.pop("ticket_turns", None)
            state.pop("last_agent_output", None)
            state.pop("lint", None)
            state.pop("commit", None)
            commit_pending = False
            commit_retries = 0

        # If current ticket is done, clear it unless we're in the middle of a
        # bounded "commit required" follow-up loop.
        if current_path and ticket_is_done(current_path) and not commit_pending:
            current_path = None
            state.pop("current_ticket", None)
            state.pop("ticket_turns", None)
            state.pop("last_agent_output", None)
            state.pop("lint", None)
            state.pop("commit", None)

        if current_path is None:
            next_path = self._find_next_ticket(ticket_paths)
            if next_path is None:
                state["status"] = "completed"
                return TicketResult(
                    status="completed", state=state, reason="All tickets done."
                )
            current_path = next_path
            state["current_ticket"] = safe_relpath(current_path, self._workspace_root)
            # Inform listeners immediately which ticket is about to run so the UI
            # can show the active indicator before the first turn completes.
            if emit_event is not None:
                emit_event(
                    FlowEventType.STEP_PROGRESS,
                    {
                        "message": "Selected ticket",
                        "current_ticket": state["current_ticket"],
                    },
                )
            # New ticket resets per-ticket state.
            state["ticket_turns"] = 0
            state.pop("last_agent_output", None)
            state.pop("lint", None)
            state.pop("loop_guard", None)
        state.pop("commit", None)

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
        reuse_conversation_id: Optional[str] = (
            _conv_id_raw if isinstance(_conv_id_raw, str) else None
        )

        # Read ticket (may lint-fail). In lint-retry mode, fall back to a relaxed
        # frontmatter parse so we can still execute an agent turn to repair the file.
        ticket_doc = None
        ticket_errors: list[str] = []
        if lint_errors:
            try:
                raw = current_path.read_text(encoding="utf-8")
            except OSError as exc:
                return self._pause(
                    state,
                    reason=(
                        "Ticket unreadable during lint retry for "
                        f"{safe_relpath(current_path, self._workspace_root)}: {exc}"
                    ),
                    current_ticket=safe_relpath(current_path, self._workspace_root),
                    reason_code="infra_error",
                )

            data, _ = parse_markdown_frontmatter(raw)
            agent = data.get("agent")
            agent_id = agent.strip() if isinstance(agent, str) else None
            if not agent_id:
                return self._pause(
                    state,
                    reason=(
                        "Cannot determine ticket agent during lint retry (missing frontmatter.agent). "
                        "Fix the ticket frontmatter manually and resume."
                    ),
                    current_ticket=safe_relpath(current_path, self._workspace_root),
                    reason_code="needs_user_fix",
                )

            # Validate agent id unless it is the special user sentinel.
            if agent_id != "user":
                try:
                    from ..agents.registry import validate_agent_id

                    agent_id = validate_agent_id(agent_id)
                except Exception as exc:
                    return self._pause(
                        state,
                        reason=(
                            "Cannot determine valid agent during lint retry for "
                            f"{safe_relpath(current_path, self._workspace_root)}: {exc}"
                        ),
                        current_ticket=safe_relpath(current_path, self._workspace_root),
                        reason_code="needs_user_fix",
                    )

            ticket_doc = type(
                "_TicketDocForLintRetry",
                (),
                {
                    "frontmatter": TicketFrontmatter(
                        agent=agent_id,
                        done=False,
                    )
                },
            )()
        else:
            ticket_doc, ticket_errors = read_ticket(current_path)
            if ticket_errors or ticket_doc is None:
                return self._pause(
                    state,
                    reason=f"Ticket frontmatter invalid: {safe_relpath(current_path, self._workspace_root)}",
                    reason_details="Errors:\n- " + "\n- ".join(ticket_errors),
                    current_ticket=safe_relpath(current_path, self._workspace_root),
                    reason_code="needs_user_fix",
                )

        # Built-in manual user ticket.
        if ticket_doc.frontmatter.agent == "user":
            if ticket_doc.frontmatter.done:
                # Nothing to do, will advance next step.
                return TicketResult(status="continue", state=state)
            return self._pause(
                state,
                reason=(
                    "Paused for user input. Mark ticket as done when ready: "
                    f"{safe_relpath(current_path, self._workspace_root)}"
                ),
                current_ticket=safe_relpath(current_path, self._workspace_root),
                reason_code="user_pause",
            )

        ticket_turns = int(state.get("ticket_turns") or 0)
        reply_seq = int(state.get("reply_seq") or 0)
        reply_context, reply_max_seq = self._build_reply_context(
            reply_paths=reply_paths, last_seq=reply_seq
        )

        previous_ticket_content: Optional[str] = None
        if self._config.include_previous_ticket_context:
            try:
                if current_path in ticket_paths:
                    curr_idx = ticket_paths.index(current_path)
                    if curr_idx > 0:
                        prev_path = ticket_paths[curr_idx - 1]
                        content = prev_path.read_text(encoding="utf-8")
                        previous_ticket_content = _truncate_text_by_bytes(
                            content, 16384
                        )
            except Exception:
                pass

        prompt = self._build_prompt(
            ticket_path=current_path,
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
            previous_ticket_content=previous_ticket_content,
            prior_no_change_turns=self._prior_no_change_turns(
                state, safe_relpath(current_path, self._workspace_root)
            ),
        )

        # Execute turn.
        # Build options dict with model/reasoning from ticket frontmatter if set.
        turn_options: dict[str, Any] = {}
        if ticket_doc.frontmatter.model:
            turn_options["model"] = ticket_doc.frontmatter.model
        if ticket_doc.frontmatter.reasoning:
            turn_options["reasoning"] = ticket_doc.frontmatter.reasoning
        req = AgentTurnRequest(
            agent_id=ticket_doc.frontmatter.agent,
            prompt=prompt,
            workspace_root=self._workspace_root,
            conversation_id=reuse_conversation_id,
            emit_event=emit_event,
            options=turn_options if turn_options else None,
        )

        total_turns += 1
        ticket_turns += 1
        state["total_turns"] = total_turns
        state["ticket_turns"] = ticket_turns

        repo_fingerprint_before_turn = self._repo_fingerprint()
        head_before_turn: Optional[str] = None
        try:
            head_proc = run_git(
                ["rev-parse", "HEAD"], cwd=self._workspace_root, check=True
            )
            head_before_turn = (head_proc.stdout or "").strip() or None
        except Exception:
            head_before_turn = None

        result = await self._agent_pool.run_turn(req)
        if result.error:
            state["last_agent_output"] = result.text
            state["last_agent_id"] = result.agent_id
            state["last_agent_conversation_id"] = result.conversation_id
            state["last_agent_turn_id"] = result.turn_id

            # Check if this is a network error that should be retried
            if _is_network_error(result.error):
                network_retries += 1
                if network_retries <= self._config.max_network_retries:
                    state["network_retry"] = {
                        "retries": network_retries,
                        "last_error": result.error,
                    }
                    return TicketResult(
                        status="continue",
                        state=state,
                        reason=(
                            f"Network error detected (attempt {network_retries}/{self._config.max_network_retries}): {result.error}\n"
                            "Retrying automatically..."
                        ),
                        current_ticket=safe_relpath(current_path, self._workspace_root),
                        agent_output=result.text,
                        agent_id=result.agent_id,
                        agent_conversation_id=result.conversation_id,
                        agent_turn_id=result.turn_id,
                    )

            # Not a network error or retries exhausted - pause for user intervention
            state.pop("network_retry", None)
            return self._pause(
                state,
                reason="Agent turn failed. Fix the issue and resume.",
                reason_details=f"Error: {result.error}",
                current_ticket=safe_relpath(current_path, self._workspace_root),
                reason_code="infra_error",
            )

        # Mark replies as consumed only after a successful agent turn.
        if reply_max_seq > reply_seq:
            state["reply_seq"] = reply_max_seq
        state["last_agent_output"] = result.text
        # Clear network retry state on successful turn
        state.pop("network_retry", None)
        state["last_agent_id"] = result.agent_id
        state["last_agent_conversation_id"] = result.conversation_id
        state["last_agent_turn_id"] = result.turn_id
        repo_fingerprint_after_turn = self._repo_fingerprint()

        # Best-effort: check whether the agent created a commit and whether the
        # working tree is clean, before any runner-driven checkpoint commit.
        head_after_agent: Optional[str] = None
        clean_after_agent: Optional[bool] = None
        status_after_agent: Optional[str] = None
        agent_committed_this_turn: Optional[bool] = None
        try:
            head_proc = run_git(
                ["rev-parse", "HEAD"], cwd=self._workspace_root, check=True
            )
            head_after_agent = (head_proc.stdout or "").strip() or None
            status_proc = run_git(
                ["status", "--porcelain"], cwd=self._workspace_root, check=True
            )
            status_after_agent = (status_proc.stdout or "").strip()
            clean_after_agent = not bool(status_after_agent)
            if head_before_turn and head_after_agent:
                agent_committed_this_turn = head_after_agent != head_before_turn
        except Exception:
            head_after_agent = None
            clean_after_agent = None
            status_after_agent = None
            agent_committed_this_turn = None

        # Post-turn: archive outbox if DISPATCH.md exists.
        dispatch_seq = int(state.get("dispatch_seq") or 0)
        current_ticket_id = safe_relpath(current_path, self._workspace_root)
        dispatch, dispatch_errors = archive_dispatch(
            outbox_paths,
            next_seq=dispatch_seq + 1,
            ticket_id=current_ticket_id,
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
                current_ticket=safe_relpath(current_path, self._workspace_root),
                reason_code="needs_user_fix",
            )

        if dispatch is not None:
            state["dispatch_seq"] = dispatch.seq
            state.pop("outbox_lint", None)

        # Create turn summary record for the agent's final output.
        # This appears in dispatch history as a distinct "turn summary" entry.
        turn_summary_seq = int(state.get("dispatch_seq") or 0) + 1

        # Compute diff stats for this turn (changes since head_before_turn).
        # This captures both committed and uncommitted changes made by the agent.
        turn_diff_stats = None
        try:
            if head_before_turn:
                # Compare current state (HEAD + working tree) against pre-turn commit
                turn_diff_stats = git_diff_stats(
                    self._workspace_root, from_ref=head_before_turn
                )
            else:
                # No reference commit; show all uncommitted changes
                turn_diff_stats = git_diff_stats(
                    self._workspace_root, from_ref=None, include_staged=True
                )
        except Exception:
            # Best-effort; don't block on stats computation errors
            turn_diff_stats = None

        turn_summary, turn_summary_errors = create_turn_summary(
            outbox_paths,
            next_seq=turn_summary_seq,
            agent_output=result.text or "",
            ticket_id=current_ticket_id,
            agent_id=result.agent_id,
            turn_number=total_turns,
            diff_stats=turn_diff_stats,
        )
        if turn_summary is not None:
            state["dispatch_seq"] = turn_summary.seq

            # Persist per-turn diff stats in FlowStore as a structured event
            # instead of embedding them into DISPATCH.md metadata.
            if emit_event is not None and isinstance(turn_diff_stats, dict):
                try:
                    emit_event(
                        FlowEventType.DIFF_UPDATED,
                        {
                            "ticket_id": current_ticket_id,
                            "dispatch_seq": turn_summary.seq,
                            "insertions": int(turn_diff_stats.get("insertions") or 0),
                            "deletions": int(turn_diff_stats.get("deletions") or 0),
                            "files_changed": int(
                                turn_diff_stats.get("files_changed") or 0
                            ),
                        },
                    )
                except Exception:
                    # Best-effort; do not block ticket execution on event emission.
                    pass

        # Loop guard: if the same ticket runs with no repository state change for
        # LOOP_NO_CHANGE_THRESHOLD consecutive successful turns, pause and ask for
        # user intervention instead of spinning.
        loop_guard_raw = state.get("loop_guard")
        loop_guard_state: dict[str, Any] = (
            dict(loop_guard_raw) if isinstance(loop_guard_raw, dict) else {}
        )
        current_ticket_id = safe_relpath(current_path, self._workspace_root)
        no_repo_change_this_turn = (
            isinstance(repo_fingerprint_before_turn, str)
            and isinstance(repo_fingerprint_after_turn, str)
            and repo_fingerprint_before_turn == repo_fingerprint_after_turn
        )
        lint_retry_mode = bool(lint_errors)
        if lint_retry_mode:
            state.pop("loop_guard", None)
        else:
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
            state["loop_guard"] = {
                "ticket": current_ticket_id,
                "no_change_count": no_change_count,
            }

            if no_change_count >= LOOP_NO_CHANGE_THRESHOLD:
                reason = "Ticket appears stuck: same ticket ran twice with no repository diff changes."
                details = (
                    "Runner paused to avoid repeated no-op work.\n\n"
                    f"Ticket: {current_ticket_id}\n"
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
                    current_ticket=current_ticket_id,
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
                    current_ticket=safe_relpath(current_path, self._workspace_root),
                    reason_code="needs_user_fix",
                )

            state["lint"] = {
                "errors": fm_errors,
                "retries": lint_retries,
                "conversation_id": result.conversation_id,
            }
            return TicketResult(
                status="continue",
                state=state,
                reason="Ticket frontmatter invalid; requesting agent fix.",
                current_ticket=safe_relpath(current_path, self._workspace_root),
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
                turn=total_turns, agent=result.agent_id
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
                # Enter or continue bounded commit loop.
                if commit_pending:
                    # A "commit required" turn just ran and did not succeed.
                    next_failed_attempts = commit_retries + 1
                else:
                    # Ticket just transitioned to done, but repo is still dirty.
                    next_failed_attempts = 0

                state["commit"] = {
                    "pending": True,
                    "retries": next_failed_attempts,
                    "head_before": head_before_turn,
                    "head_after": head_after_agent,
                    "agent_committed_this_turn": agent_committed_this_turn,
                    "status_porcelain": status_after_agent,
                }

                if (
                    commit_pending
                    and next_failed_attempts >= self._config.max_commit_retries
                ):
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
                    return self._pause(
                        state,
                        reason=(
                            f"Commit failed after {self._config.max_commit_retries} attempts. "
                            "Manual commit required."
                        ),
                        reason_details="".join(details_parts),
                        current_ticket=safe_relpath(current_path, self._workspace_root),
                        reason_code="needs_user_fix",
                    )

                return TicketResult(
                    status="continue",
                    state=state,
                    reason="Ticket done but commit required; requesting agent commit.",
                    current_ticket=safe_relpath(current_path, self._workspace_root),
                    agent_output=result.text,
                    agent_id=result.agent_id,
                    agent_conversation_id=result.conversation_id,
                    agent_turn_id=result.turn_id,
                )

            # Clean (or unknown) → commit satisfied (or no changes / cannot check).
            state.pop("commit", None)
            state.pop("current_ticket", None)
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
            current_ticket=safe_relpath(current_path, self._workspace_root),
            agent_output=result.text,
            agent_id=result.agent_id,
            agent_conversation_id=result.conversation_id,
            agent_turn_id=result.turn_id,
        )

    def _find_next_ticket(self, ticket_paths: list[Path]) -> Optional[Path]:
        for path in ticket_paths:
            if ticket_is_done(path):
                continue
            return path
        return None

    def _recheck_ticket_frontmatter(self, ticket_path: Path):
        try:
            raw = ticket_path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, [f"Failed to read ticket after turn: {exc}"]
        from .frontmatter import parse_markdown_frontmatter

        data, _ = parse_markdown_frontmatter(raw)
        fm, errors = lint_ticket_frontmatter(data)
        return fm, errors

    def _checkpoint_git(self, *, turn: int, agent: str) -> Optional[str]:
        """Create a best-effort git commit checkpoint.

        Returns an error string if the checkpoint failed, else None.
        """

        try:
            status_proc = run_git(
                ["status", "--porcelain"], cwd=self._workspace_root, check=True
            )
            if not (status_proc.stdout or "").strip():
                return None
            run_git(["add", "-A"], cwd=self._workspace_root, check=True)
            msg = self._config.checkpoint_message_template.format(
                run_id=self._run_id,
                turn=turn,
                agent=agent,
            )
            run_git(["commit", "-m", msg], cwd=self._workspace_root, check=True)
            return None
        except Exception as exc:
            _logger.exception("Checkpoint commit failed")
            return str(exc)

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
        try:
            head_proc = run_git(
                ["rev-parse", "HEAD"], cwd=self._workspace_root, check=True
            )
            status_proc = run_git(
                ["status", "--porcelain"], cwd=self._workspace_root, check=True
            )
            head = (head_proc.stdout or "").strip()
            status = (status_proc.stdout or "").strip()
            if not head:
                return None
            return f"{head}\n{status}"
        except Exception:
            return None

    def _create_runner_pause_dispatch(
        self,
        *,
        outbox_paths,
        state: dict[str, Any],
        title: str,
        body: str,
        ticket_id: str,
    ):
        """Create and archive a runner-generated pause dispatch."""
        try:
            outbox_paths.dispatch_path.write_text(
                f"---\nmode: pause\ntitle: {title}\n---\n\n{body}\n",
                encoding="utf-8",
            )
        except OSError:
            return None
        next_seq = int(state.get("dispatch_seq") or 0) + 1
        dispatch_record, dispatch_errors = archive_dispatch(
            outbox_paths,
            next_seq=next_seq,
            ticket_id=ticket_id,
            repo_id=self._repo_id,
            run_id=self._run_id,
            origin="runner",
        )
        if dispatch_errors:
            return None
        if dispatch_record is not None:
            state["dispatch_seq"] = dispatch_record.seq
        return dispatch_record

    def _build_reply_context(self, *, reply_paths, last_seq: int) -> tuple[str, int]:
        """Render new human replies (reply_history) into a prompt block.

        Returns (rendered_text, max_seq_seen).
        """

        history_dir = getattr(reply_paths, "reply_history_dir", None)
        if history_dir is None:
            return "", last_seq
        if not history_dir.exists() or not history_dir.is_dir():
            return "", last_seq

        entries: list[tuple[int, Path]] = []
        try:
            for child in history_dir.iterdir():
                try:
                    if not child.is_dir():
                        continue
                    name = child.name
                    if not (len(name) == 4 and name.isdigit()):
                        continue
                    seq = int(name)
                    if seq <= last_seq:
                        continue
                    entries.append((seq, child))
                except OSError:
                    continue
        except OSError:
            return "", last_seq

        if not entries:
            return "", last_seq

        entries.sort(key=lambda x: x[0])
        max_seq = max(seq for seq, _ in entries)

        blocks: list[str] = []
        for seq, entry_dir in entries:
            reply_path = entry_dir / "USER_REPLY.md"
            reply, errors = (
                parse_user_reply(reply_path)
                if reply_path.exists()
                else (None, ["USER_REPLY.md missing"])
            )

            block_lines: list[str] = [f"[USER_REPLY {seq:04d}]"]
            if errors:
                block_lines.append("Errors:\n- " + "\n- ".join(errors))
            if reply is not None:
                if reply.title:
                    block_lines.append(f"Title: {reply.title}")
                if reply.body:
                    block_lines.append(reply.body)

            attachments: list[str] = []
            try:
                for child in sorted(entry_dir.iterdir(), key=lambda p: p.name):
                    try:
                        if child.name.startswith("."):
                            continue
                        if child.name == "USER_REPLY.md":
                            continue
                        if child.is_dir():
                            continue
                        attachments.append(safe_relpath(child, self._workspace_root))
                    except OSError:
                        continue
            except OSError:
                attachments = []

            if attachments:
                block_lines.append("Attachments:\n- " + "\n- ".join(attachments))

            blocks.append("\n".join(block_lines).strip())

        rendered = "\n\n".join(blocks).strip()
        return rendered, max_seq

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
            "ticket_block": ticket_block,
        }

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
                f"{checkpoint_block}\n\n"
                f"{commit_block}\n\n"
                f"{lint_block}\n\n"
                f"{loop_guard_block}\n\n"
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

        prompt = _shrink_prompt(
            max_bytes=self._config.prompt_max_bytes,
            render=render,
            sections=sections,
            order=[
                "prev_block",
                "prev_ticket_block",
                "reply_block",
                "workspace_block",
                "ticket_block",
            ],
        )
        return prompt

    def _prior_no_change_turns(self, state: dict[str, Any], ticket_id: str) -> int:
        loop_guard_raw = state.get("loop_guard")
        loop_guard_state = (
            dict(loop_guard_raw) if isinstance(loop_guard_raw, dict) else {}
        )
        if loop_guard_state.get("ticket") != ticket_id:
            return 0
        return int(loop_guard_state.get("no_change_count") or 0)
