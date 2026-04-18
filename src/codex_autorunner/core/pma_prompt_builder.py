from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, TypedDict

from .pma_context_shared import PMA_MAX_MESSAGES, PMA_MAX_REPOS, PMA_MAX_TEXT
from .pma_prompt_state import (
    PMA_PROMPT_SECTION_ORDER,
    _digest_preview,
    _digest_text,
)

PMA_PROMPT_SECTION_META: dict[str, dict[str, str]] = {
    "prompt": {"label": "PMA_PROMPT_MD", "tag": "PMA_PROMPT_MD"},
    "discoverability": {
        "label": "PMA_DISCOVERABILITY",
        "tag": "PMA_DISCOVERABILITY",
    },
    "fastpath": {"label": "PMA_FASTPATH", "tag": "PMA_FASTPATH"},
    "agents": {"label": "AGENTS_MD", "tag": "AGENTS_MD"},
    "active_context": {
        "label": "ACTIVE_CONTEXT_MD",
        "tag": "ACTIVE_CONTEXT_MD",
    },
    "context_log_tail": {
        "label": "CONTEXT_LOG_TAIL_MD",
        "tag": "CONTEXT_LOG_TAIL_MD",
    },
    "hub_snapshot": {"label": "HUB_SNAPSHOT", "tag": "hub_snapshot"},
}

PMA_FASTPATH = """<pma_fastpath>
You are PMA inside Codex Autorunner (CAR). Treat the filesystem as truth; prefer creating/updating CAR artifacts over "chat-only" plans.

First-turn routine:
1) Read <user_message> and <hub_snapshot>.
2) BRANCH A - Run Dispatches (paused runs needing attention):
    - If hub_snapshot.inbox has entries (any next_action value), handle them first.
    - These are paused/blocked/dead ticket flow runs that need user attention.
    - Ticket flow requires a clean commit after each completed ticket. If a ticket is done but the repo is still dirty, or ownership of remaining changes is ambiguous, escalate to the user instead of guessing a reply.
    - next_action values indicate the type of attention needed:
      - reply_and_resume: Paused run with a dispatch question - summarize and answer it.
      - inspect_and_resume: Run state needs attention - review blocking_reason and propose action.
      - restart_worker: Worker process died - suggest force resume or diagnose crash.
      - diagnose_or_restart: Run failed or stopped - suggest diagnose or restart.
    - Always include the item.open_url so the user can jump to the repo Inbox tab.
3) BRANCH B - Managed threads vs ticket flows:
    - Managed threads are the default for straightforward work in one managed resource: exploratory work, reviews, bug fixes, focused refactors, and single-feature PRs that fit in one clear prompt.
    - If `hub_snapshot.pma_threads` has a relevant active thread, resume it instead of spawning a new one.
    - Treat `chat_bound=true` managed threads as continuity artifacts protected from cleanup by default. Broad requests like "clean up workspace" do not authorize archiving or removing them; only explicit user direction does.
    - For hub-scoped PMA CLI commands, include `--path <hub_root>` so they resolve the intended hub config instead of relying on the current working directory.
    - Do not launch runtime CLIs directly (`codex`, `opencode`, `zeroclaw`, etc.) for PMA-managed work when a managed thread fits. Use CAR managed threads so lifecycle, progress, subscriptions, and wake-ups stay visible to PMA.
    - If no suitable thread exists, spawn one, run work, and keep it compact:
      - `car pma thread spawn --agent codex --repo <repo_id> --name <label> --path <hub_root>`
      - `car pma thread spawn --resource-kind agent_workspace --resource-id <workspace_id> --name <label> --path <hub_root>`
      - `car pma thread send --id <managed_thread_id> --message "..." --path <hub_root>`
      - `car pma thread send --id <managed_thread_id> --message-file prompt.md --path <hub_root>`
      - `car pma thread send --id <managed_thread_id> --message "..." --watch --path <hub_root>` only when you intentionally want synchronous foreground babysitting
      - `car pma thread status --id <managed_thread_id> --path <hub_root>`
      - `car pma thread compact --id <id> --summary "..." --path <hub_root>`
      - `car pma thread archive --id <id> --path <hub_root>`
    - Do not write ticket files as scaffolding for managed-thread work. If the task fits in one clear prompt, stay in a managed thread.
    - Use tickets/ticket_flow when the work needs ordered multi-step structure: cross-repo changes, 3+ planned tickets, explicit acceptance-criteria tracking, or pause/resume/review handoffs.
4) BRANCH C - PMA File Inbox (fresh uploads vs stale leftovers):
    - If PMA File Inbox shows next_action="process_uploaded_file" and hub_snapshot.inbox is empty:
      - Inspect files in `.codex-autorunner/filebox/inbox/` (read their contents).
      - Classify each upload: ticket pack (TICKET-*.md), docs (*.md), code (*.py/*.ts/*.js), assets (images/pdfs).
      - For each file, determine the target repo/worktree based on:
        - File content hints (repo_id mentions, worktree paths)
        - Filename patterns matching known repos
      - Propose or execute the minimal CAR-native action per file:
        - Ticket packs: copy to `<repo_root>/.codex-autorunner/tickets/` and run `car hub tickets setup-pack`
        - Docs: integrate into contextspace (`active_context.md`, `spec.md`, `decisions.md`)
        - Code: identify target worktree, propose handoff or direct edit
      - Assets: suggest destination (repo docs, archive)
    - If PMA File Inbox shows next_action="review_stale_uploaded_file":
      - Treat the file as a likely leftover, not urgent new work.
      - First verify whether it was already handled by checking the user request, recent PMA history, and nearby outbox/repo activity.
      - If it was already consumed or is no longer relevant, move it out of the active inbox with `car pma file consume <filename> --path <hub_root>` or `car pma file dismiss <filename> --path <hub_root>`.
      - Only route it like a fresh upload when evidence says it is still pending work.
    - Only ask the user "which file first?" or "which repo?" when routing is truly ambiguous.
5) BRANCH D - Automation continuity (subscriptions + timers):
    - If work should continue without manual polling, use PMA automation primitives.
    - Subscriptions:
      - Create/list/delete via `/hub/pma/subscriptions`.
      - Common event_types:
        - ticket flow: `flow_paused`, `flow_completed`, `flow_failed`, `flow_stopped`
        - managed thread: `managed_thread_completed`, `managed_thread_failed`
    - Timers:
      - one-shot (`timer_type=one_shot`, `delay_seconds`)
      - watchdog (`timer_type=watchdog`, `idle_seconds`; touch/cancel as progress changes)
      - Endpoints: `/hub/pma/timers`, `/hub/pma/timers/{timer_id}/touch`, `/hub/pma/timers/{timer_id}/cancel`
    - Prefer idempotency keys and lane-specific routing (`lane_id`) for chainable plans.
    - Consult `.codex-autorunner/pma/docs/ABOUT_CAR.md` section "PMA automation wake-ups" for recipes.
6) If the request is new work (not inbox/file processing):
    - Identify the target managed resource(s): repo(s) and/or agent workspace(s).
    - Prefer hub-owned worktrees for changes.
    - Prefer one-shot setup/repair commands: `car hub tickets setup-pack`, `car hub tickets fmt`, `car hub tickets doctor --fix`.
    - Only create/adjust repo tickets under each repo's `.codex-autorunner/tickets/` when you have already decided the work should run as ticket_flow.

Web UI map (user perspective):
- Hub root: `/` (repos list + global notifications).
- Repo view: `/repos/<repo_id>/` tabs: Tickets | Inbox | Contextspace | Terminal | Analytics | Archive.
  - Tickets: edit queue; Inbox: paused run dispatches; Contextspace: active_context/spec/decisions.

Ticket planning constraints (state machine):
- Ticket flow processes `.codex-autorunner/tickets/TICKET-###*.md` in ascending numeric order.
- On each turn it picks the first ticket where `done != true`; when that ticket is completed, it advances to the next.
- `depends_on` frontmatter is ignored by runtime ordering; filename order remains the execution contract.
- If prerequisites are discovered late, reorder/split tickets so prerequisite work appears earlier.

What each ticket agent turn can already see:
- The current ticket file (full markdown + frontmatter).
- Pinned contextspace docs when present: `active_context.md`, `decisions.md`, `spec.md` (truncated).
- Reply context from prior user dispatches and prior agent output (if present).
</pma_fastpath>
"""


class PromptSection(TypedDict):
    label: str
    tag: str
    content: str
    digest: str


@dataclass(frozen=True)
class PmaPromptRenderLimits:
    max_repos: int = PMA_MAX_REPOS
    max_messages: int = PMA_MAX_MESSAGES
    max_text_chars: int = PMA_MAX_TEXT

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "PmaPromptRenderLimits":
        limits = snapshot.get("limits") if isinstance(snapshot, Mapping) else None
        limits_map = limits if isinstance(limits, Mapping) else {}
        return cls(
            max_repos=int(limits_map.get("max_repos", PMA_MAX_REPOS)),
            max_messages=int(limits_map.get("max_messages", PMA_MAX_MESSAGES)),
            max_text_chars=int(limits_map.get("max_text_chars", PMA_MAX_TEXT)),
        )


def render_pma_discoverability_preamble(
    pma_docs: Optional[Mapping[str, Any]] = None,
) -> str:
    prompt = (
        "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`.\n"
        "Durable guidance: `.codex-autorunner/pma/docs/AGENTS.md`.\n"
        "Working context: `.codex-autorunner/pma/docs/active_context.md`.\n"
        "History: `.codex-autorunner/pma/docs/context_log.md`.\n"
        "Automation quickstart: `/hub/pma/subscriptions` (event triggers) and `/hub/pma/timers` (one-shot/watchdog).\n"
        'Automation recipes: `.codex-autorunner/pma/docs/ABOUT_CAR.md` -> "PMA automation wake-ups".\n'
        "Ticket templates: pass `--repo <path>` (a git worktree) for every `car templates` subcommand; add `--path <hub_root>` when the shell cwd is not inside the hub tree. Examples: `car templates list --repo <path>`, `car templates search <query> --repo <path>`, `car templates show <id> --repo <path>`, `car templates apply <id> --repo <path>`.\n"
        "To send a file to the user, write it to `.codex-autorunner/filebox/outbox/`.\n"
        "User uploaded files are in `.codex-autorunner/filebox/inbox/`.\n\n"
    )
    if pma_docs:
        prompt += _render_pma_workspace_docs(pma_docs)
    return prompt


def _render_pma_workspace_docs(resolved_docs: Mapping[str, Any]) -> str:
    max_lines = resolved_docs.get("active_context_max_lines")
    line_count = resolved_docs.get("active_context_line_count")
    auto_prune = resolved_docs.get("active_context_auto_prune") or {}
    auto_pruned_at = auto_prune.get("last_auto_pruned_at")
    auto_pruned_before = auto_prune.get("line_count_before")
    auto_pruned_budget = auto_prune.get("line_budget")
    return (
        "<pma_workspace_docs>\n"
        "<AGENTS_MD>\n"
        f"{resolved_docs.get('agents', '')}\n"
        "</AGENTS_MD>\n"
        "<ACTIVE_CONTEXT_MD>\n"
        f"{resolved_docs.get('active_context', '')}\n"
        "</ACTIVE_CONTEXT_MD>\n"
        f"<ACTIVE_CONTEXT_BUDGET lines='{max_lines}' current_lines='{line_count}' />\n"
        f"<ACTIVE_CONTEXT_AUTO_PRUNE last_at='{auto_pruned_at}' line_count_before='{auto_pruned_before}' line_budget='{auto_pruned_budget}' triggered_now='{str(bool(resolved_docs.get('active_context_auto_pruned'))).lower()}' />\n"
        "<CONTEXT_LOG_TAIL_MD>\n"
        f"{resolved_docs.get('context_log_tail', '')}\n"
        "</CONTEXT_LOG_TAIL_MD>\n"
        "</pma_workspace_docs>\n\n"
    )


def _build_prompt_sections(
    *,
    base_prompt: str,
    discoverability_text: str,
    pma_docs: Optional[Mapping[str, Any]],
    snapshot_text: str,
) -> dict[str, PromptSection]:
    sections: dict[str, PromptSection] = {
        "prompt": {
            "label": PMA_PROMPT_SECTION_META["prompt"]["label"],
            "tag": PMA_PROMPT_SECTION_META["prompt"]["tag"],
            "content": base_prompt,
            "digest": "",
        },
        "discoverability": {
            "label": PMA_PROMPT_SECTION_META["discoverability"]["label"],
            "tag": PMA_PROMPT_SECTION_META["discoverability"]["tag"],
            "content": discoverability_text,
            "digest": "",
        },
        "fastpath": {
            "label": PMA_PROMPT_SECTION_META["fastpath"]["label"],
            "tag": PMA_PROMPT_SECTION_META["fastpath"]["tag"],
            "content": PMA_FASTPATH,
            "digest": "",
        },
        "agents": {
            "label": PMA_PROMPT_SECTION_META["agents"]["label"],
            "tag": PMA_PROMPT_SECTION_META["agents"]["tag"],
            "content": str((pma_docs or {}).get("agents") or ""),
            "digest": "",
        },
        "active_context": {
            "label": PMA_PROMPT_SECTION_META["active_context"]["label"],
            "tag": PMA_PROMPT_SECTION_META["active_context"]["tag"],
            "content": str((pma_docs or {}).get("active_context") or ""),
            "digest": "",
        },
        "context_log_tail": {
            "label": PMA_PROMPT_SECTION_META["context_log_tail"]["label"],
            "tag": PMA_PROMPT_SECTION_META["context_log_tail"]["tag"],
            "content": str((pma_docs or {}).get("context_log_tail") or ""),
            "digest": "",
        },
        "hub_snapshot": {
            "label": PMA_PROMPT_SECTION_META["hub_snapshot"]["label"],
            "tag": PMA_PROMPT_SECTION_META["hub_snapshot"]["tag"],
            "content": snapshot_text,
            "digest": "",
        },
    }
    for payload in sections.values():
        payload["digest"] = _digest_text(payload.get("content") or "")
    return sections


def _render_pma_actionable_state(
    snapshot: Mapping[str, Any],
    *,
    render_hub_snapshot: Any,
    limits: PmaPromptRenderLimits,
) -> str:
    actionable_snapshot: dict[str, Any] = {}
    if snapshot.get("availability") is not None:
        actionable_snapshot["availability"] = snapshot.get("availability")
    if snapshot.get("generated_at") is not None:
        actionable_snapshot["generated_at"] = snapshot.get("generated_at")
    if snapshot.get("freshness") is not None:
        actionable_snapshot["freshness"] = snapshot.get("freshness")

    action_queue = snapshot.get("action_queue") or []
    if action_queue:
        actionable_snapshot["action_queue"] = action_queue
    else:
        for key in ("inbox", "pma_threads", "pma_files_detail", "automation"):
            value = snapshot.get(key)
            if value:
                actionable_snapshot[key] = value

    rendered = render_hub_snapshot(
        actionable_snapshot,
        max_repos=limits.max_repos,
        max_messages=limits.max_messages,
        max_text_chars=limits.max_text_chars,
    ).strip()
    return rendered or "No current PMA actions."


def _render_prompt_delta_header(
    *,
    sections: Mapping[str, Mapping[str, str]],
    prior_sections: Optional[Mapping[str, Any]],
    prompt_state_key: str,
    current_mode: str,
    reason: str,
    prior_updated_at: Optional[str],
) -> str:
    def _section_label(name: str) -> str:
        section = sections.get(name) or {}
        return str(section.get("label") or name)

    def _format_section_list(labels: Sequence[str]) -> str:
        filtered = [
            label for label in labels if isinstance(label, str) and label.strip()
        ]
        return ", ".join(filtered) if filtered else "none"

    attrs = [
        f"mode='{current_mode}'",
        f"reason='{reason}'",
        f"state_key='{prompt_state_key}'",
    ]
    if prior_updated_at:
        attrs.append(f"prior_updated_at='{prior_updated_at}'")
    lines = [f"<what_changed_since_last_turn {' '.join(attrs)}>"]
    prior_section_map = prior_sections if isinstance(prior_sections, Mapping) else {}
    statuses_by_name: dict[str, str] = {}

    for name in PMA_PROMPT_SECTION_ORDER:
        section = sections.get(name) or {}
        current_digest = str(section.get("digest") or "")
        previous = prior_section_map.get(name)
        previous_digest = (
            str(previous.get("digest") or "") if isinstance(previous, Mapping) else ""
        )
        if current_mode == "full" and not previous_digest:
            status = "first_turn"
        elif current_mode == "full":
            status = "full_refresh"
        elif previous_digest and previous_digest == current_digest:
            status = "unchanged"
        elif previous_digest:
            status = "changed"
        else:
            status = "new"
        statuses_by_name[name] = status

    if current_mode == "delta":
        unchanged_labels = [
            _section_label(name)
            for name in PMA_PROMPT_SECTION_ORDER
            if statuses_by_name.get(name) == "unchanged"
        ]
        changed_labels = [
            _section_label(name)
            for name in PMA_PROMPT_SECTION_ORDER
            if statuses_by_name.get(name) in {"changed", "new"}
        ]
        lines.append(f"- cached={_format_section_list(unchanged_labels)}")
        changed_line = f"- changed={_format_section_list(changed_labels)}"
        if changed_labels == [_section_label("hub_snapshot")]:
            changed_line += " (see <current_actionable_state>)"
        lines.append(changed_line)
    else:
        full_context_labels = [
            _section_label(name) for name in PMA_PROMPT_SECTION_ORDER
        ]
        lines.append(f"- full_context={_format_section_list(full_context_labels)}")

    for name in PMA_PROMPT_SECTION_ORDER:
        status = statuses_by_name.get(name, "")
        section = sections.get(name) or {}
        if (
            current_mode == "delta"
            and status in {"changed", "new"}
            and name != "hub_snapshot"
        ):
            tag = section.get("tag") or str(name)
            lines.append(f"<{tag}>")
            lines.append(str(section.get("content") or ""))
            lines.append(f"</{tag}>")
    lines.append("</what_changed_since_last_turn>")
    return "\n".join(lines) + "\n\n"


def render_pma_prompt(
    *,
    base_prompt: str,
    message: str,
    snapshot_text: str,
    actionable_state_text: str,
    discoverability_text: str,
    pma_docs: Optional[Mapping[str, Any]],
    sections: Mapping[str, Mapping[str, str]],
    prompt_state_key: Optional[str],
    use_delta: bool,
    delta_reason: str,
    prior_sections: Optional[Mapping[str, Any]],
    prior_updated_at: Optional[str],
) -> str:
    prompt = f"{base_prompt}\n\n" if base_prompt else ""
    if not use_delta:
        prompt += discoverability_text
    if not use_delta and pma_docs:
        prompt += _render_pma_workspace_docs(pma_docs)
    if not use_delta:
        prompt += f"{PMA_FASTPATH}\n\n"
    if prompt_state_key:
        prompt += _render_prompt_delta_header(
            sections=sections,
            prior_sections=prior_sections,
            prompt_state_key=prompt_state_key,
            current_mode="delta" if use_delta else "full",
            reason=delta_reason,
            prior_updated_at=prior_updated_at,
        )
    prompt += (
        "<current_actionable_state>\n"
        f"{actionable_state_text}\n"
        "</current_actionable_state>\n\n"
    )
    if not use_delta:
        prompt += f"<hub_snapshot>\n{snapshot_text}\n</hub_snapshot>\n\n"
    elif prompt_state_key:
        prompt += (
            "<hub_snapshot_ref "
            f"digest='{_digest_preview(str((sections.get('hub_snapshot') or {}).get('digest') or ''))}' "
            f"state_key='{prompt_state_key}' />\n\n"
        )
    prompt += f"<user_message>\n{message}\n</user_message>\n"
    return prompt
