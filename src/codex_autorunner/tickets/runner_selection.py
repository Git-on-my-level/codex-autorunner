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
from .models import TicketContextEntry, TicketDoc, TicketFrontmatter, TicketRunConfig
from .replies import parse_user_reply
from .runner_prompt import _truncate_text_by_bytes, build_prompt
from .runner_types import (
    PreTurnPlan,
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

    current_ticket_done = ticket_is_done(current_path) if current_path else False
    if current_path and commit_pending and not current_ticket_done:
        state_updates["commit"] = None
        commit_pending = False

    if current_path and current_ticket_done and not commit_pending:
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
    profile = data.get("profile")
    profile_id = profile.strip() if isinstance(profile, str) else None
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
        except (ImportError, ValueError) as exc:
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
                    profile=profile_id or None,
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


TICKET_CONTEXT_DEFAULT_MAX_BYTES = 4096
TICKET_CONTEXT_TOTAL_MAX_BYTES = 16384


def load_ticket_context_block(
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


def build_reply_context(
    *,
    workspace_root: Path,
    reply_paths,
    last_seq: int,
) -> tuple[str, int]:
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
                    attachments.append(safe_relpath(child, workspace_root))
                except OSError:
                    continue
        except OSError:
            attachments = []

        if attachments:
            block_lines.append("Attachments:\n- " + "\n- ".join(attachments))

        blocks.append("\n".join(block_lines).strip())

    rendered = "\n\n".join(blocks).strip()
    return rendered, max_seq


def _prior_no_change_turns(state: dict[str, Any], ticket_id: str) -> int:
    loop_guard_raw = state.get("loop_guard")
    loop_guard_state = dict(loop_guard_raw) if isinstance(loop_guard_raw, dict) else {}
    if loop_guard_state.get("ticket") != ticket_id:
        return 0
    return int(loop_guard_state.get("no_change_count") or 0)


def plan_pre_turn(
    *,
    selection_result: TicketSelectionResult,
    workspace_root: Path,
    ticket_dir: Path,
    config: TicketRunConfig,
    state: dict[str, Any],
    run_id: str,
    outbox_paths,
    reply_paths,
) -> PreTurnPlan:
    """Plan the pre-turn phase: validate, load context, build prompt.

    Returns a PreTurnPlan with either ready-to-execute prompt inputs
    or a terminal pause/failure/skip result.
    """
    assert selection_result.selected is not None
    current_path = selection_result.selected.path
    current_ticket_path = safe_relpath(current_path, workspace_root)
    state_updates: dict[str, Any] = {}

    _commit_raw = state.get("commit")
    commit_state = _commit_raw if isinstance(_commit_raw, dict) else {}
    commit_pending = bool(commit_state.get("pending"))
    commit_retries = int(commit_state.get("retries") or 0)

    if state.get("status") == "paused":
        state_updates["status"] = "running"
        state_updates["reason"] = None
        state_updates["reason_details"] = None
        state_updates["reason_code"] = None
        state_updates["pause_context"] = None

    _lint_raw = state.get("lint")
    lint_state = _lint_raw if isinstance(_lint_raw, dict) else {}
    _lint_errors_raw = lint_state.get("errors")
    lint_errors: list[str] = (
        _lint_errors_raw if isinstance(_lint_errors_raw, list) else []
    )
    lint_retries = int(lint_state.get("retries") or 0)
    _conv_id_raw = lint_state.get("conversation_id")
    reuse_conversation_id: Optional[str] = (
        _conv_id_raw if isinstance(_conv_id_raw, str) else None
    )

    validation_result = validate_ticket_for_execution(
        ticket_path=current_path,
        workspace_root=workspace_root,
        state=state,
        lint_errors=lint_errors if lint_errors else None,
    )
    if validation_result.status == "paused":
        reason_details = (
            "Errors:\n- " + "\n- ".join(validation_result.errors)
            if validation_result.errors
            else None
        )
        return PreTurnPlan(
            status="paused",
            state_updates=state_updates,
            pause_reason=validation_result.pause_reason or "Ticket validation failed.",
            pause_reason_details=reason_details,
            pause_reason_code=validation_result.pause_reason_code or "needs_user_fix",
            current_ticket_path=current_ticket_path,
        )
    if not validation_result.validated:
        return PreTurnPlan(
            status="paused",
            state_updates=state_updates,
            pause_reason="Ticket validation failed unexpectedly.",
            pause_reason_code="infra_error",
            current_ticket_path=current_ticket_path,
        )

    ticket_doc = validation_result.validated.ticket_doc
    current_ticket_id = ticket_doc.frontmatter.ticket_id
    state_updates["current_ticket_id"] = current_ticket_id

    if validation_result.validated.skip_execution:
        return PreTurnPlan(
            status="skip",
            state_updates=state_updates,
        )

    reply_seq = int(state.get("reply_seq") or 0)
    reply_context, reply_max_seq = build_reply_context(
        workspace_root=workspace_root,
        reply_paths=reply_paths,
        last_seq=reply_seq,
    )

    requested_context_block, missing_required_context = load_ticket_context_block(
        workspace_root=workspace_root,
        entries=ticket_doc.frontmatter.context,
    )
    if missing_required_context:
        details = "Missing required ticket context files:\n- " + "\n- ".join(
            missing_required_context
        )
        failure_updates = dict(state_updates)
        failure_updates["status"] = "failed"
        failure_updates["reason_code"] = "missing_required_context"
        failure_updates["reason"] = "Required ticket context file missing."
        failure_updates["reason_details"] = details
        return PreTurnPlan(
            status="failed",
            state_updates=failure_updates,
            pause_reason="Required ticket context file missing.",
            pause_reason_details=details,
            pause_reason_code="missing_required_context",
            current_ticket_path=current_ticket_path,
        )

    previous_ticket_content: Optional[str] = None
    if config.include_previous_ticket_context:
        try:
            ticket_paths = list_ticket_paths(ticket_dir)
            if current_path in ticket_paths:
                curr_idx = ticket_paths.index(current_path)
                if curr_idx > 0:
                    prev_path = ticket_paths[curr_idx - 1]
                    content = prev_path.read_text(encoding="utf-8")
                    previous_ticket_content = _truncate_text_by_bytes(content, 16384)
        except OSError:
            _logger.debug("failed to read previous ticket content", exc_info=True)

    prompt = build_prompt(
        ticket_path=current_path,
        workspace_root=workspace_root,
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
        commit_max_attempts=config.max_commit_retries,
        outbox_paths=outbox_paths,
        lint_errors=lint_errors if lint_errors else None,
        reply_context=reply_context,
        requested_context=requested_context_block,
        previous_ticket_content=previous_ticket_content,
        prior_no_change_turns=_prior_no_change_turns(state, current_ticket_id),
        prompt_max_bytes=config.prompt_max_bytes,
    )

    turn_options: dict[str, Any] = {}
    if ticket_doc.frontmatter.model:
        turn_options["model"] = ticket_doc.frontmatter.model
    if ticket_doc.frontmatter.reasoning:
        turn_options["reasoning"] = ticket_doc.frontmatter.reasoning
    turn_options["ticket_flow_run_id"] = run_id

    return PreTurnPlan(
        status="ready",
        state_updates=state_updates,
        prompt=prompt,
        ticket_doc=ticket_doc,
        current_ticket_id=current_ticket_id,
        conversation_id=reuse_conversation_id,
        turn_options=turn_options if turn_options else None,
        reply_max_seq=reply_max_seq,
        reply_seq=reply_seq,
        commit_pending=commit_pending,
        commit_retries=commit_retries,
        lint_errors=lint_errors if lint_errors else None,
        lint_retries=lint_retries,
        current_ticket_path=current_ticket_path,
    )
