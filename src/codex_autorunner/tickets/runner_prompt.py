from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..core.apps.prompt_hints import build_installed_apps_prompt_hint
from . import runner_prompt_support as _support
from .files import safe_relpath
from .runner_prompt_support import (
    FULL_TICKET_FLOW_INSTRUCTIONS,
    TicketFlowPromptModel,
    TicketFlowPromptSections,
    build_checkpoint_block,
    build_commit_block,
    build_lint_block,
    build_loop_guard_block,
    build_previous_ticket_block,
    build_ticket_block,
    build_workspace_block,
    reduce_ticket_flow_prompt_to_budget,
    render_ticket_flow_prompt,
    validate_ticket_flow_prompt,
)

_logger = logging.getLogger(__name__)

CAR_HUD_MAX_LINES = 14
CAR_HUD_MAX_CHARS = 900
# Upper bound on static template bytes outside ``prev_block`` so huge
# ``last_agent_output`` can be capped once before section shrinking runs.
_PREVIOUS_OUTPUT_HEADROOM_BYTES = 1200

_truncate_text_by_bytes = _support.truncate_text_by_bytes
_preserve_ticket_structure = _support.preserve_ticket_structure
_shrink_prompt = _support.shrink_prompt


def _build_car_hud() -> str:
    """Return a compact, deterministic CAR self-description block."""
    lines = [
        "CAR HUD (stable, bounded, non-secret-bearing):",
        "- Runtime root: `.codex-autorunner/`",
        "- Ticket flow semantics: process `TICKET-###*.md` in ascending index order; run the first ticket where frontmatter `done` is not `true`.",
        "- Self-description command: `car describe --json`",
        "- Canonical self-description docs: `.codex-autorunner/docs/self-description-contract.md`",
        "- Canonical self-description schema: `.codex-autorunner/docs/car-describe.schema.json`",
        "- Template discovery: `car templates repos list --json`",
        "- Template apply: `car templates apply <repo_id>:<path>[@<ref>]`",
    ]
    clipped_lines = lines[:CAR_HUD_MAX_LINES]
    hud = "\n".join(clipped_lines)
    if len(hud) > CAR_HUD_MAX_CHARS:
        hud = hud[: CAR_HUD_MAX_CHARS - 3] + "..."
    return hud


def _build_apps_hint(workspace_root: Path) -> str:
    try:
        return build_installed_apps_prompt_hint(workspace_root)
    except Exception as exc:
        _logger.warning("App hint generation failed, degrading: %s", exc)
        return ""


def _build_prompt_model(
    *,
    ticket_path: Path,
    workspace_root: Path,
    last_agent_output: Optional[str],
    last_checkpoint_error: Optional[str],
    commit_required: bool,
    commit_attempt: int,
    commit_max_attempts: int,
    outbox_paths: Any,
    lint_errors: Optional[list[str]],
    reply_context: Optional[str],
    requested_context: Optional[str],
    previous_ticket_content: Optional[str],
    prior_no_change_turns: int,
    prompt_max_bytes: int,
) -> TicketFlowPromptModel:
    rel_ticket = safe_relpath(ticket_path, workspace_root)
    prev_block = last_agent_output or ""
    if prev_block:
        cap = max(prompt_max_bytes - _PREVIOUS_OUTPUT_HEADROOM_BYTES, 1)
        if len(prev_block.encode("utf-8")) > cap:
            prev_block = _truncate_text_by_bytes(prev_block, cap)
    return TicketFlowPromptModel(
        instructions=FULL_TICKET_FLOW_INSTRUCTIONS,
        include_optional_sections=True,
        rel_ticket=rel_ticket,
        rel_dispatch_dir=safe_relpath(outbox_paths.dispatch_dir, workspace_root),
        rel_dispatch_path=safe_relpath(outbox_paths.dispatch_path, workspace_root),
        car_hud=_build_car_hud(),
        apps_hint=_build_apps_hint(workspace_root),
        checkpoint_block=build_checkpoint_block(last_checkpoint_error),
        commit_block=build_commit_block(
            commit_required=commit_required,
            commit_attempt=commit_attempt,
            commit_max_attempts=commit_max_attempts,
        ),
        lint_block=build_lint_block(lint_errors),
        loop_guard_block=build_loop_guard_block(prior_no_change_turns),
        sections=TicketFlowPromptSections(
            prev_block=prev_block,
            prev_ticket_block=build_previous_ticket_block(previous_ticket_content),
            reply_block=reply_context or "",
            requested_context_block=requested_context or "",
            workspace_block=build_workspace_block(workspace_root),
            ticket_block=build_ticket_block(ticket_path, rel_ticket),
        ),
    )


def build_prompt(
    *,
    ticket_path: Path,
    workspace_root: Path,
    ticket_doc: Any,
    last_agent_output: Optional[str],
    last_checkpoint_error: Optional[str] = None,
    commit_required: bool = False,
    commit_attempt: int = 0,
    commit_max_attempts: int = 2,
    outbox_paths: Any,
    lint_errors: Optional[list[str]],
    reply_context: Optional[str] = None,
    requested_context: Optional[str] = None,
    previous_ticket_content: Optional[str] = None,
    prior_no_change_turns: int = 0,
    prompt_max_bytes: int = 5 * 1024 * 1024,
) -> str:
    """Build the full prompt for an agent turn."""
    _ = ticket_doc
    model = _build_prompt_model(
        ticket_path=ticket_path,
        workspace_root=workspace_root,
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
        prompt_max_bytes=prompt_max_bytes,
    )
    model = reduce_ticket_flow_prompt_to_budget(model, max_bytes=prompt_max_bytes)
    prompt = render_ticket_flow_prompt(model)
    if len(prompt.encode("utf-8")) > prompt_max_bytes:
        prompt = _truncate_text_by_bytes(prompt, prompt_max_bytes)
    validate_ticket_flow_prompt(prompt, max_bytes=prompt_max_bytes)
    return prompt
