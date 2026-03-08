from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from ..contextspace.paths import contextspace_doc_path
from .files import safe_relpath

_logger = logging.getLogger(__name__)

WORKSPACE_DOC_MAX_CHARS = 4000
TRUNCATION_MARKER = "\n\n[... TRUNCATED ...]\n\n"
CAR_HUD_MAX_LINES = 14
CAR_HUD_MAX_CHARS = 900


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


def _preserve_ticket_structure(ticket_block: str, max_bytes: int) -> str:
    """Truncate ticket block while preserving prefix and ticket frontmatter."""
    if len(ticket_block.encode("utf-8")) <= max_bytes:
        return ticket_block

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

    preserve_end = second_marker_idx + len(marker)
    preserved_part = ticket_block[:preserve_end]

    preserved_bytes = len(preserved_part.encode("utf-8"))
    marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
    remaining_bytes = max(max_bytes - preserved_bytes, 0)

    if remaining_bytes > 0:
        body = ticket_block[preserve_end:]
        body_budget = max(remaining_bytes - marker_bytes, 0)
        truncated_body = _truncate_text_by_bytes(body, body_budget)
        return preserved_part + truncated_body

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
) -> str:
    """Build the full prompt for an agent turn."""
    rel_ticket = safe_relpath(ticket_path, workspace_root)
    rel_dispatch_dir = safe_relpath(outbox_paths.dispatch_dir, workspace_root)
    rel_dispatch_path = safe_relpath(outbox_paths.dispatch_path, workspace_root)

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

    lint_block = ""
    if lint_errors:
        lint_block = (
            "<CAR_TICKET_FRONTMATTER_LINT_REPAIR>\n"
            "Ticket frontmatter lint failed. Fix ONLY the ticket YAML frontmatter to satisfy:\n- "
            + "\n- ".join(lint_errors)
            + "\n</CAR_TICKET_FRONTMATTER_LINT_REPAIR>"
        )

    loop_guard_block = ""
    if prior_no_change_turns > 0:
        loop_guard_block = (
            "<CAR_LOOP_GUARD>\n"
            "Previous turn(s) on this ticket produced no repository diff change.\n"
            f"Consecutive no-change turns so far: {prior_no_change_turns}\n"
            "If you are still blocked, write DISPATCH.md with mode: pause instead of retrying unchanged steps.\n"
            "</CAR_LOOP_GUARD>"
        )

    reply_block = reply_context or ""
    requested_context_block = requested_context or ""

    workspace_block = ""
    workspace_docs: list[tuple[str, str, str]] = []
    for key, label in (
        ("active_context", "Active context"),
        ("decisions", "Decisions"),
        ("spec", "Spec"),
    ):
        path = contextspace_doc_path(workspace_root, key)
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
                safe_relpath(path, workspace_root),
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

    prev_block = last_agent_output or ""

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
            '- You do not need a "final" dispatch when you finish; the runner will archive your turn output automatically. Dispatch only if you want something to stand out or you need user input.\n\n'
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
            '- Prefer referencing these docs instead of creating duplicate "shadow" docs elsewhere.\n\n'
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

    prompt = _shrink_prompt(
        max_bytes=5 * 1024 * 1024,
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
    return prompt
