from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..core.apps.prompt_hints import build_installed_apps_prompt_hint
from . import runner_prompt_support as _support
from .files import safe_relpath
from .runner_prompt_support import (
    MAIN_SECTION_ORDER,
    build_checkpoint_block,
    build_commit_block,
    build_lint_block,
    build_loop_guard_block,
    build_previous_ticket_block,
    build_ticket_block,
    build_ticket_first_fallback_prompt,
    build_workspace_block,
    prompt_has_ticket_control_plane,
    render_full_prompt,
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
    rel_ticket = safe_relpath(ticket_path, workspace_root)
    rel_dispatch_dir = safe_relpath(outbox_paths.dispatch_dir, workspace_root)
    rel_dispatch_path = safe_relpath(outbox_paths.dispatch_path, workspace_root)
    checkpoint_block = build_checkpoint_block(last_checkpoint_error)
    apps_hint = _build_apps_hint(workspace_root)
    commit_block = build_commit_block(
        commit_required=commit_required,
        commit_attempt=commit_attempt,
        commit_max_attempts=commit_max_attempts,
    )
    lint_block = build_lint_block(lint_errors)
    loop_guard_block = build_loop_guard_block(prior_no_change_turns)
    ticket_block = build_ticket_block(ticket_path, rel_ticket)
    prev_block = last_agent_output or ""
    if prev_block:
        cap = max(prompt_max_bytes - _PREVIOUS_OUTPUT_HEADROOM_BYTES, 1)
        if len(prev_block.encode("utf-8")) > cap:
            prev_block = _truncate_text_by_bytes(prev_block, cap)
    sections = {
        "prev_block": prev_block,
        "prev_ticket_block": build_previous_ticket_block(previous_ticket_content),
        "workspace_block": build_workspace_block(workspace_root),
        "reply_block": reply_context or "",
        "requested_context_block": requested_context or "",
        "ticket_block": ticket_block,
    }
    prompt = _shrink_prompt(
        max_bytes=prompt_max_bytes,
        render=lambda: render_full_prompt(
            rel_ticket=rel_ticket,
            rel_dispatch_dir=rel_dispatch_dir,
            rel_dispatch_path=rel_dispatch_path,
            car_hud=_build_car_hud(),
            apps_hint=apps_hint,
            checkpoint_block=checkpoint_block,
            commit_block=commit_block,
            lint_block=lint_block,
            loop_guard_block=loop_guard_block,
            sections=sections,
        ),
        sections=sections,
        order=MAIN_SECTION_ORDER,
    )
    if not prompt_has_ticket_control_plane(prompt, ticket_doc):
        cap = max(prompt_max_bytes - _PREVIOUS_OUTPUT_HEADROOM_BYTES, 1)
        fallback_prev = sections["prev_block"]
        raw_prev = last_agent_output if isinstance(last_agent_output, str) else ""
        if raw_prev and len(raw_prev.encode("utf-8")) > cap:
            fallback_prev = _truncate_text_by_bytes(raw_prev, cap)
        prompt = build_ticket_first_fallback_prompt(
            max_bytes=prompt_max_bytes,
            rel_ticket=rel_ticket,
            rel_dispatch_path=rel_dispatch_path,
            prev_block=fallback_prev,
            checkpoint_block=checkpoint_block,
            commit_block=commit_block,
            lint_block=lint_block,
            loop_guard_block=loop_guard_block,
            ticket_block=ticket_block,
        )
    return prompt
