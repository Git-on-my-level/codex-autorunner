from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ..contextspace.paths import contextspace_doc_path
from .files import safe_relpath

_logger = logging.getLogger(__name__)

CONTEXTSPACE_DOC_MAX_CHARS = 4000
PREVIOUS_TICKET_MAX_BYTES = 16384
# Raw ticket markdown lives inside <CAR_CURRENT_TICKET_FILE> under these tags.
CAR_TICKET_OPEN = "<CAR_TICKET>"
CAR_TICKET_CLOSE = "</CAR_TICKET>"
TRUNCATION_MARKER = "\n\n[... TRUNCATED ...]\n\n"
FULL_TICKET_FLOW_INSTRUCTIONS = (
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
    "- If present, `.codex-autorunner/bin/ticket_tool.py` can create/insert/move tickets; `.codex-autorunner/bin/lint_tickets.py` is the canonical ticket linter and `ticket_tool.py lint` is only a compatibility wrapper (see `.codex-autorunner/ABOUT_CAR.md`).\n"
    "Using ticket templates (optional):\n"
    "- If you need a standard ticket pattern, prefer: `car templates fetch <repo_id>:<path>[@<ref>]`\n"
    "  - Trusted repos skip scanning; untrusted repos are scanned (cached by blob SHA).\n\n"
    "Contextspace docs:\n"
    "- You may update or add context under `.codex-autorunner/contextspace/` so future ticket turns have durable context.\n"
    '- Prefer referencing these docs instead of creating duplicate "shadow" docs elsewhere.\n\n'
    "Repo hygiene:\n"
    "- Do not add new `.codex-autorunner/` artifacts to git unless they are already tracked."
)
COMPACT_TICKET_FLOW_INSTRUCTIONS = (
    "CAR ticket flow: read ticket, change repo, update ticket, set "
    "`done: true` only when complete. If blocked, write DISPATCH.md "
    "`mode: pause`."
)
REQUIRED_PROMPT_MARKERS = (
    "<CAR_TICKET_FLOW_PROMPT>",
    "</CAR_TICKET_FLOW_PROMPT>",
    "<CAR_TICKET_FLOW_INSTRUCTIONS>",
    "</CAR_TICKET_FLOW_INSTRUCTIONS>",
    "<CAR_RUNTIME_PATHS>",
    "</CAR_RUNTIME_PATHS>",
    "<CAR_CURRENT_TICKET_FILE>",
    "</CAR_CURRENT_TICKET_FILE>",
    CAR_TICKET_OPEN,
    CAR_TICKET_CLOSE,
)


@dataclass(frozen=True)
class PromptSectionPolicy:
    key: str
    required: bool = False
    preserve_ticket_structure: bool = False


SECTION_REDUCTION_POLICY = (
    PromptSectionPolicy("prev_block"),
    PromptSectionPolicy("prev_ticket_block"),
    PromptSectionPolicy("reply_block"),
    PromptSectionPolicy("requested_context_block"),
    PromptSectionPolicy("contextspace_block"),
    PromptSectionPolicy("ticket_block", required=True, preserve_ticket_structure=True),
)
MAIN_SECTION_ORDER = [policy.key for policy in SECTION_REDUCTION_POLICY]


def _section_key_affects_render_size(
    key: str, *, include_optional_sections: bool
) -> bool:
    """Return whether mutating this section key can change rendered prompt size."""
    if include_optional_sections:
        return True
    return key == "ticket_block"


@dataclass(frozen=True)
class TicketFlowPromptSections:
    prev_block: str
    prev_ticket_block: str
    reply_block: str
    requested_context_block: str
    contextspace_block: str
    ticket_block: str

    def as_dict(self) -> dict[str, str]:
        return {
            "prev_block": self.prev_block,
            "prev_ticket_block": self.prev_ticket_block,
            "reply_block": self.reply_block,
            "requested_context_block": self.requested_context_block,
            "contextspace_block": self.contextspace_block,
            "ticket_block": self.ticket_block,
        }

    @classmethod
    def from_dict(cls, values: dict[str, str]) -> TicketFlowPromptSections:
        return cls(
            prev_block=values.get("prev_block", ""),
            prev_ticket_block=values.get("prev_ticket_block", ""),
            reply_block=values.get("reply_block", ""),
            requested_context_block=values.get("requested_context_block", ""),
            contextspace_block=values.get("contextspace_block", ""),
            ticket_block=values.get("ticket_block", ""),
        )


@dataclass(frozen=True)
class TicketFlowPromptModel:
    instructions: str
    include_optional_sections: bool
    rel_ticket: str
    rel_dispatch_dir: str
    rel_dispatch_path: str
    car_hud: str
    apps_hint: str
    checkpoint_block: str
    commit_block: str
    lint_block: str
    loop_guard_block: str
    sections: TicketFlowPromptSections

    def with_sections(
        self, sections: TicketFlowPromptSections
    ) -> TicketFlowPromptModel:
        return TicketFlowPromptModel(
            instructions=self.instructions,
            include_optional_sections=self.include_optional_sections,
            rel_ticket=self.rel_ticket,
            rel_dispatch_dir=self.rel_dispatch_dir,
            rel_dispatch_path=self.rel_dispatch_path,
            car_hud=self.car_hud,
            apps_hint=self.apps_hint,
            checkpoint_block=self.checkpoint_block,
            commit_block=self.commit_block,
            lint_block=self.lint_block,
            loop_guard_block=self.loop_guard_block,
            sections=sections,
        )

    def with_instructions(self, instructions: str) -> TicketFlowPromptModel:
        return TicketFlowPromptModel(
            instructions=instructions,
            include_optional_sections=self.include_optional_sections,
            rel_ticket=self.rel_ticket,
            rel_dispatch_dir=self.rel_dispatch_dir,
            rel_dispatch_path=self.rel_dispatch_path,
            car_hud=self.car_hud,
            apps_hint=self.apps_hint,
            checkpoint_block=self.checkpoint_block,
            commit_block=self.commit_block,
            lint_block=self.lint_block,
            loop_guard_block=self.loop_guard_block,
            sections=self.sections,
        )

    def without_optional_static_blocks(self) -> TicketFlowPromptModel:
        return TicketFlowPromptModel(
            instructions=self.instructions,
            include_optional_sections=False,
            rel_ticket=self.rel_ticket,
            rel_dispatch_dir=self.rel_dispatch_dir,
            rel_dispatch_path=self.rel_dispatch_path,
            car_hud="",
            apps_hint="",
            checkpoint_block=self.checkpoint_block,
            commit_block=self.commit_block,
            lint_block=self.lint_block,
            loop_guard_block=self.loop_guard_block,
            sections=self.sections,
        )


def truncate_text_by_bytes(text: str, max_bytes: int) -> str:
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


def preserve_ticket_structure(ticket_block: str, max_bytes: int) -> str:
    """Truncate ticket block while preserving prefix and ticket frontmatter."""
    if len(ticket_block.encode("utf-8")) <= max_bytes:
        return ticket_block

    marker = "\n---\n"
    ticket_md_idx = ticket_block.find(CAR_TICKET_OPEN)
    if ticket_md_idx == -1:
        return truncate_text_by_bytes(ticket_block, max_bytes)

    first_marker_idx = ticket_block.find(marker, ticket_md_idx)
    if first_marker_idx == -1:
        return truncate_text_by_bytes(ticket_block, max_bytes)

    second_marker_idx = ticket_block.find(marker, first_marker_idx + 1)
    if second_marker_idx == -1:
        return truncate_text_by_bytes(ticket_block, max_bytes)

    preserve_end = second_marker_idx + len(marker)
    preserved_part = ticket_block[:preserve_end]
    preserved_bytes = len(preserved_part.encode("utf-8"))
    suffix_start = ticket_block.find(f"\n{CAR_TICKET_CLOSE}", preserve_end)
    suffix = ticket_block[suffix_start:] if suffix_start != -1 else ""
    suffix_bytes = len(suffix.encode("utf-8"))
    remaining_bytes = max(max_bytes - preserved_bytes - suffix_bytes, 0)

    if remaining_bytes > 0:
        body = (
            ticket_block[preserve_end:suffix_start]
            if suffix_start != -1
            else ticket_block[preserve_end:]
        )
        return preserved_part + truncate_text_by_bytes(body, remaining_bytes) + suffix

    return truncate_text_by_bytes(ticket_block, max_bytes)


def _minimum_ticket_section_bytes(ticket_block: str) -> int:
    marker = "\n---\n"
    ticket_md_idx = ticket_block.find(CAR_TICKET_OPEN)
    if ticket_md_idx == -1:
        return 1
    first_marker_idx = ticket_block.find(marker, ticket_md_idx)
    if first_marker_idx == -1:
        return 1
    second_marker_idx = ticket_block.find(marker, first_marker_idx + 1)
    if second_marker_idx == -1:
        return 1
    preserve_end = second_marker_idx + len(marker)
    suffix_start = ticket_block.find(f"\n{CAR_TICKET_CLOSE}", preserve_end)
    suffix = ticket_block[suffix_start:] if suffix_start != -1 else ""
    minimum = (ticket_block[:preserve_end] + TRUNCATION_MARKER + suffix).encode("utf-8")
    return len(minimum)


def _minimum_section_bytes(key: str, value: str) -> int:
    if key == "ticket_block":
        return _minimum_ticket_section_bytes(value)
    return 1


def _truncate_section(key: str, value: str, max_bytes: int) -> str:
    policy = next(
        (candidate for candidate in SECTION_REDUCTION_POLICY if candidate.key == key),
        PromptSectionPolicy(key),
    )
    if policy.preserve_ticket_structure:
        return preserve_ticket_structure(value, max_bytes)
    return truncate_text_by_bytes(value, max_bytes)


def shrink_prompt(
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

    marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
    for key in order:
        if len(prompt.encode("utf-8")) <= max_bytes:
            break
        value = sections.get(key, "")
        if not value:
            continue
        overflow = len(prompt.encode("utf-8")) - max_bytes
        value_bytes = len(value.encode("utf-8"))
        new_limit = max(value_bytes - overflow, 0)
        if 0 < new_limit < value_bytes:
            new_limit = max(new_limit - marker_bytes, 0)
        if new_limit == 0 and value_bytes > 0:
            new_limit = min(value_bytes, marker_bytes + 1)
        sections[key] = _truncate_section(key, value, new_limit)
        prompt = render()

    # Never tail-truncate the assembled prompt: the ticket shell places
    # ``<CAR_PREVIOUS_AGENT_OUTPUT>`` near the end, so a single cut from the
    # start would drop the marker while leaving earlier XML intact.
    while len(prompt.encode("utf-8")) > max_bytes:
        progressed = False
        for key in order:
            if len(prompt.encode("utf-8")) <= max_bytes:
                break
            value = sections.get(key, "")
            if not value:
                continue
            value_bytes = len(value.encode("utf-8"))
            if value_bytes <= 1:
                sections[key] = ""
            else:
                new_limit = max(value_bytes // 2, 1)
                sections[key] = _truncate_section(key, value, new_limit)
            prompt = render()
            progressed = True
            break
        if not progressed:
            break

    # Static shell text (instructions, HUD, etc.) is not in ``sections``; if the
    # prompt is still over budget, clear variable sections entirely, then tighten
    # the ticket block while preserving frontmatter (never keep only the tail of
    # the rendered prompt — that drops YAML ``agent``/``done`` lines near the
    # start of ``<CAR_TICKET>``).
    while len(prompt.encode("utf-8")) > max_bytes:
        progressed = False
        for key in order:
            if sections.get(key, ""):
                sections[key] = ""
                prompt = render()
                progressed = True
                break
        if not progressed:
            break

    ticket_key = "ticket_block"
    while len(prompt.encode("utf-8")) > max_bytes:
        tb = sections.get(ticket_key, "")
        if not tb:
            break
        tb_bytes = len(tb.encode("utf-8"))
        if tb_bytes <= 1:
            sections[ticket_key] = ""
            prompt = render()
            break
        new_limit = max(tb_bytes // 2, 1)
        sections[ticket_key] = preserve_ticket_structure(tb, new_limit)
        prompt = render()

    if len(prompt.encode("utf-8")) > max_bytes:
        prompt = truncate_text_by_bytes(prompt, max_bytes)

    return prompt


def build_checkpoint_block(last_checkpoint_error: str | None) -> str:
    if not last_checkpoint_error:
        return ""
    return (
        "<CAR_CHECKPOINT_WARNING>\n"
        "WARNING: The previous checkpoint git commit failed (often due to pre-commit hooks).\n"
        "Resolve this before proceeding, or future turns may fail to checkpoint.\n\n"
        "Checkpoint error:\n"
        f"{last_checkpoint_error}\n"
        "</CAR_CHECKPOINT_WARNING>"
    )


def build_commit_block(
    *,
    commit_required: bool,
    commit_attempt: int,
    commit_max_attempts: int,
) -> str:
    if not commit_required:
        return ""
    attempts_remaining = max(commit_max_attempts - commit_attempt + 1, 0)
    return (
        "<CAR_COMMIT_REQUIRED>\n"
        "ACTION REQUIRED: The repo is dirty but the ticket is marked done.\n"
        "Commit your changes (ensuring any pre-commit hooks pass) so the flow can advance.\n\n"
        f"Attempts remaining before user intervention: {attempts_remaining}\n"
        "</CAR_COMMIT_REQUIRED>"
    )


def build_lint_block(lint_errors: list[str] | None) -> str:
    if not lint_errors:
        return ""
    return (
        "<CAR_TICKET_FRONTMATTER_LINT_REPAIR>\n"
        "Ticket frontmatter lint failed. Fix ONLY the ticket YAML frontmatter to satisfy:\n- "
        + "\n- ".join(lint_errors)
        + "\n</CAR_TICKET_FRONTMATTER_LINT_REPAIR>"
    )


def build_loop_guard_block(prior_no_change_turns: int) -> str:
    if prior_no_change_turns <= 0:
        return ""
    return (
        "<CAR_LOOP_GUARD>\n"
        "Previous turn(s) on this ticket produced no repository diff change.\n"
        f"Consecutive no-change turns so far: {prior_no_change_turns}\n"
        "If you are still blocked, write DISPATCH.md with mode: pause instead of retrying unchanged steps.\n"
        "</CAR_LOOP_GUARD>"
    )


def build_contextspace_block(workspace_root: Path) -> str:
    entries: list[tuple[str, str, str]] = []
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
        if snippet:
            entries.append(
                (
                    label,
                    safe_relpath(path, workspace_root),
                    snippet[:CONTEXTSPACE_DOC_MAX_CHARS],
                )
            )
    if not entries:
        return ""
    return "\n\n".join(f"{label} [{rel}]:\n{body}" for label, rel, body in entries)


def build_ticket_block(ticket_path: Path, rel_ticket: str) -> str:
    ticket_raw_content = ticket_path.read_text(encoding="utf-8")
    return (
        "<CAR_CURRENT_TICKET_FILE>\n"
        f"PATH: {rel_ticket}\n"
        f"{CAR_TICKET_OPEN}\n"
        f"{ticket_raw_content}\n"
        f"{CAR_TICKET_CLOSE}\n"
        "</CAR_CURRENT_TICKET_FILE>"
    )


def _apps_hint_block(apps_hint: str) -> str:
    if not apps_hint:
        return ""
    return f"<CAR_INSTALLED_APPS>\n{apps_hint}\n</CAR_INSTALLED_APPS>"


def render_ticket_flow_prompt(model: TicketFlowPromptModel) -> str:
    """Render the canonical ticket-flow prompt contract."""
    sections = model.sections
    optional_sections = ""
    if model.include_optional_sections:
        chunks: list[str] = [
            "<CAR_REQUESTED_CONTEXT>\n"
            f"{sections.requested_context_block}\n"
            "</CAR_REQUESTED_CONTEXT>\n\n",
        ]
        if sections.contextspace_block.strip():
            chunks.append(
                "<CAR_CONTEXTSPACE_DOCS>\n"
                f"{sections.contextspace_block}\n"
                "</CAR_CONTEXTSPACE_DOCS>\n\n"
            )
        chunks.append(
            "<CAR_HUMAN_REPLIES>\n"
            f"{sections.reply_block}\n"
            "</CAR_HUMAN_REPLIES>\n\n",
        )
        if sections.prev_ticket_block.strip():
            chunks.append(
                "<CAR_PREVIOUS_TICKET_REFERENCE>\n"
                f"{sections.prev_ticket_block}\n"
                "</CAR_PREVIOUS_TICKET_REFERENCE>\n\n"
            )
        optional_sections = "".join(chunks)
    previous_agent_output = ""
    if model.include_optional_sections:
        previous_agent_output = (
            "\n\n<CAR_PREVIOUS_AGENT_OUTPUT>\n"
            f"{sections.prev_block}\n"
            "</CAR_PREVIOUS_AGENT_OUTPUT>"
        )
    return (
        "<CAR_TICKET_FLOW_PROMPT>\n\n"
        "<CAR_TICKET_FLOW_INSTRUCTIONS>\n"
        f"{model.instructions}\n"
        "</CAR_TICKET_FLOW_INSTRUCTIONS>\n\n"
        "<CAR_RUNTIME_PATHS>\n"
        f"Current ticket file: {model.rel_ticket}\n"
        f"Dispatch directory: {model.rel_dispatch_dir}\n"
        f"DISPATCH.md path: {model.rel_dispatch_path}\n"
        "</CAR_RUNTIME_PATHS>\n\n"
        "<CAR_HUD>\n"
        f"{model.car_hud}\n"
        "</CAR_HUD>\n\n"
        f"{_apps_hint_block(model.apps_hint)}\n\n"
        f"{model.checkpoint_block}\n\n"
        f"{model.commit_block}\n\n"
        f"{model.lint_block}\n\n"
        f"{model.loop_guard_block}\n\n"
        f"{optional_sections}"
        f"{sections.ticket_block}\n\n"
        f"{previous_agent_output}\n\n"
        "</CAR_TICKET_FLOW_PROMPT>"
    )


def reduce_ticket_flow_prompt_to_budget(
    model: TicketFlowPromptModel, *, max_bytes: int
) -> TicketFlowPromptModel:
    """Shrink optional prompt sections without changing the prompt contract."""
    work_model = model
    sections = work_model.sections.as_dict()

    def current_model() -> TicketFlowPromptModel:
        return work_model.with_sections(TicketFlowPromptSections.from_dict(sections))

    def current_prompt() -> str:
        return render_ticket_flow_prompt(current_model())

    def shrink_rendered_sections() -> None:
        marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
        for policy in SECTION_REDUCTION_POLICY:
            key = policy.key
            if not _section_key_affects_render_size(
                key, include_optional_sections=work_model.include_optional_sections
            ):
                continue
            value = sections.get(key, "")
            if not value:
                continue
            prompt = current_prompt()
            if len(prompt.encode("utf-8")) <= max_bytes:
                break
            overflow = len(prompt.encode("utf-8")) - max_bytes
            value_bytes = len(value.encode("utf-8"))
            new_limit = max(value_bytes - overflow, 0)
            if 0 < new_limit < value_bytes:
                new_limit = max(new_limit - marker_bytes, 0)
            if new_limit == 0 and value_bytes > 0 and policy.required:
                new_limit = min(value_bytes, marker_bytes + 1)
            if policy.required:
                new_limit = max(new_limit, _minimum_section_bytes(key, value))
            sections[key] = _truncate_section(key, value, new_limit)

        while len(current_prompt().encode("utf-8")) > max_bytes:
            progressed = False
            for policy in SECTION_REDUCTION_POLICY:
                key = policy.key
                if not _section_key_affects_render_size(
                    key, include_optional_sections=work_model.include_optional_sections
                ):
                    continue
                value = sections.get(key, "")
                if not value:
                    continue
                if not policy.required:
                    sections[key] = ""
                else:
                    value_bytes = len(value.encode("utf-8"))
                    minimum_bytes = _minimum_section_bytes(key, value)
                    if value_bytes <= minimum_bytes:
                        continue
                    else:
                        sections[key] = _truncate_section(
                            key, value, max(value_bytes // 2, minimum_bytes)
                        )
                progressed = True
                break
            if not progressed:
                break

    if len(current_prompt().encode("utf-8")) <= max_bytes:
        return current_model()

    shrink_rendered_sections()
    if len(current_prompt().encode("utf-8")) <= max_bytes:
        return current_model()

    work_model = work_model.with_instructions(COMPACT_TICKET_FLOW_INSTRUCTIONS)
    work_model = work_model.without_optional_static_blocks()
    shrink_rendered_sections()

    while len(current_prompt().encode("utf-8")) > max_bytes:
        if work_model.checkpoint_block:
            work_model = replace(work_model, checkpoint_block="")
        elif work_model.commit_block:
            work_model = replace(work_model, commit_block="")
        elif work_model.lint_block:
            work_model = replace(work_model, lint_block="")
        elif work_model.loop_guard_block:
            work_model = replace(work_model, loop_guard_block="")
        else:
            break

    return current_model()


def validate_ticket_flow_prompt(prompt: str, *, max_bytes: int) -> None:
    """Assert the rendered prompt preserves the ticket-flow prompt contract."""
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > max_bytes:
        raise ValueError(
            f"ticket-flow prompt exceeds budget: {prompt_bytes} > {max_bytes}"
        )
    missing = [marker for marker in REQUIRED_PROMPT_MARKERS if marker not in prompt]
    if missing:
        raise ValueError(
            "ticket-flow prompt missing required marker(s): " + ", ".join(missing)
        )
