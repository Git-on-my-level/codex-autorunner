from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..bootstrap import ensure_pma_docs, pma_doc_path
from .config import load_hub_config
from .config_contract import ConfigError
from .filebox import BOXES, empty_listing, list_filebox
from .flows.models import flow_run_duration_seconds
from .freshness import (
    build_freshness_payload,
    iso_now,
    resolve_stale_threshold_seconds,
    summarize_section_freshness,
)
from .hub import HubSupervisor
from .pma_active_context import (
    PMA_ACTIVE_CONTEXT_MAX_LINES,
    get_active_context_auto_prune_meta,
    maybe_auto_prune_active_context,
)
from .pma_automation_snapshot import snapshot_pma_automation as _snapshot_pma_automation
from .pma_file_inbox import (
    PMA_FILE_NEXT_ACTION_PROCESS,
    PMA_FILE_NEXT_ACTION_REVIEW_STALE,  # noqa: F401
    _extract_entry_freshness,
    _timestamp_sort_value,  # noqa: F401
    classify_pma_file_inbox_entry,  # noqa: F401
    enrich_pma_file_inbox_entry,
)
from .pma_prompt_state import (
    PMA_PROMPT_SECTION_ORDER,
    _digest_preview,
    _digest_text,
    _merge_prompt_session_state,
)
from .pma_thread_snapshot import snapshot_pma_threads as _snapshot_pma_threads
from .state_roots import resolve_hub_templates_root
from .ticket_flow_projection import build_canonical_state_v1
from .ticket_flow_summary import build_ticket_flow_summary

_logger = logging.getLogger(__name__)

PMA_MAX_REPOS = 25
PMA_MAX_MESSAGES = 10
PMA_MAX_TEXT = 800
PMA_MAX_TEMPLATE_REPOS = 25
PMA_MAX_TEMPLATE_FIELD_CHARS = 120
PMA_MAX_PMA_FILES = 50
PMA_MAX_LIFECYCLE_EVENTS = 20
PMA_MAX_PMA_THREADS = 20
PMA_MAX_AUTOMATION_ITEMS = 10

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
    - If request is exploratory/review/debug/quick-fix work in one managed resource, prefer managed threads.
    - If `hub_snapshot.pma_threads` has a relevant active thread, resume it instead of spawning a new one.
    - Treat `chat_bound=true` managed threads as continuity artifacts protected from cleanup by default. Broad requests like "clean up workspace" do not authorize archiving or removing them; only explicit user direction does.
    - For hub-scoped PMA CLI commands, include `--path <hub_root>` so they resolve the intended hub config instead of relying on the current working directory.
    - If no suitable thread exists, spawn one, run work, and keep it compact:
      - `car pma thread spawn --agent codex --repo <repo_id> --name <label> --path <hub_root>`
      - `car pma thread spawn --resource-kind agent_workspace --resource-id <workspace_id> --name <label> --path <hub_root>`
      - `car pma thread send --id <managed_thread_id> --message "..." --watch --path <hub_root>`
      - `car pma thread send --id <managed_thread_id> --message-file prompt.md --watch --path <hub_root>`
      - `car pma thread send --id <managed_thread_id> --message "..." --notify-on terminal --notify-lane <lane_id> --path <hub_root>`
      - `car pma thread status --id <managed_thread_id> --path <hub_root>`
      - `car pma thread compact --id <id> --summary "..." --path <hub_root>`
      - `car pma thread archive --id <id> --path <hub_root>`
    - If request is a multi-step deliverable or cross-repo change, prefer tickets/ticket_flow.
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
    - Create/adjust repo tickets under each repo's `.codex-autorunner/tickets/` when the target resource is repo-backed.

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

PMA_DOCS_MAX_CHARS = 12_000
PMA_CONTEXT_LOG_TAIL_LINES = 120


def _tail_lines(text: str, max_lines: int) -> str:
    if max_lines <= 0:
        return ""
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[-max_lines:])


def _truncate(text: Optional[str], limit: int) -> str:
    raw = text or ""
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 3)] + "..."


def _trim_extra(extra: Any, limit: int) -> Any:
    if extra is None:
        return None
    if isinstance(extra, str):
        return _truncate(extra, limit)
    try:
        raw = json.dumps(extra, ensure_ascii=True, sort_keys=True, default=str)
    except (ValueError, TypeError):
        raw = str(extra)
    if len(raw) <= limit:
        return extra
    return {
        "_omitted": True,
        "note": "extra omitted due to size",
        "preview": _truncate(raw, limit),
    }


def load_pma_workspace_docs(hub_root: Path) -> dict[str, Any]:
    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        _logger.warning("Could not ensure PMA docs: %s", exc)

    docs_max_chars = PMA_DOCS_MAX_CHARS
    active_context_max_lines = PMA_ACTIVE_CONTEXT_MAX_LINES
    context_log_tail_lines = PMA_CONTEXT_LOG_TAIL_LINES
    try:
        hub_config = load_hub_config(hub_root)
        pma_cfg = getattr(hub_config, "pma", None)
        if pma_cfg is not None:
            docs_max_chars = int(getattr(pma_cfg, "docs_max_chars", docs_max_chars))
            active_context_max_lines = int(
                getattr(pma_cfg, "active_context_max_lines", active_context_max_lines)
            )
            context_log_tail_lines = int(
                getattr(pma_cfg, "context_log_tail_lines", context_log_tail_lines)
            )
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _logger.warning("Could not load PMA config: %s", exc)

    auto_prune_state = maybe_auto_prune_active_context(
        hub_root,
        max_lines=active_context_max_lines,
    )
    auto_prune_meta = get_active_context_auto_prune_meta(hub_root)

    agents_path = pma_doc_path(hub_root, "AGENTS.md")
    active_context_path = pma_doc_path(hub_root, "active_context.md")
    context_log_path = pma_doc_path(hub_root, "context_log.md")

    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.warning("Could not read file %s: %s", path, exc)
            return ""

    agents = _truncate(_read(agents_path), docs_max_chars)
    active_context = _read(active_context_path)
    active_context_lines = len((active_context or "").splitlines())
    active_context = _truncate(active_context, docs_max_chars)
    context_log_tail = _tail_lines(_read(context_log_path), context_log_tail_lines)
    context_log_tail = _truncate(context_log_tail, docs_max_chars)

    return {
        "agents": agents,
        "active_context": active_context,
        "active_context_line_count": active_context_lines,
        "active_context_max_lines": active_context_max_lines,
        "context_log_tail": context_log_tail,
        "active_context_auto_pruned": bool(auto_prune_state),
        "active_context_auto_prune": auto_prune_meta,
    }


def _load_template_scan_summary(
    hub_root: Optional[Path],
    *,
    max_field_chars: int = PMA_MAX_TEMPLATE_FIELD_CHARS,
) -> Optional[dict[str, Any]]:
    if hub_root is None:
        return None
    try:
        scans_root = resolve_hub_templates_root(hub_root) / "scans"
        if not scans_root.exists():
            return None
        candidates = [
            entry
            for entry in scans_root.iterdir()
            if entry.is_file() and entry.suffix == ".json"
        ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda entry: entry.stat().st_mtime)
        payload = json.loads(newest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return {
            "repo_id": _truncate(str(payload.get("repo_id", "")), max_field_chars),
            "decision": _truncate(str(payload.get("decision", "")), max_field_chars),
            "severity": _truncate(str(payload.get("severity", "")), max_field_chars),
            "scanned_at": _truncate(
                str(payload.get("scanned_at", "")), max_field_chars
            ),
        }
    except (OSError, ValueError, TypeError, KeyError) as exc:
        _logger.warning("Could not load template scan summary: %s", exc)
        return None


def _snapshot_pma_files(
    hub_root: Path,
) -> tuple[dict[str, list[str]], dict[str, list[dict[str, Any]]]]:
    pma_files: dict[str, list[str]] = {box: [] for box in BOXES}
    pma_files_detail: dict[str, list[dict[str, Any]]] = empty_listing()
    try:
        filebox = list_filebox(hub_root)
        for box in BOXES:
            entries = filebox.get(box) or []
            names = sorted([e.name for e in entries])
            pma_files[box] = names
            pma_files_detail[box] = [
                (
                    enrich_pma_file_inbox_entry(
                        {
                            "item_type": "pma_file",
                            "next_action": PMA_FILE_NEXT_ACTION_PROCESS,
                            "box": box,
                            "name": e.name,
                            "source": e.source or "filebox",
                            "size": str(e.size) if e.size is not None else "",
                            "modified_at": e.modified_at or "",
                        }
                    )
                    if box == "inbox"
                    else {
                        "item_type": "pma_file",
                        "box": box,
                        "name": e.name,
                        "source": e.source or "filebox",
                        "size": str(e.size) if e.size is not None else "",
                        "modified_at": e.modified_at or "",
                    }
                )
                for e in entries
            ]
    except (OSError, KeyError, TypeError, RuntimeError) as exc:
        _logger.warning("Could not list filebox contents: %s", exc)
    return pma_files, pma_files_detail


def _build_templates_snapshot(
    supervisor: HubSupervisor,
    *,
    hub_root: Optional[Path] = None,
    max_repos: int = PMA_MAX_TEMPLATE_REPOS,
    max_field_chars: int = PMA_MAX_TEMPLATE_FIELD_CHARS,
) -> dict[str, Any]:
    hub_config = getattr(supervisor, "hub_config", None)
    templates_cfg = getattr(hub_config, "templates", None)
    if templates_cfg is None:
        return {"enabled": False, "repos": []}
    repos = []
    for repo in templates_cfg.repos[: max(0, max_repos)]:
        repos.append(
            {
                "id": _truncate(repo.id, max_field_chars),
                "default_ref": _truncate(repo.default_ref, max_field_chars),
                "trusted": bool(repo.trusted),
            }
        )
    payload: dict[str, Any] = {
        "enabled": bool(templates_cfg.enabled),
        "repos": repos,
    }
    scan_summary = _load_template_scan_summary(
        hub_root, max_field_chars=max_field_chars
    )
    if scan_summary:
        payload["last_scan"] = scan_summary
    return payload


def _resolve_pma_freshness_threshold_seconds(
    supervisor: Optional[HubSupervisor],
) -> int:
    pma_config = getattr(getattr(supervisor, "hub_config", None), "pma", None)
    return resolve_stale_threshold_seconds(
        getattr(pma_config, "freshness_stale_threshold_seconds", None)
    )


def _build_snapshot_freshness_summary(
    *,
    generated_at: str,
    stale_threshold_seconds: int,
    repos: list[dict[str, Any]],
    agent_workspaces: list[dict[str, Any]],
    inbox: list[dict[str, Any]],
    action_queue: list[dict[str, Any]],
    pma_threads: list[dict[str, Any]],
    pma_files_detail: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "stale_threshold_seconds": stale_threshold_seconds,
        "sections": {
            "repos": summarize_section_freshness(
                repos,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                extractor=_extract_entry_freshness,
            ),
            "agent_workspaces": summarize_section_freshness(
                agent_workspaces,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
            ),
            "inbox": summarize_section_freshness(
                inbox,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                extractor=_extract_entry_freshness,
            ),
            "action_queue": summarize_section_freshness(
                action_queue,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                extractor=_extract_entry_freshness,
            ),
            "pma_threads": summarize_section_freshness(
                pma_threads,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
            ),
            "pma_file_inbox": summarize_section_freshness(
                pma_files_detail.get("inbox") or [],
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
            ),
            "pma_file_outbox": summarize_section_freshness(
                pma_files_detail.get("outbox") or [],
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
            ),
        },
    }


def load_pma_prompt(hub_root: Path) -> str:
    path = pma_doc_path(hub_root, "prompt.md")
    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        _logger.warning("Could not ensure PMA docs for prompt: %s", exc)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.warning("Could not read prompt file: %s", exc)
        return ""


def format_pma_discoverability_preamble(
    *,
    hub_root: Optional[Path] = None,
    pma_docs: Optional[dict[str, Any]] = None,
) -> str:
    prompt = (
        "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`.\n"
        "Durable guidance: `.codex-autorunner/pma/docs/AGENTS.md`.\n"
        "Working context: `.codex-autorunner/pma/docs/active_context.md`.\n"
        "History: `.codex-autorunner/pma/docs/context_log.md`.\n"
        "Automation quickstart: `/hub/pma/subscriptions` (event triggers) and `/hub/pma/timers` (one-shot/watchdog).\n"
        'Automation recipes: `.codex-autorunner/pma/docs/ABOUT_CAR.md` -> "PMA automation wake-ups".\n'
        "To send a file to the user, write it to `.codex-autorunner/filebox/outbox/`.\n"
        "User uploaded files are in `.codex-autorunner/filebox/inbox/`.\n\n"
    )

    resolved_docs = pma_docs
    if resolved_docs is None and hub_root is not None:
        try:
            resolved_docs = load_pma_workspace_docs(hub_root)
        except (OSError, ValueError, TypeError, RuntimeError, ConfigError) as exc:
            _logger.warning("Could not load PMA workspace docs: %s", exc)
    if resolved_docs:
        prompt += _render_pma_workspace_docs(resolved_docs)
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
) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {
        "prompt": {
            "label": PMA_PROMPT_SECTION_META["prompt"]["label"],
            "tag": PMA_PROMPT_SECTION_META["prompt"]["tag"],
            "content": base_prompt,
        },
        "discoverability": {
            "label": PMA_PROMPT_SECTION_META["discoverability"]["label"],
            "tag": PMA_PROMPT_SECTION_META["discoverability"]["tag"],
            "content": discoverability_text,
        },
        "fastpath": {
            "label": PMA_PROMPT_SECTION_META["fastpath"]["label"],
            "tag": PMA_PROMPT_SECTION_META["fastpath"]["tag"],
            "content": PMA_FASTPATH,
        },
        "agents": {
            "label": PMA_PROMPT_SECTION_META["agents"]["label"],
            "tag": PMA_PROMPT_SECTION_META["agents"]["tag"],
            "content": str((pma_docs or {}).get("agents") or ""),
        },
        "active_context": {
            "label": PMA_PROMPT_SECTION_META["active_context"]["label"],
            "tag": PMA_PROMPT_SECTION_META["active_context"]["tag"],
            "content": str((pma_docs or {}).get("active_context") or ""),
        },
        "context_log_tail": {
            "label": PMA_PROMPT_SECTION_META["context_log_tail"]["label"],
            "tag": PMA_PROMPT_SECTION_META["context_log_tail"]["tag"],
            "content": str((pma_docs or {}).get("context_log_tail") or ""),
        },
        "hub_snapshot": {
            "label": PMA_PROMPT_SECTION_META["hub_snapshot"]["label"],
            "tag": PMA_PROMPT_SECTION_META["hub_snapshot"]["tag"],
            "content": snapshot_text,
        },
    }
    for payload in sections.values():
        payload["digest"] = _digest_text(payload.get("content") or "")
    return sections


def _render_pma_actionable_state(
    snapshot: Mapping[str, Any],
    *,
    max_repos: int,
    max_messages: int,
    max_text_chars: int,
) -> str:
    actionable_snapshot: dict[str, Any] = {}
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

    rendered = _render_hub_snapshot(
        actionable_snapshot,
        max_repos=max_repos,
        max_messages=max_messages,
        max_text_chars=max_text_chars,
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


def format_pma_prompt(
    base_prompt: str,
    snapshot: dict[str, Any],
    message: str,
    hub_root: Optional[Path] = None,
    *,
    prompt_state_key: Optional[str] = None,
    force_full_context: bool = False,
) -> str:
    limits = snapshot.get("limits") or {}
    max_repos = limits.get("max_repos", PMA_MAX_REPOS)
    max_messages = limits.get("max_messages", PMA_MAX_MESSAGES)
    max_text_chars = limits.get("max_text_chars", PMA_MAX_TEXT)
    snapshot_text = _render_hub_snapshot(
        snapshot,
        max_repos=max_repos,
        max_messages=max_messages,
        max_text_chars=max_text_chars,
    )
    actionable_state_text = _render_pma_actionable_state(
        snapshot,
        max_repos=max_repos,
        max_messages=max_messages,
        max_text_chars=max_text_chars,
    )
    discoverability_text = format_pma_discoverability_preamble(hub_root=None)
    pma_docs: Optional[dict[str, Any]] = None
    if hub_root is not None:
        try:
            pma_docs = load_pma_workspace_docs(hub_root)
        except (OSError, ValueError, TypeError, RuntimeError, ConfigError) as exc:
            _logger.warning("Could not load PMA workspace docs: %s", exc)

    sections = _build_prompt_sections(
        base_prompt=base_prompt,
        discoverability_text=discoverability_text,
        pma_docs=pma_docs,
        snapshot_text=snapshot_text,
    )
    use_delta = False
    delta_reason = "state_key_missing"
    prior_sections: Optional[Mapping[str, Any]] = None
    prior_updated_at: Optional[str] = None

    if hub_root is not None and prompt_state_key:
        (
            use_delta,
            delta_reason,
            prior_sections,
            prior_updated_at,
        ) = _merge_prompt_session_state(
            hub_root,
            prompt_state_key=prompt_state_key,
            sections=sections,
            force_full_context=force_full_context,
        )

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


def _gather_lifecycle_events(
    supervisor: HubSupervisor, limit: int = 20
) -> list[dict[str, Any]]:
    events = supervisor.lifecycle_store.get_unprocessed(limit=limit)
    result: list[dict[str, Any]] = []
    for event in events[:limit]:
        result.append(
            {
                "event_type": event.event_type.value,
                "repo_id": event.repo_id,
                "run_id": event.run_id,
                "timestamp": event.timestamp,
                "data": event.data,
            }
        )
    return result


async def build_hub_snapshot(
    supervisor: Optional[HubSupervisor],
    hub_root: Optional[Path] = None,
) -> dict[str, Any]:
    generated_at = iso_now()
    stale_threshold_seconds = _resolve_pma_freshness_threshold_seconds(supervisor)
    if supervisor is None:
        return {
            "generated_at": generated_at,
            "repos": [],
            "agent_workspaces": [],
            "inbox": [],
            "action_queue": [],
            "templates": {"enabled": False, "repos": []},
            "lifecycle_events": [],
            "pma_files_detail": empty_listing(),
            "pma_threads": [],
            "automation": {
                "subscriptions": {"active_count": 0, "sample": []},
                "timers": {"pending_count": 0, "sample": []},
                "wakeups": {
                    "pending_count": 0,
                    "dispatched_recent_count": 0,
                    "pending_sample": [],
                },
            },
            "freshness": _build_snapshot_freshness_summary(
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                repos=[],
                agent_workspaces=[],
                inbox=[],
                action_queue=[],
                pma_threads=[],
                pma_files_detail=empty_listing(),
            ),
        }

    snapshots = await asyncio.to_thread(supervisor.list_repos)
    snapshots = sorted(snapshots, key=lambda snap: snap.id)
    list_agent_workspaces = getattr(supervisor, "list_agent_workspaces", None)
    if callable(list_agent_workspaces):
        agent_workspace_snapshots = await asyncio.to_thread(list_agent_workspaces)
    else:
        agent_workspace_snapshots = []
    agent_workspace_snapshots = sorted(
        agent_workspace_snapshots, key=lambda snap: snap.id
    )
    pma_config = supervisor.hub_config.pma if supervisor else None
    max_repos = (
        pma_config.max_repos
        if pma_config and pma_config.max_repos > 0
        else PMA_MAX_REPOS
    )
    max_messages = (
        pma_config.max_messages
        if pma_config and pma_config.max_messages > 0
        else PMA_MAX_MESSAGES
    )
    max_text_chars = (
        pma_config.max_text_chars
        if pma_config and pma_config.max_text_chars > 0
        else PMA_MAX_TEXT
    )
    repos: list[dict[str, Any]] = []
    for snap in snapshots[:max_repos]:
        effective_destination = (
            dict(snap.effective_destination)
            if isinstance(snap.effective_destination, dict)
            else {"kind": "local"}
        )
        summary: dict[str, Any] = {
            "id": snap.id,
            "display_name": snap.display_name,
            "status": snap.status.value,
            "last_run_id": snap.last_run_id,
            "last_run_started_at": snap.last_run_started_at,
            "last_run_finished_at": snap.last_run_finished_at,
            "last_run_duration_seconds": None,
            "last_exit_code": snap.last_exit_code,
            "effective_destination": effective_destination,
            "ticket_flow": None,
            "run_state": None,
            "canonical_state_v1": None,
        }
        if snap.initialized and snap.exists_on_disk:
            summary["ticket_flow"] = build_ticket_flow_summary(
                snap.path, include_failure=False
            )
            run_state, run_record = get_latest_ticket_flow_run_state_with_record(
                snap.path, snap.id
            )
            summary["run_state"] = run_state
            if run_record is not None:
                if str(summary.get("last_run_id")) != str(run_record.id):
                    summary["last_exit_code"] = None
                summary["last_run_id"] = run_record.id
                summary["last_run_started_at"] = run_record.started_at
                summary["last_run_finished_at"] = run_record.finished_at
                summary["last_run_duration_seconds"] = flow_run_duration_seconds(
                    run_record
                )
            summary["canonical_state_v1"] = build_canonical_state_v1(
                repo_root=snap.path,
                repo_id=snap.id,
                run_state=summary["run_state"],
                record=run_record,
                preferred_run_id=(
                    str(snap.last_run_id) if snap.last_run_id is not None else None
                ),
                stale_threshold_seconds=stale_threshold_seconds,
            )
        repos.append(summary)

    agent_workspaces: list[dict[str, Any]] = []
    for workspace in agent_workspace_snapshots[:max_repos]:
        if hub_root is not None:
            summary = workspace.to_dict(hub_root)
        else:
            summary = {
                "id": workspace.id,
                "runtime": workspace.runtime,
                "path": str(workspace.path),
                "display_name": workspace.display_name,
                "enabled": workspace.enabled,
                "exists_on_disk": workspace.exists_on_disk,
                "effective_destination": workspace.effective_destination,
                "resource_kind": workspace.resource_kind,
            }
        agent_workspaces.append(summary)

    inbox = await asyncio.to_thread(
        _gather_inbox,
        supervisor,
        max_text_chars=max_text_chars,
        stale_threshold_seconds=stale_threshold_seconds,
    )
    inbox = inbox[:max_messages]

    lifecycle_events = await asyncio.to_thread(
        _gather_lifecycle_events, supervisor, limit=20
    )

    templates = _build_templates_snapshot(supervisor, hub_root=hub_root)

    pma_files: dict[str, list[str]] = {box: [] for box in BOXES}
    pma_files_detail: dict[str, list[dict[str, Any]]] = empty_listing()
    pma_threads: list[dict[str, Any]] = []
    automation = await asyncio.to_thread(_snapshot_pma_automation, supervisor)
    if hub_root:
        pma_files, pma_files_detail = _snapshot_pma_files(hub_root)
        pma_threads = _snapshot_pma_threads(hub_root)
        for thread in pma_threads:
            thread["freshness"] = build_freshness_payload(
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                candidates=[
                    ("thread_status_changed_at", thread.get("status_changed_at")),
                    ("thread_updated_at", thread.get("updated_at")),
                ],
            )
        for box in BOXES:
            for index, entry in enumerate(pma_files_detail.get(box) or []):
                entry["freshness"] = build_freshness_payload(
                    generated_at=generated_at,
                    stale_threshold_seconds=stale_threshold_seconds,
                    candidates=[("file_modified_at", entry.get("modified_at"))],
                )
                if box == "inbox":
                    pma_files_detail[box][index] = enrich_pma_file_inbox_entry(entry)

    action_queue = build_pma_action_queue(
        inbox=inbox,
        pma_threads=pma_threads,
        pma_files_detail=pma_files_detail,
        automation=automation,
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
    )

    freshness = _build_snapshot_freshness_summary(
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
        repos=repos,
        agent_workspaces=agent_workspaces,
        inbox=inbox,
        action_queue=action_queue,
        pma_threads=pma_threads,
        pma_files_detail=pma_files_detail,
    )

    return {
        "generated_at": generated_at,
        "repos": repos,
        "agent_workspaces": agent_workspaces,
        "inbox": inbox,
        "action_queue": action_queue,
        "templates": templates,
        "pma_files": pma_files,
        "pma_files_detail": pma_files_detail,
        "pma_threads": pma_threads,
        "automation": automation,
        "lifecycle_events": lifecycle_events,
        "freshness": freshness,
        "limits": {
            "max_repos": max_repos,
            "max_messages": max_messages,
            "max_text_chars": max_text_chars,
        },
    }


from .pma_action_queue import build_pma_action_queue  # noqa: E402
from .pma_inbox import _gather_inbox  # noqa: E402
from .pma_prompt_state import (  # noqa: E402, F401
    clear_pma_prompt_state_sessions,
    default_pma_prompt_state_path,
    list_pma_prompt_state_session_keys,
)
from .pma_rendering import _render_hub_snapshot  # noqa: E402
from .pma_ticket_flow_state import (  # noqa: E402, F401
    TicketFlowRunState,
    TicketFlowWorkerCrash,
    _dispatch_is_actionable,
    _latest_dispatch,
    _latest_reply_history_seq,
    _resolve_paused_dispatch_state,
    _ticket_flow_inbox_item_type_and_next_action,
    build_ticket_flow_run_state,
    get_latest_ticket_flow_run_state_with_record,
)
