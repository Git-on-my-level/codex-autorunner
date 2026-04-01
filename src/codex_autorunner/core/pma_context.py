from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, TypedDict, cast

from ..bootstrap import ensure_pma_docs, pma_doc_path
from ..flows.ticket_flow.runtime_helpers import ticket_flow_inbox_preflight
from ..tickets.files import safe_relpath
from ..tickets.models import Dispatch
from ..tickets.outbox import parse_dispatch, resolve_outbox_paths
from ..tickets.replies import resolve_reply_paths
from .chat_bindings import active_chat_binding_metadata_by_thread
from .config import load_hub_config, load_repo_config
from .filebox import BOXES, empty_listing, list_filebox
from .flows.failure_diagnostics import (
    format_failure_summary,
    get_failure_payload,
    get_terminal_failure_reason_code,
)
from .flows.models import (
    FlowRunRecord,
    FlowRunStatus,
    flow_run_duration_seconds,
    format_flow_duration,
)
from .flows.store import FlowStore
from .flows.worker_process import check_worker_health, read_worker_crash_info
from .flows.workspace_root import resolve_ticket_flow_workspace_root
from .freshness import (
    build_freshness_payload,
    iso_now,
    resolve_stale_threshold_seconds,
    summarize_section_freshness,
)
from .hub import HubSupervisor
from .hub_inbox_resolution import (
    MESSAGE_PENDING_AUTO_DISMISS_STATE,
    clear_message_resolution,
    find_message_resolution,
    find_message_resolution_entry,
    load_hub_inbox_dismissals,
    record_message_pending_auto_dismiss,
    record_message_resolution,
)
from .locks import file_lock
from .managed_thread_status import derive_managed_thread_operator_status
from .pma_active_context import (
    PMA_ACTIVE_CONTEXT_MAX_LINES,
    get_active_context_auto_prune_meta,
    maybe_auto_prune_active_context,
)
from .pma_audit import PmaActionType, PmaAuditEntry, PmaAuditLog
from .pma_thread_store import PmaThreadStore, default_pma_threads_db_path
from .state_roots import resolve_hub_templates_root
from .ticket_flow_projection import (
    build_canonical_state_v1,
    select_authoritative_run_record,
)
from .ticket_flow_summary import build_ticket_flow_summary
from .time_utils import now_iso
from .utils import atomic_write

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
PMA_PROMPT_STATE_FILENAME = "prompt_state.json"
PMA_PROMPT_STATE_VERSION = 1
PMA_PROMPT_STATE_MAX_SESSIONS = 200
PMA_PROMPT_DIGEST_PREVIEW = 12

PMA_PROMPT_SECTION_ORDER = (
    "prompt",
    "discoverability",
    "fastpath",
    "agents",
    "active_context",
    "context_log_tail",
    "hub_snapshot",
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

PMA_ACTION_QUEUE_PRECEDENCE: dict[str, tuple[int, str]] = {
    "ticket_flow_inbox": (10, "ticket_flow_inbox"),
    "automation_wakeup": (15, "automation_wakeup"),
    "managed_thread_followup": (20, "managed_thread_followup"),
    "pma_file_inbox": (30, "pma_file_inbox"),
}
PMA_FILE_NEXT_ACTION_PROCESS = "process_uploaded_file"
PMA_FILE_NEXT_ACTION_REVIEW_STALE = "review_stale_uploaded_file"

# Keep this short and stable; see ticket TICKET-001 for rationale.
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
   - If no suitable thread exists, spawn one, run work, and keep it compact:
     - `car pma thread spawn --agent codex --repo <repo_id> --name <label>`
     - `car pma thread spawn --resource-kind agent_workspace --resource-id <workspace_id> --name <label>`
     - `car pma thread send --id <managed_thread_id> --message "..." --watch`
     - `car pma thread send --id <managed_thread_id> --message-file prompt.md --watch`
     - `car pma thread send --id <managed_thread_id> --message "..." --notify-on terminal --notify-lane <lane_id>`
     - `car pma thread status --id <managed_thread_id>`
     - `car pma thread compact --id <id> --summary "..."`
     - `car pma thread archive --id <id>`
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
     - If it was already consumed or is no longer relevant, delete it from `.codex-autorunner/filebox/inbox/`.
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
   - Consult `.codex-autorunner/pma/docs/ABOUT_CAR.md` section “PMA automation wake-ups” for recipes.
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

# Defaults used when hub config is not available (should be rare).
PMA_DOCS_MAX_CHARS = 12_000
PMA_CONTEXT_LOG_TAIL_LINES = 120


class TicketFlowWorkerCrash(TypedDict):
    summary: Optional[str]
    open_url: str
    path: str


class TicketFlowRunState(TypedDict, total=False):
    state: str
    blocking_reason: Optional[str]
    current_ticket: Optional[str]
    last_progress_at: Optional[str]
    recommended_action: Optional[str]
    recommended_actions: list[str]
    attention_required: bool
    worker_status: Optional[str]
    crash: Optional[TicketFlowWorkerCrash]
    flow_status: str
    duration_seconds: Optional[float]
    repo_id: str
    run_id: str
    active_run_id: Optional[str]


def _tail_lines(text: str, max_lines: int) -> str:
    if max_lines <= 0:
        return ""
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[-max_lines:])


def load_pma_workspace_docs(hub_root: Path) -> dict[str, Any]:
    """Load hub-level PMA context docs for prompt injection.

    These docs act as durable memory and working context for PMA.
    """
    try:
        ensure_pma_docs(hub_root)
    except Exception as exc:
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
    except Exception as exc:
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
        except Exception as exc:
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


def default_pma_prompt_state_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_PROMPT_STATE_FILENAME


def _default_pma_prompt_state() -> dict[str, Any]:
    return {
        "version": PMA_PROMPT_STATE_VERSION,
        "sessions": {},
        "updated_at": now_iso(),
    }


def _prompt_state_lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _digest_text(value: Any) -> str:
    raw = value if isinstance(value, str) else str(value or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _digest_preview(digest: Any) -> str:
    if not isinstance(digest, str):
        return ""
    return digest[:PMA_PROMPT_DIGEST_PREVIEW]


def _is_digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


def _build_prompt_bundle_digest(sections: Mapping[str, Mapping[str, Any]]) -> str:
    payload = {
        name: str((sections.get(name) or {}).get("digest") or "")
        for name in PMA_PROMPT_SECTION_ORDER
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return _digest_text(raw)


def _read_pma_prompt_state_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _default_pma_prompt_state()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        _logger.warning("Could not read PMA prompt state: %s", exc)
        return _default_pma_prompt_state()
    return data if isinstance(data, dict) else _default_pma_prompt_state()


def _write_pma_prompt_state_unlocked(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _validate_prompt_session_record(record: Any) -> bool:
    if not isinstance(record, Mapping):
        return False
    sections = record.get("sections")
    if not isinstance(sections, Mapping):
        return False
    for name in PMA_PROMPT_SECTION_ORDER:
        section = sections.get(name)
        if not isinstance(section, Mapping):
            return False
        if not _is_digest(section.get("digest")):
            return False
    bundle_digest = record.get("bundle_digest")
    if not _is_digest(bundle_digest):
        return False
    return bundle_digest == _build_prompt_bundle_digest(
        cast(Mapping[str, Mapping[str, Any]], sections)
    )


def _trim_prompt_sessions(sessions: Mapping[str, Any]) -> dict[str, Any]:
    items: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in sessions.items():
        if not isinstance(key, str) or not key:
            continue
        if not isinstance(value, Mapping):
            continue
        items.append((key, value))
    if len(items) <= PMA_PROMPT_STATE_MAX_SESSIONS:
        return {key: dict(value) for key, value in items}

    def _sort_key(item: tuple[str, Mapping[str, Any]]) -> tuple[str, str]:
        updated_at = str(item[1].get("updated_at") or "")
        return (updated_at, item[0])

    trimmed = sorted(items, key=_sort_key)[-PMA_PROMPT_STATE_MAX_SESSIONS:]
    return {key: dict(value) for key, value in trimmed}


def clear_pma_prompt_state_sessions(
    hub_root: Path,
    *,
    keys: Sequence[str] = (),
    prefixes: Sequence[str] = (),
    exclude_prefixes: Sequence[str] = (),
) -> list[str]:
    """Clear PMA prompt-state sessions by exact key and/or key prefix."""

    normalized_keys = {
        str(key).strip() for key in keys if isinstance(key, str) and key.strip()
    }
    normalized_prefixes = tuple(
        str(prefix).strip()
        for prefix in prefixes
        if isinstance(prefix, str) and prefix.strip()
    )
    normalized_excludes = tuple(
        str(prefix).strip()
        for prefix in exclude_prefixes
        if isinstance(prefix, str) and prefix.strip()
    )
    if not normalized_keys and not normalized_prefixes:
        return []

    path = default_pma_prompt_state_path(hub_root)
    lock_path = _prompt_state_lock_path(path)
    cleared_keys: list[str] = []

    def _is_excluded(session_key: str) -> bool:
        return any(
            session_key == excluded.rstrip(".") or session_key.startswith(excluded)
            for excluded in normalized_excludes
        )

    with file_lock(lock_path):
        state = _read_pma_prompt_state_unlocked(path)
        sessions = state.get("sessions")
        if not isinstance(sessions, Mapping):
            return []

        updated_sessions = dict(sessions)
        for session_key in tuple(updated_sessions.keys()):
            if not isinstance(session_key, str) or not session_key:
                continue
            key_match = session_key in normalized_keys
            prefix_match = bool(normalized_prefixes) and any(
                session_key.startswith(prefix) for prefix in normalized_prefixes
            )
            if not key_match and not prefix_match:
                continue
            if _is_excluded(session_key):
                continue
            updated_sessions.pop(session_key, None)
            cleared_keys.append(session_key)

        if cleared_keys:
            state["version"] = PMA_PROMPT_STATE_VERSION
            state["updated_at"] = now_iso()
            state["sessions"] = _trim_prompt_sessions(updated_sessions)
            _write_pma_prompt_state_unlocked(path, state)

    return sorted(cleared_keys)


def list_pma_prompt_state_session_keys(hub_root: Path) -> list[str]:
    """Return persisted PMA prompt-state session keys."""

    path = default_pma_prompt_state_path(hub_root)
    lock_path = _prompt_state_lock_path(path)
    with file_lock(lock_path):
        state = _read_pma_prompt_state_unlocked(path)
        sessions = state.get("sessions")
        if not isinstance(sessions, Mapping):
            return []
        return sorted(
            key for key in sessions.keys() if isinstance(key, str) and key.strip()
        )


def _merge_prompt_session_state(
    hub_root: Path,
    *,
    prompt_state_key: str,
    sections: Mapping[str, Mapping[str, str]],
    force_full_context: bool,
) -> tuple[bool, str, Optional[Mapping[str, Any]], Optional[str]]:
    path = default_pma_prompt_state_path(hub_root)
    lock_path = _prompt_state_lock_path(path)
    use_delta = False
    delta_reason = "first_turn"
    prior_sections: Optional[Mapping[str, Any]] = None
    prior_updated_at: Optional[str] = None

    with file_lock(lock_path):
        state = _read_pma_prompt_state_unlocked(path)
        sessions = state.get("sessions")
        if isinstance(sessions, Mapping):
            prior_record = sessions.get(prompt_state_key)
            if _validate_prompt_session_record(prior_record):
                validated_record = cast(Mapping[str, Any], prior_record)
                prior_sections = cast(
                    Optional[Mapping[str, Any]], validated_record.get("sections")
                )
                prior_updated_at = cast(
                    Optional[str], validated_record.get("updated_at")
                )
                if force_full_context:
                    delta_reason = "explicit_refresh"
                else:
                    use_delta = True
                    delta_reason = "cached_context"
            elif prior_record is not None:
                delta_reason = "digest_mismatch"

        updated_sessions = dict(sessions) if isinstance(sessions, Mapping) else {}
        timestamp = now_iso()
        updated_sessions[prompt_state_key] = {
            "version": PMA_PROMPT_STATE_VERSION,
            "updated_at": timestamp,
            "bundle_digest": _build_prompt_bundle_digest(sections),
            "sections": {
                name: {"digest": str(payload.get("digest") or "")}
                for name, payload in sections.items()
            },
        }
        state["version"] = PMA_PROMPT_STATE_VERSION
        state["updated_at"] = timestamp
        state["sessions"] = _trim_prompt_sessions(updated_sessions)
        _write_pma_prompt_state_unlocked(path, state)

    return use_delta, delta_reason, prior_sections, prior_updated_at


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
    except Exception:
        raw = str(extra)
    if len(raw) <= limit:
        return extra
    return {
        "_omitted": True,
        "note": "extra omitted due to size",
        "preview": _truncate(raw, limit),
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
    except Exception as exc:
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
    except Exception as exc:
        _logger.warning("Could not list filebox contents: %s", exc)
    return pma_files, pma_files_detail


def _snapshot_pma_threads(
    hub_root: Path,
    *,
    limit: int = PMA_MAX_PMA_THREADS,
    max_preview_chars: int = PMA_MAX_TEMPLATE_FIELD_CHARS,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    db_path = default_pma_threads_db_path(hub_root)
    if not db_path.exists():
        return []

    try:
        store = PmaThreadStore(hub_root)
        threads = store.list_threads(limit=limit)
    except Exception as exc:
        _logger.warning("Could not load PMA managed threads: %s", exc)
        return []
    try:
        chat_binding_metadata = active_chat_binding_metadata_by_thread(
            hub_root=hub_root
        )
    except Exception as exc:
        _logger.warning(
            "Could not load PMA chat-binding metadata for thread snapshot: %s", exc
        )
        chat_binding_metadata = {}

    snapshot_threads: list[dict[str, Any]] = []
    for thread in threads[:limit]:
        managed_thread_id = str(thread.get("managed_thread_id") or "").strip()
        workspace_raw = str(thread.get("workspace_root") or "").strip()
        workspace_root = workspace_raw
        if workspace_raw:
            try:
                workspace_root = safe_relpath(Path(workspace_raw).resolve(), hub_root)
            except Exception:
                workspace_root = workspace_raw
        chat_binding = chat_binding_metadata.get(managed_thread_id, {})
        snapshot_threads.append(
            {
                "managed_thread_id": managed_thread_id
                or thread.get("managed_thread_id"),
                "agent": thread.get("agent"),
                "repo_id": thread.get("repo_id"),
                "resource_kind": thread.get("resource_kind"),
                "resource_id": thread.get("resource_id"),
                "workspace_root": workspace_root,
                "name": thread.get("name"),
                "status": thread.get("normalized_status") or thread.get("status"),
                "lifecycle_status": thread.get("lifecycle_status")
                or thread.get("status"),
                "status_reason": thread.get("status_reason")
                or thread.get("status_reason_code"),
                "status_terminal": bool(thread.get("status_terminal")),
                "status_changed_at": thread.get("status_changed_at")
                or thread.get("status_updated_at"),
                "last_turn_id": thread.get("last_turn_id"),
                "last_message_preview": _truncate(
                    str(thread.get("last_message_preview") or ""),
                    max_preview_chars,
                ),
                "updated_at": thread.get("updated_at"),
                "chat_bound": bool(chat_binding.get("chat_bound")),
                "binding_kind": chat_binding.get("binding_kind"),
                "binding_id": chat_binding.get("binding_id"),
                "binding_count": int(chat_binding.get("binding_count") or 0),
                "binding_kinds": list(chat_binding.get("binding_kinds") or []),
                "binding_ids": list(chat_binding.get("binding_ids") or []),
                "cleanup_protected": bool(chat_binding.get("cleanup_protected")),
            }
        )
    return snapshot_threads


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


def _extract_entry_freshness(entry: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    freshness = entry.get("freshness")
    if isinstance(freshness, Mapping):
        return freshness
    canonical = entry.get("canonical_state_v1")
    if isinstance(canonical, Mapping):
        nested = canonical.get("freshness")
        if isinstance(nested, Mapping):
            return nested
    return None


def classify_pma_file_inbox_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    freshness = _extract_entry_freshness(entry)
    is_stale = bool(
        isinstance(freshness, Mapping) and freshness.get("is_stale") is True
    )
    if is_stale:
        return {
            "next_action": PMA_FILE_NEXT_ACTION_REVIEW_STALE,
            "attention_summary": (
                "Likely stale leftover upload. Verify whether it was already handled "
                "before treating it as new work."
            ),
            "why_selected": (
                "Stale file remains in the PMA inbox and is more likely leftover "
                "state than urgent work"
            ),
            "recommended_action": PMA_FILE_NEXT_ACTION_REVIEW_STALE,
            "recommended_detail": (
                "Check recent PMA activity before routing. If the file was already "
                "handled, delete it from `.codex-autorunner/filebox/inbox/`."
            ),
            "urgency": "low",
            "likely_false_positive": True,
        }
    return {
        "next_action": PMA_FILE_NEXT_ACTION_PROCESS,
        "attention_summary": "Fresh upload is waiting in the PMA inbox.",
        "why_selected": "Fresh upload is waiting in the PMA inbox",
        "recommended_action": PMA_FILE_NEXT_ACTION_PROCESS,
        "recommended_detail": (
            "Inspect `.codex-autorunner/filebox/inbox/` and route the upload"
        ),
        "urgency": "normal",
        "likely_false_positive": False,
    }


def enrich_pma_file_inbox_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    enriched = dict(entry)
    enriched.update(classify_pma_file_inbox_entry(enriched))
    return enriched


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_sort_value(value: Any) -> float:
    parsed = _parse_iso_timestamp(value)
    if parsed is None:
        return 0.0
    return parsed.timestamp()


def _resolve_inbox_auto_dismiss_grace_seconds(supervisor: HubSupervisor) -> int:
    raw = os.getenv("CAR_PMA_INBOX_AUTO_DISMISS_GRACE_SECONDS")
    if raw is not None:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return max(
        0,
        int(
            getattr(supervisor.hub_config.pma, "inbox_auto_dismiss_grace_seconds", 3600)
        ),
    )


def _record_inbox_auto_dismiss_audit(
    *,
    supervisor: HubSupervisor,
    repo_id: str,
    repo_root: Path,
    run_id: str,
    item_type: str,
    seq: Optional[int],
    reason: str,
    grace_seconds: int,
    reason_code: Optional[str],
    terminal_failure_reason_code: Optional[str],
) -> None:
    try:
        PmaAuditLog(supervisor.hub_config.root).append(
            PmaAuditEntry(
                action_type=PmaActionType.INBOX_AUTO_DISMISSED,
                agent="pma",
                status="ok",
                details={
                    "repo_id": repo_id,
                    "repo_root": str(repo_root),
                    "run_id": run_id,
                    "item_type": item_type,
                    "seq": seq,
                    "grace_seconds": grace_seconds,
                    "reason": reason,
                    "reason_code": reason_code,
                    "terminal_failure_reason_code": terminal_failure_reason_code,
                    "action": "dismiss",
                },
            )
        )
    except Exception as exc:
        _logger.warning(
            "Failed to record PMA inbox auto-dismiss audit entry for repo %s run %s: %s",
            repo_id,
            run_id,
            exc,
        )


def _maybe_auto_dismiss_terminal_inbox_item(
    *,
    supervisor: HubSupervisor,
    repo_root: Path,
    repo_id: str,
    dismissals: dict[str, dict[str, Any]],
    record: FlowRunRecord,
    item_type: str,
    seq: Optional[int],
) -> Optional[dict[str, Any]]:
    if item_type not in {"run_failed", "run_stopped"}:
        return None

    run_id = str(record.id)
    terminal_failure_reason = get_terminal_failure_reason_code(record)
    existing_entry = find_message_resolution_entry(
        dismissals,
        run_id=run_id,
        item_type=item_type,
        seq=seq,
        include_unresolved=True,
    )
    preflight = ticket_flow_inbox_preflight(repo_root)
    if preflight.is_recoverable:
        if (
            isinstance(existing_entry, dict)
            and str(existing_entry.get("resolution_state") or "").strip().lower()
            == MESSAGE_PENDING_AUTO_DISMISS_STATE
            and clear_message_resolution(
                repo_root=repo_root,
                run_id=run_id,
                item_type=item_type,
                seq=seq,
            )
        ):
            dismissals.clear()
            dismissals.update(load_hub_inbox_dismissals(repo_root))
        return None

    grace_seconds = _resolve_inbox_auto_dismiss_grace_seconds(supervisor)
    pending = existing_entry
    pending_state = (
        str(pending.get("resolution_state") or "").strip().lower()
        if isinstance(pending, dict)
        else ""
    )
    pending_reason = (
        str(pending.get("reason") or "").strip() if isinstance(pending, dict) else ""
    )
    expires_at = _parse_iso_timestamp(
        pending.get("expires_at") if isinstance(pending, dict) else None
    )
    if (
        pending_state == MESSAGE_PENDING_AUTO_DISMISS_STATE
        and pending_reason == str(preflight.reason or "").strip()
        and expires_at is not None
    ):
        now = datetime.now(timezone.utc)
        if now >= expires_at:
            reason = (
                f"Auto-dismissed unrecoverable inbox item after {grace_seconds} seconds: "
                f"{preflight.reason}"
            )
            resolved = record_message_resolution(
                repo_root=repo_root,
                repo_id=repo_id,
                run_id=run_id,
                item_type=item_type,
                seq=seq,
                action="dismiss",
                reason=reason,
                actor="hub_inbox_auto_dismiss",
            )
            dismissals.clear()
            dismissals.update(load_hub_inbox_dismissals(repo_root))
            _record_inbox_auto_dismiss_audit(
                supervisor=supervisor,
                repo_id=repo_id,
                repo_root=repo_root,
                run_id=run_id,
                item_type=item_type,
                seq=seq,
                reason=reason,
                grace_seconds=grace_seconds,
                reason_code=preflight.reason_code,
                terminal_failure_reason_code=(
                    terminal_failure_reason.value
                    if terminal_failure_reason is not None
                    else None
                ),
            )
            _logger.info(
                "Auto-dismissed unrecoverable ticket_flow inbox item for repo %s run %s (%s)",
                repo_id,
                run_id,
                preflight.reason,
            )
            return resolved
        return None

    if grace_seconds == 0:
        reason = (
            f"Auto-dismissed unrecoverable inbox item immediately: {preflight.reason}"
        )
        resolved = record_message_resolution(
            repo_root=repo_root,
            repo_id=repo_id,
            run_id=run_id,
            item_type=item_type,
            seq=seq,
            action="dismiss",
            reason=reason,
            actor="hub_inbox_auto_dismiss",
        )
        dismissals.clear()
        dismissals.update(load_hub_inbox_dismissals(repo_root))
        _record_inbox_auto_dismiss_audit(
            supervisor=supervisor,
            repo_id=repo_id,
            repo_root=repo_root,
            run_id=run_id,
            item_type=item_type,
            seq=seq,
            reason=reason,
            grace_seconds=grace_seconds,
            reason_code=preflight.reason_code,
            terminal_failure_reason_code=(
                terminal_failure_reason.value
                if terminal_failure_reason is not None
                else None
            ),
        )
        _logger.info(
            "Auto-dismissed unrecoverable ticket_flow inbox item for repo %s run %s (%s)",
            repo_id,
            run_id,
            preflight.reason,
        )
        return resolved

    record_message_pending_auto_dismiss(
        repo_root=repo_root,
        repo_id=repo_id,
        run_id=run_id,
        item_type=item_type,
        seq=seq,
        reason=preflight.reason,
        grace_seconds=grace_seconds,
        actor="hub_inbox_auto_dismiss",
    )
    dismissals.clear()
    dismissals.update(load_hub_inbox_dismissals(repo_root))
    _logger.info(
        "Scheduled auto-dismiss for unrecoverable ticket_flow inbox item in repo %s run %s after %s seconds (%s)",
        repo_id,
        run_id,
        grace_seconds,
        preflight.reason,
    )
    return None


def _queue_precedence(source: str) -> tuple[int, str]:
    return PMA_ACTION_QUEUE_PRECEDENCE.get(source, (999, source or "unknown"))


def _queue_supersession_payload(
    *,
    status: str,
    is_primary: bool,
    superseded: bool,
    superseded_by: Optional[str],
    reason: Optional[str],
) -> dict[str, Any]:
    return {
        "status": status,
        "is_primary": is_primary,
        "superseded": superseded,
        "superseded_by": superseded_by,
        "reason": reason,
    }


def _thread_followup_state_rank(state: str) -> int:
    normalized = state.strip().lower()
    if normalized == "attention_required":
        return 0
    if normalized == "awaiting_followup":
        return 10
    if normalized == "reusable":
        return 20
    if normalized == "protected_chat_bound":
        return 25
    if normalized == "idle_archive_candidate":
        return 30
    return 99


def _thread_followup_semantics(entry: Mapping[str, Any]) -> dict[str, Any]:
    status = str(entry.get("status") or "").strip().lower()
    status_reason = str(entry.get("status_reason") or "").strip().lower()
    last_turn_id = str(entry.get("last_turn_id") or "").strip()
    is_chat_bound = bool(
        entry.get("chat_bound") is True or entry.get("cleanup_protected") is True
    )
    freshness = _extract_entry_freshness(entry)
    is_stale = bool(
        isinstance(freshness, Mapping) and freshness.get("is_stale") is True
    )

    if status == "failed":
        return {
            "followup_state": "attention_required",
            "operator_need": "urgent",
            "recommended_action": "inspect_managed_thread_failure",
            "why_selected": "Managed thread failed and needs inspection before reuse",
            "recommended_detail_template": "car pma thread status --id {managed_thread_id}",
        }
    if status == "paused":
        return {
            "followup_state": "awaiting_followup",
            "operator_need": "normal",
            "recommended_action": "resume_managed_thread",
            "why_selected": "Managed thread is paused and likely waiting for the next turn",
            "recommended_detail_template": (
                'car pma thread send --id {managed_thread_id} --message "..." --watch'
            ),
        }
    if status in {"completed", "interrupted"}:
        if is_stale:
            if is_chat_bound:
                return {
                    "followup_state": "protected_chat_bound",
                    "operator_need": "protected",
                    "recommended_action": "show_protected_threads",
                    "why_selected": (
                        "Chat-bound managed thread is dormant, but continuity-bearing "
                        "chat threads are protected from cleanup by default"
                    ),
                    "recommended_detail_template": (
                        "Protected by default: review this chat-bound thread before "
                        "taking any action. Do not archive or remove it unless the "
                        'user is explicit; broad requests like "clean up workspace" '
                        "are not enough."
                    ),
                }
            return {
                "followup_state": "idle_archive_candidate",
                "operator_need": "cleanup",
                "recommended_action": "review_or_archive_managed_thread",
                "why_selected": (
                    "Managed thread has been dormant since its last completed turn "
                    "and is a cleanup candidate"
                ),
                "recommended_detail_template": (
                    "Review before cleanup: car pma thread archive --id "
                    "{managed_thread_id} if dormant, or reuse it with "
                    'car pma thread send --id {managed_thread_id} --message "..." --watch'
                ),
            }
        return {
            "followup_state": "reusable",
            "operator_need": "optional",
            "recommended_action": "consider_resuming_managed_thread",
            "why_selected": (
                "Managed thread can be reused, but there is no stronger signal "
                "that PMA needs to act on it now"
            ),
            "recommended_detail_template": (
                "Optional reuse: car pma thread send --id {managed_thread_id} "
                '--message "..." --watch'
            ),
        }
    if status == "idle":
        if (
            (status_reason == "thread_created" and not last_turn_id)
            or status_reason == "thread_resumed"
        ) and not is_stale:
            return {
                "followup_state": "reusable",
                "operator_need": "optional",
                "recommended_action": "consider_resuming_managed_thread",
                "why_selected": (
                    "Managed thread was recently created or resumed, but there is "
                    "no explicit wake-up or continuity signal that PMA needs to "
                    "act on it now"
                ),
                "recommended_detail_template": (
                    "Optional reuse: car pma thread send --id {managed_thread_id} "
                    '--message "..." --watch'
                ),
            }
        if is_stale:
            if is_chat_bound:
                return {
                    "followup_state": "protected_chat_bound",
                    "operator_need": "protected",
                    "recommended_action": "show_protected_threads",
                    "why_selected": (
                        "Chat-bound managed thread is dormant, but continuity-bearing "
                        "chat threads are protected from cleanup by default"
                    ),
                    "recommended_detail_template": (
                        "Protected by default: review this chat-bound thread before "
                        "taking any action. Do not archive or remove it unless the "
                        'user is explicit; broad requests like "clean up workspace" '
                        "are not enough."
                    ),
                }
            return {
                "followup_state": "idle_archive_candidate",
                "operator_need": "cleanup",
                "recommended_action": "review_or_archive_managed_thread",
                "why_selected": (
                    "Managed thread has been idle for a while with no recent "
                    "operator signal and is a cleanup candidate"
                ),
                "recommended_detail_template": (
                    "Review before cleanup: car pma thread archive --id "
                    "{managed_thread_id} if dormant, or reuse it with "
                    'car pma thread send --id {managed_thread_id} --message "..." --watch'
                ),
            }
        return {
            "followup_state": "reusable",
            "operator_need": "optional",
            "recommended_action": "consider_resuming_managed_thread",
            "why_selected": (
                "Managed thread is available for reuse, but no immediate follow-up "
                "signal is present"
            ),
            "recommended_detail_template": (
                "Optional reuse: car pma thread send --id {managed_thread_id} "
                '--message "..." --watch'
            ),
        }
    return {
        "followup_state": "reusable",
        "operator_need": "optional",
        "recommended_action": "consider_resuming_managed_thread",
        "why_selected": "Managed thread can accept another turn if PMA needs it",
        "recommended_detail_template": (
            "Optional reuse: car pma thread send --id {managed_thread_id} "
            '--message "..." --watch'
        ),
    }


def _thread_group_owner_label(
    *,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    workspace_root: Optional[str],
) -> str:
    if repo_id:
        return f"repo {repo_id}"
    if resource_kind and resource_id:
        return f"{resource_kind}:{resource_id}"
    if workspace_root:
        return workspace_root
    return "unowned threads"


def _collect_thread_owner_labels(items: Sequence[Mapping[str, Any]]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for item in items:
        label = _thread_group_owner_label(
            repo_id=str(item.get("repo_id") or "").strip() or None,
            resource_kind=str(item.get("resource_kind") or "").strip() or None,
            resource_id=str(item.get("resource_id") or "").strip() or None,
            workspace_root=str(item.get("workspace_root") or "").strip() or None,
        )
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels


def _build_low_signal_thread_summary_item(
    *,
    followup_state: str,
    items: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    newest = max(items, key=lambda item: float(item.get("sort_timestamp") or 0.0))
    owner_labels = _collect_thread_owner_labels(items)
    repo_ids = sorted(
        {
            str(item.get("repo_id") or "").strip()
            for item in items
            if str(item.get("repo_id") or "").strip()
        }
    )
    summary_repo_id = None
    if len(repo_ids) == 1:
        candidate_repo_id = repo_ids[0]
        if all(
            str(item.get("repo_id") or "").strip() == candidate_repo_id
            for item in items
        ):
            summary_repo_id = candidate_repo_id
    if followup_state == "idle_archive_candidate":
        name = f"Dormant cleanup candidates ({len(items)})"
        operator_need = "cleanup"
        why_selected = (
            f"Collapsed {len(items)} dormant cleanup candidates into a counts-first "
            "summary so cleanup inventory does not dominate the main queue"
        )
        recommended_action = "show_cleanup_candidates"
        recommended_detail = (
            "Show cleanup candidates to inspect the dormant managed threads before "
            "archiving any of them."
        )
        drilldown_commands = ["show cleanup candidates"]
    elif followup_state == "protected_chat_bound":
        name = f"Protected chat-bound threads ({len(items)})"
        operator_need = "protected"
        why_selected = (
            f"Collapsed {len(items)} protected chat-bound threads into a counts-first "
            "summary so continuity-bearing chat state stays separate from generic "
            "cleanup inventory"
        )
        recommended_action = "show_protected_threads"
        recommended_detail = (
            "Show protected threads to review chat-bound continuity artifacts. "
            "These are protected by default and should not be archived or removed "
            'unless the user is very explicit; broad requests like "clean up '
            'workspace" are not enough.'
        )
        drilldown_commands = ["show protected threads"]
    else:
        name = f"Reusable managed threads ({len(items)})"
        operator_need = "optional"
        why_selected = (
            f"Collapsed {len(items)} reusable managed threads into a counts-first "
            "summary so optional reuse stays behind true next-action work"
        )
        recommended_action = "show_reusable_threads"
        recommended_detail = (
            "Show reusable threads to inspect the available managed threads before "
            "reusing one."
        )
        drilldown_commands = ["show reusable threads"]

    if owner_labels:
        owner_preview = ", ".join(owner_labels[:3])
        if len(owner_labels) > 3:
            owner_preview += ", ..."
        recommended_detail += f" Owners: {owner_preview}."

    return {
        "item_type": "managed_thread_followup_summary",
        "queue_source": "managed_thread_followup",
        "action_queue_id": f"managed_thread_followup_summary:{followup_state}",
        "precedence": dict(newest.get("precedence") or {}),
        "repo_id": summary_repo_id,
        "name": name,
        "thread_count": len(items),
        "managed_thread_ids": [
            item.get("managed_thread_id")
            for item in items
            if item.get("managed_thread_id")
        ],
        "chat_bound_thread_count": sum(1 for item in items if item.get("chat_bound")),
        "followup_state": followup_state,
        "followup_state_counts": {followup_state: len(items)},
        "operator_need": operator_need,
        "operator_need_rank": _thread_followup_state_rank(followup_state),
        "why_selected": why_selected,
        "recommended_action": recommended_action,
        "recommended_detail": recommended_detail,
        "drilldown_commands": drilldown_commands,
        "cleanup_protected": followup_state == "protected_chat_bound",
        "owner_count": len(owner_labels),
        "owner_labels": owner_labels,
        "freshness": (
            dict(newest.get("freshness") or {})
            if isinstance(newest.get("freshness"), Mapping)
            else None
        ),
        "scope": {
            "kind": "thread_summary",
            "key": f"thread_summary:{followup_state}",
        },
        "sort_timestamp": newest.get("sort_timestamp"),
    }


def _collapse_low_signal_thread_queue_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    passthrough: list[dict[str, Any]] = []
    reusable_items: list[dict[str, Any]] = []
    protected_items: list[dict[str, Any]] = []
    cleanup_items: list[dict[str, Any]] = []

    for item in items:
        followup_state = str(item.get("followup_state") or "").strip().lower()
        if followup_state == "reusable":
            reusable_items.append(item)
            continue
        if followup_state == "protected_chat_bound":
            protected_items.append(item)
            continue
        if followup_state == "idle_archive_candidate":
            cleanup_items.append(item)
            continue
        passthrough.append(item)

    collapsed: list[dict[str, Any]] = list(passthrough)
    if reusable_items:
        collapsed.append(
            _build_low_signal_thread_summary_item(
                followup_state="reusable",
                items=reusable_items,
            )
        )
    if protected_items:
        collapsed.append(
            _build_low_signal_thread_summary_item(
                followup_state="protected_chat_bound",
                items=protected_items,
            )
        )
    if cleanup_items:
        collapsed.append(
            _build_low_signal_thread_summary_item(
                followup_state="idle_archive_candidate",
                items=cleanup_items,
            )
        )

    return collapsed


def _build_ticket_flow_queue_items(
    inbox: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    rank, label = _queue_precedence("ticket_flow_inbox")
    for entry in inbox:
        if not isinstance(entry, dict):
            continue
        copied = dict(entry)
        repo_id = str(copied.get("repo_id") or "").strip()
        run_id = str(copied.get("run_id") or "").strip()
        seq = copied.get("seq")
        queue_id = f"ticket_flow_inbox:{repo_id or '-'}:{run_id or '-'}:{seq or 0}"
        recommended_detail = (
            str((copied.get("run_state") or {}).get("recommended_action") or "").strip()
            or None
        )
        next_action = (
            str(copied.get("next_action") or "").strip() or "inspect_and_resume"
        )
        freshness = _extract_entry_freshness(copied)
        copied.update(
            {
                "action_queue_id": queue_id,
                "queue_source": "ticket_flow_inbox",
                "precedence": {"rank": rank, "label": label},
                "why_selected": (
                    "Newest authoritative ticket-flow run requires operator attention"
                    if copied.get("dispatch_actionable") is False
                    else "Newest authoritative ticket-flow run has an unanswered dispatch"
                ),
                "recommended_action": next_action,
                "recommended_detail": recommended_detail,
                "freshness": (
                    dict(freshness) if isinstance(freshness, Mapping) else None
                ),
                "scope": {
                    "kind": "run",
                    "key": f"run:{run_id}" if run_id else f"repo:{repo_id}",
                },
                "sort_timestamp": _timestamp_sort_value(
                    (freshness or {}).get("basis_at")
                    if isinstance(freshness, Mapping)
                    else copied.get("run_created_at")
                ),
            }
        )
        items.append(copied)
    return items


def _build_thread_queue_items(
    pma_threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    rank, label = _queue_precedence("managed_thread_followup")
    for entry in pma_threads:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status in {"", "running", "archived"}:
            continue
        repo_id = str(entry.get("repo_id") or "").strip()
        managed_thread_id = str(entry.get("managed_thread_id") or "").strip()
        freshness = _extract_entry_freshness(entry)
        semantics = _thread_followup_semantics(entry)
        next_action = str(semantics.get("recommended_action") or "").strip() or None
        why_selected = str(semantics.get("why_selected") or "").strip() or None
        detail_template = (
            str(semantics.get("recommended_detail_template") or "").strip() or None
        )
        scope_key = (
            f"thread:{managed_thread_id}"
            if managed_thread_id
            else (
                f"resource:{entry.get('resource_kind')}:{entry.get('resource_id')}"
                if entry.get("resource_kind") and entry.get("resource_id")
                else f"repo:{repo_id}"
            )
        )
        items.append(
            {
                "item_type": "managed_thread_followup",
                "managed_thread_id": managed_thread_id,
                "repo_id": repo_id or None,
                "agent": entry.get("agent"),
                "resource_kind": entry.get("resource_kind"),
                "resource_id": entry.get("resource_id"),
                "workspace_root": entry.get("workspace_root"),
                "name": entry.get("name"),
                "status": entry.get("status"),
                "lifecycle_status": entry.get("lifecycle_status"),
                "status_reason": entry.get("status_reason"),
                "status_terminal": entry.get("status_terminal"),
                "last_turn_id": entry.get("last_turn_id"),
                "last_message_preview": entry.get("last_message_preview"),
                "updated_at": entry.get("updated_at"),
                "chat_bound": bool(entry.get("chat_bound")),
                "binding_kind": entry.get("binding_kind"),
                "binding_id": entry.get("binding_id"),
                "binding_count": int(entry.get("binding_count") or 0),
                "binding_kinds": list(entry.get("binding_kinds") or []),
                "binding_ids": list(entry.get("binding_ids") or []),
                "cleanup_protected": bool(entry.get("cleanup_protected")),
                "open_url": (
                    f"/hub/pma/threads/{managed_thread_id}"
                    if managed_thread_id
                    else None
                ),
                "action_queue_id": f"managed_thread_followup:{managed_thread_id or '-'}",
                "queue_source": "managed_thread_followup",
                "precedence": {"rank": rank, "label": label},
                "followup_state": semantics.get("followup_state"),
                "operator_need": semantics.get("operator_need"),
                "operator_need_rank": _thread_followup_state_rank(
                    str(semantics.get("followup_state") or "")
                ),
                "why_selected": why_selected,
                "recommended_action": next_action,
                "recommended_detail": (
                    detail_template.format(managed_thread_id=managed_thread_id)
                    if managed_thread_id and detail_template
                    else None
                ),
                "freshness": (
                    dict(freshness) if isinstance(freshness, Mapping) else None
                ),
                "scope": {
                    "kind": "thread" if managed_thread_id else "resource",
                    "key": scope_key,
                },
                "sort_timestamp": _timestamp_sort_value(
                    (freshness or {}).get("basis_at")
                    if isinstance(freshness, Mapping)
                    else entry.get("updated_at")
                ),
            }
        )
    return _collapse_low_signal_thread_queue_items(items)


def _build_file_queue_items(
    pma_files_detail: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    stale_items: list[dict[str, Any]] = []
    rank, label = _queue_precedence("pma_file_inbox")
    for entry in pma_files_detail.get("inbox") or []:
        if not isinstance(entry, dict):
            continue
        copied = enrich_pma_file_inbox_entry(entry)
        freshness = _extract_entry_freshness(copied)
        name = str(copied.get("name") or "").strip()
        copied.update(
            {
                "action_queue_id": f"pma_file_inbox:{name or '-'}",
                "queue_source": "pma_file_inbox",
                "precedence": {"rank": rank, "label": label},
                "freshness": (
                    dict(freshness) if isinstance(freshness, Mapping) else None
                ),
                "scope": {"kind": "filebox", "key": f"filebox:inbox:{name or '-'}"},
                "sort_timestamp": _timestamp_sort_value(
                    (freshness or {}).get("basis_at")
                    if isinstance(freshness, Mapping)
                    else entry.get("modified_at")
                ),
            }
        )
        if copied.get("next_action") == PMA_FILE_NEXT_ACTION_REVIEW_STALE:
            stale_items.append(copied)
            continue
        items.append(copied)
    if stale_items:
        newest = max(
            stale_items, key=lambda item: float(item.get("sort_timestamp") or 0.0)
        )
        items.append(
            {
                "item_type": "pma_file_summary",
                "queue_source": "pma_file_inbox",
                "action_queue_id": "pma_file_inbox_summary:stale_uploaded_files",
                "precedence": {"rank": rank, "label": label},
                "name": f"Stale uploaded files ({len(stale_items)})",
                "file_count": len(stale_items),
                "file_names": [
                    str(item.get("name") or "").strip()
                    for item in stale_items
                    if str(item.get("name") or "").strip()
                ],
                "next_action": PMA_FILE_NEXT_ACTION_REVIEW_STALE,
                "recommended_action": "show_stale_uploaded_files",
                "recommended_detail": (
                    "Show stale uploaded files to inspect likely leftovers before "
                    "deleting or routing any of them."
                ),
                "why_selected": (
                    f"Collapsed {len(stale_items)} stale uploaded files into a "
                    "counts-first summary so likely leftovers do not dominate the "
                    "main queue"
                ),
                "operator_need": "review",
                "operator_need_rank": 40,
                "likely_false_positive": True,
                "drilldown_commands": ["show stale uploaded files"],
                "freshness": (
                    dict(newest.get("freshness") or {})
                    if isinstance(newest.get("freshness"), Mapping)
                    else None
                ),
                "scope": {
                    "kind": "filebox_summary",
                    "key": "filebox_summary:stale_uploaded_files",
                },
                "sort_timestamp": newest.get("sort_timestamp"),
            }
        )
    return items


def _build_automation_queue_items(
    automation: Mapping[str, Any],
    *,
    generated_at: str,
    stale_threshold_seconds: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    rank, label = _queue_precedence("automation_wakeup")
    wakeups = (automation.get("wakeups") or {}).get("pending_sample") or []
    for entry in wakeups:
        if not isinstance(entry, dict):
            continue
        repo_id = str(entry.get("repo_id") or "").strip()
        thread_id = str(entry.get("thread_id") or "").strip()
        wakeup_id = str(entry.get("wakeup_id") or "").strip()
        basis_at = entry.get("timestamp")
        freshness = build_freshness_payload(
            generated_at=generated_at,
            stale_threshold_seconds=stale_threshold_seconds,
            candidates=[("automation_wakeup", basis_at)],
        )
        scope_key = (
            f"thread:{thread_id}"
            if thread_id
            else (
                f"run:{entry.get('run_id')}"
                if entry.get("run_id")
                else (f"repo:{repo_id}" if repo_id else f"wakeup:{wakeup_id}")
            )
        )
        items.append(
            {
                "item_type": "automation_wakeup",
                "wakeup_id": wakeup_id or None,
                "source": entry.get("source"),
                "event_type": entry.get("event_type"),
                "subscription_id": entry.get("subscription_id"),
                "timer_id": entry.get("timer_id"),
                "repo_id": repo_id or None,
                "run_id": entry.get("run_id"),
                "thread_id": thread_id or None,
                "lane_id": entry.get("lane_id"),
                "from_state": entry.get("from_state"),
                "to_state": entry.get("to_state"),
                "reason": entry.get("reason"),
                "timestamp": basis_at,
                "action_queue_id": f"automation_wakeup:{wakeup_id or scope_key}",
                "queue_source": "automation_wakeup",
                "precedence": {"rank": rank, "label": label},
                "why_selected": "Pending automation wakeup signals follow-up work",
                "recommended_action": "handle_automation_wakeup",
                "recommended_detail": (
                    f"Continue lane {entry.get('lane_id')}"
                    if entry.get("lane_id")
                    else "Inspect the pending PMA automation wakeup"
                ),
                "operator_need_rank": 5,
                "freshness": freshness,
                "scope": {
                    "kind": "wakeup",
                    "key": scope_key,
                },
                "sort_timestamp": _timestamp_sort_value(basis_at),
            }
        )
    return items


def _is_strong_action_queue_item(item: Mapping[str, Any]) -> bool:
    if bool(item.get("likely_false_positive")):
        return False

    item_type = str(item.get("item_type") or "").strip().lower()
    followup_state = str(item.get("followup_state") or "").strip().lower()
    operator_need = str(item.get("operator_need") or "").strip().lower()

    if item_type in {
        "managed_thread_followup_summary",
        "pma_file_summary",
    }:
        return False
    if item_type == "managed_thread_followup":
        return followup_state in {"attention_required", "awaiting_followup"}
    if item_type == "pma_file":
        return True
    if item_type == "automation_wakeup":
        return True
    if item.get("queue_source") == "ticket_flow_inbox":
        return True
    return operator_need in {"urgent", "normal"}


def build_pma_action_queue(
    *,
    inbox: list[dict[str, Any]],
    pma_threads: list[dict[str, Any]],
    pma_files_detail: Mapping[str, list[dict[str, Any]]],
    automation: Mapping[str, Any],
    generated_at: Optional[str] = None,
    stale_threshold_seconds: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Build one operator-facing queue across inbox, threads, files, and wakeups."""

    resolved_generated_at = generated_at or iso_now()
    resolved_stale_threshold = resolve_stale_threshold_seconds(stale_threshold_seconds)
    items = [
        *_build_ticket_flow_queue_items(inbox),
        *_build_thread_queue_items(pma_threads),
        *_build_file_queue_items(pma_files_detail),
        *_build_automation_queue_items(
            automation,
            generated_at=resolved_generated_at,
            stale_threshold_seconds=resolved_stale_threshold,
        ),
    ]

    items = sorted(
        items,
        key=lambda item: (
            int(((item.get("precedence") or {}).get("rank") or 999)),
            int(item.get("operator_need_rank") or 0),
            -float(item.get("sort_timestamp") or 0.0),
            str(item.get("action_queue_id") or ""),
        ),
    )

    winning_scope: dict[str, dict[str, Any]] = {}
    winning_repo_blocker: dict[str, dict[str, Any]] = {}
    primary_queue_id = next(
        (
            str(item.get("action_queue_id") or "")
            for item in items
            if _is_strong_action_queue_item(item)
        ),
        None,
    )
    for index, item in enumerate(items, start=1):
        scope = item.get("scope") or {}
        scope_key = str(scope.get("key") or "")
        queue_id = str(item.get("action_queue_id") or "")
        repo_key = str(item.get("repo_id") or "").strip()
        repo_blocker = winning_repo_blocker.get(repo_key) if repo_key else None
        scope_winner = winning_scope.get(scope_key) if scope_key else None
        winner = repo_blocker or scope_winner
        if winner is not None:
            item["supersession"] = _queue_supersession_payload(
                status="superseded",
                is_primary=False,
                superseded=True,
                superseded_by=str(winner.get("action_queue_id") or "") or None,
                reason=(
                    "A higher-precedence action already covers the same operator scope"
                ),
            )
        else:
            if scope_key:
                winning_scope[scope_key] = item
            is_primary = bool(primary_queue_id and queue_id == primary_queue_id)
            item["supersession"] = _queue_supersession_payload(
                status="primary" if is_primary else "non_primary",
                is_primary=is_primary,
                superseded=False,
                superseded_by=None,
                reason=(
                    "Highest-priority actionable item in the queue"
                    if is_primary
                    else (
                        "Actionable, but lower priority than the current primary item"
                        if primary_queue_id
                        else "Inventory or hygiene item; no strong next action currently"
                    )
                ),
            )
        if (
            str(item.get("queue_source") or "") == "ticket_flow_inbox"
            and repo_key
            and repo_key not in winning_repo_blocker
        ):
            winning_repo_blocker[repo_key] = item
        item["queue_rank"] = index
        item.pop("sort_timestamp", None)
    return items


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


def _render_freshness_summary(payload: Any, *, max_field_chars: int) -> Optional[str]:
    if not isinstance(payload, Mapping):
        return None
    status = _truncate(str(payload.get("status") or "unknown"), max_field_chars)
    basis = _truncate(str(payload.get("recency_basis") or ""), max_field_chars)
    basis_at = _truncate(str(payload.get("basis_at") or ""), max_field_chars)
    age_raw = payload.get("age_seconds")
    age_text = _truncate(str(age_raw), max_field_chars) if age_raw is not None else ""
    parts = [f"status={status}"]
    if basis:
        parts.append(f"basis={basis}")
    if basis_at:
        parts.append(f"basis_at={basis_at}")
    if age_text:
        parts.append(f"age_seconds={age_text}")
    return " ".join(parts) if parts else None


def _render_destination_summary(
    destination: Any,
    *,
    max_field_chars: int,
) -> str:
    destination_payload = destination if isinstance(destination, Mapping) else {}
    destination_kind = _truncate(
        str(destination_payload.get("kind", "local")),
        max_field_chars,
    )
    destination_text = destination_kind or "local"
    if destination_kind == "docker":
        image = _truncate(
            str(destination_payload.get("image", "")),
            max_field_chars,
        )
        destination_text = f"docker:{image}" if image else "docker:image-missing"
    return destination_text


def _render_resource_owner_summary(
    item: Mapping[str, Any],
    *,
    max_field_chars: int,
) -> str:
    resource_kind = _truncate(str(item.get("resource_kind") or ""), max_field_chars)
    resource_id = _truncate(str(item.get("resource_id") or ""), max_field_chars)
    repo_id = _truncate(str(item.get("repo_id") or ""), max_field_chars)
    if resource_kind and resource_id:
        if resource_kind == "repo":
            return f"repo_id={resource_id}"
        return f"owner={resource_kind}:{resource_id}"
    if repo_id:
        return f"repo_id={repo_id}"
    workspace_root = _truncate(str(item.get("workspace_root") or ""), max_field_chars)
    if workspace_root:
        return f"workspace_root={workspace_root}"
    return "owner=unowned"


def load_pma_prompt(hub_root: Path) -> str:
    path = pma_doc_path(hub_root, "prompt.md")
    try:
        ensure_pma_docs(hub_root)
    except Exception as exc:
        _logger.warning("Could not ensure PMA docs for prompt: %s", exc)
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        _logger.warning("Could not read prompt file: %s", exc)
        return ""


def _render_ticket_flow_summary(summary: Optional[dict[str, Any]]) -> str:
    if not summary:
        return "null"
    status = summary.get("status")
    done_count = summary.get("done_count")
    total_count = summary.get("total_count")
    current_step = summary.get("current_step")
    pr_url = summary.get("pr_url")
    final_review_status = summary.get("final_review_status")
    parts: list[str] = []
    if status is not None:
        parts.append(f"status={status}")
    if done_count is not None and total_count is not None:
        parts.append(f"done={done_count}/{total_count}")
    if current_step is not None:
        parts.append(f"step={current_step}")
    if pr_url:
        parts.append("pr=opened")
    if final_review_status:
        parts.append(f"final_review={final_review_status}")
    if not parts:
        return "null"
    return " ".join(parts)


def _render_hub_snapshot(
    snapshot: dict[str, Any],
    *,
    max_repos: int = PMA_MAX_REPOS,
    max_messages: int = PMA_MAX_MESSAGES,
    max_text_chars: int = PMA_MAX_TEXT,
    max_template_repos: int = PMA_MAX_TEMPLATE_REPOS,
    max_field_chars: int = PMA_MAX_TEMPLATE_FIELD_CHARS,
    max_pma_files: int = PMA_MAX_PMA_FILES,
    max_lifecycle_events: int = PMA_MAX_LIFECYCLE_EVENTS,
    max_pma_threads: int = PMA_MAX_PMA_THREADS,
    max_automation_items: int = PMA_MAX_AUTOMATION_ITEMS,
) -> str:
    lines: list[str] = []

    generated_at = _truncate(str(snapshot.get("generated_at") or ""), max_field_chars)
    snapshot_freshness = snapshot.get("freshness") or {}
    if generated_at or snapshot_freshness:
        lines.append("Snapshot Freshness:")
        threshold = snapshot_freshness.get("stale_threshold_seconds")
        threshold_text = (
            _truncate(str(threshold), max_field_chars) if threshold is not None else ""
        )
        header_parts: list[str] = []
        if generated_at:
            header_parts.append(f"generated_at={generated_at}")
        if threshold_text:
            header_parts.append(f"stale_threshold_seconds={threshold_text}")
        if header_parts:
            lines.append(f"- {' '.join(header_parts)}")
        sections = snapshot_freshness.get("sections") or {}
        if isinstance(sections, Mapping):
            for section_name, payload in sections.items():
                if not isinstance(payload, Mapping):
                    continue
                count = _truncate(
                    str(payload.get("entity_count") or 0), max_field_chars
                )
                stale = _truncate(str(payload.get("stale_count") or 0), max_field_chars)
                newest = _truncate(
                    str(payload.get("newest_basis_at") or ""), max_field_chars
                )
                lines.append(
                    f"- section={_truncate(str(section_name), max_field_chars)} "
                    f"count={count} stale={stale}"
                    + (f" newest_basis_at={newest}" if newest else "")
                )
        lines.append("")

    action_queue = snapshot.get("action_queue") or []
    if action_queue:
        lines.append("PMA Action Queue:")
        has_primary = any(
            (item.get("supersession") or {}).get("status") == "primary"
            for item in action_queue
            if isinstance(item, Mapping)
        )
        if not has_primary:
            lines.append("- No strong next-action item right now")
        for item in list(action_queue)[: max(0, max_messages)]:
            queue_id = _truncate(
                str(item.get("action_queue_id") or ""), max_field_chars
            )
            source = _truncate(str(item.get("queue_source") or ""), max_field_chars)
            queue_rank = _truncate(str(item.get("queue_rank") or ""), max_field_chars)
            item_type = _truncate(str(item.get("item_type") or ""), max_field_chars)
            item_name = _truncate(str(item.get("name") or ""), max_field_chars)
            repo_id = _truncate(str(item.get("repo_id") or ""), max_field_chars)
            run_id = _truncate(str(item.get("run_id") or ""), max_field_chars)
            managed_thread_id = _truncate(
                str(item.get("managed_thread_id") or item.get("thread_id") or ""),
                max_field_chars,
            )
            file_name = (
                _truncate(str(item.get("name") or ""), max_field_chars)
                if item.get("item_type") == "pma_file"
                else ""
            )
            recommended_action = _truncate(
                str(item.get("recommended_action") or ""), max_field_chars
            )
            followup_state = _truncate(
                str(item.get("followup_state") or ""), max_field_chars
            )
            operator_need = _truncate(
                str(item.get("operator_need") or ""), max_field_chars
            )
            thread_count = _truncate(
                str(item.get("thread_count") or ""), max_field_chars
            )
            file_count = _truncate(str(item.get("file_count") or ""), max_field_chars)
            precedence = item.get("precedence") or {}
            precedence_rank = _truncate(
                str(precedence.get("rank") or ""), max_field_chars
            )
            precedence_label = _truncate(
                str(precedence.get("label") or ""), max_field_chars
            )
            supersession = item.get("supersession") or {}
            supersession_status = _truncate(
                str(supersession.get("status") or ""), max_field_chars
            )
            superseded_by = _truncate(
                str(supersession.get("superseded_by") or ""), max_field_chars
            )
            lines.append(
                f"- rank={queue_rank} source={source} precedence={precedence_rank}:{precedence_label} "
                f"status={supersession_status} item_type={item_type} id={queue_id}"
                + (f" repo_id={repo_id}" if repo_id else "")
                + (f" run_id={run_id}" if run_id else "")
                + (
                    f" managed_thread_id={managed_thread_id}"
                    if managed_thread_id
                    else ""
                )
                + (f" name={item_name}" if item_name and item_name != file_name else "")
                + (f" file={file_name}" if file_name else "")
                + (f" followup_state={followup_state}" if followup_state else "")
                + (f" operator_need={operator_need}" if operator_need else "")
                + (f" thread_count={thread_count}" if thread_count else "")
                + (f" file_count={file_count}" if file_count else "")
                + (
                    f" recommended_action={recommended_action}"
                    if recommended_action
                    else ""
                )
            )
            why_selected = item.get("why_selected")
            if why_selected:
                lines.append(
                    f"  why_selected: {_truncate(str(why_selected), max_text_chars)}"
                )
            recommended_detail = item.get("recommended_detail")
            if recommended_detail:
                lines.append(
                    "  recommended_detail: "
                    f"{_truncate(str(recommended_detail), max_text_chars)}"
                )
            drilldown_commands = item.get("drilldown_commands")
            if isinstance(drilldown_commands, Sequence):
                visible_commands = [
                    _truncate(str(command), max_text_chars)
                    for command in drilldown_commands
                    if str(command).strip()
                ]
                if visible_commands:
                    lines.append(f"  drilldown: {', '.join(visible_commands)}")
            if superseded_by:
                lines.append(f"  superseded_by: {superseded_by}")
            supersession_reason = supersession.get("reason")
            if supersession_reason:
                lines.append(
                    "  supersession_reason: "
                    f"{_truncate(str(supersession_reason), max_text_chars)}"
                )
            freshness_summary = _render_freshness_summary(
                _extract_entry_freshness(item), max_field_chars=max_field_chars
            )
            if freshness_summary:
                lines.append(f"  freshness: {freshness_summary}")
        lines.append("")

    inbox = snapshot.get("inbox") or []
    if inbox:
        lines.append("Run Dispatches (paused runs needing attention):")
        for item in list(inbox)[: max(0, max_messages)]:
            item_type = _truncate(
                str(item.get("item_type", "run_dispatch")), max_field_chars
            )
            next_action = _truncate(
                str(item.get("next_action", "reply_and_resume")), max_field_chars
            )
            repo_id = _truncate(str(item.get("repo_id", "")), max_field_chars)
            run_id = _truncate(str(item.get("run_id", "")), max_field_chars)
            seq = _truncate(str(item.get("seq", "")), max_field_chars)
            dispatch = item.get("dispatch") or {}
            mode = _truncate(str(dispatch.get("mode", "")), max_field_chars)
            handoff = bool(dispatch.get("is_handoff"))
            run_state = item.get("run_state") or {}
            state = _truncate(str(run_state.get("state", "")), max_field_chars)
            current_ticket = _truncate(
                str(run_state.get("current_ticket", "")), max_field_chars
            )
            last_progress_at = _truncate(
                str(run_state.get("last_progress_at", "")), max_field_chars
            )
            duration = _truncate(
                str(
                    format_flow_duration(
                        cast(Optional[float], run_state.get("duration_seconds"))
                    )
                    or ""
                ),
                max_field_chars,
            )
            lines.append(
                f"- type={item_type} next_action={next_action} repo_id={repo_id} "
                f"run_id={run_id} seq={seq} mode={mode} handoff={str(handoff).lower()} "
                f"state={state} current_ticket={current_ticket} last_progress_at={last_progress_at}"
                + (f" duration={duration}" if duration else "")
            )
            title = dispatch.get("title")
            if title:
                lines.append(f"  title: {_truncate(str(title), max_text_chars)}")
            body = dispatch.get("body")
            if body:
                lines.append(f"  body: {_truncate(str(body), max_text_chars)}")
            files = item.get("files") or []
            if files:
                display = [
                    _truncate(str(name), max_field_chars)
                    for name in list(files)[: max(0, max_pma_files)]
                ]
                lines.append(f"  attachments: [{', '.join(display)}]")
            open_url = item.get("open_url")
            if open_url:
                lines.append(f"  open_url: {_truncate(str(open_url), max_field_chars)}")
            blocking_reason = run_state.get("blocking_reason")
            if blocking_reason:
                lines.append(
                    f"  blocking_reason: {_truncate(str(blocking_reason), max_text_chars)}"
                )
            run_recommended_action = run_state.get("recommended_action")
            if run_recommended_action:
                lines.append(
                    "  recommended_action: "
                    f"{_truncate(str(run_recommended_action), max_text_chars)}"
                )
            freshness_summary = _render_freshness_summary(
                _extract_entry_freshness(item), max_field_chars=max_field_chars
            )
            if freshness_summary:
                lines.append(f"  freshness: {freshness_summary}")
        lines.append("")

    repos = snapshot.get("repos") or []
    if repos:
        lines.append("Repos:")
        for repo in list(repos)[: max(0, max_repos)]:
            repo_id = _truncate(str(repo.get("id", "")), max_field_chars)
            display_name = _truncate(str(repo.get("display_name", "")), max_field_chars)
            status = _truncate(str(repo.get("status", "")), max_field_chars)
            last_run_id = _truncate(str(repo.get("last_run_id", "")), max_field_chars)
            last_exit = _truncate(str(repo.get("last_exit_code", "")), max_field_chars)
            destination_text = _render_destination_summary(
                repo.get("effective_destination"),
                max_field_chars=max_field_chars,
            )
            ticket_flow = _render_ticket_flow_summary(repo.get("ticket_flow"))
            run_state = repo.get("run_state") or {}
            state = _truncate(str(run_state.get("state", "")), max_field_chars)
            blocking_reason = _truncate(
                str(run_state.get("blocking_reason", "")), max_text_chars
            )
            recommended_action = _truncate(
                str(run_state.get("recommended_action", "")), max_text_chars
            )
            duration = _truncate(
                str(
                    format_flow_duration(
                        cast(Optional[float], run_state.get("duration_seconds"))
                    )
                    or ""
                ),
                max_field_chars,
            )
            lines.append(
                f"- {repo_id} ({display_name}): status={status} "
                f"destination={destination_text} "
                f"last_run_id={last_run_id} last_exit_code={last_exit} "
                f"ticket_flow={ticket_flow} state={state}"
                + (f" duration={duration}" if duration else "")
            )
            if blocking_reason:
                lines.append(f"  blocking_reason: {blocking_reason}")
            if recommended_action:
                lines.append(f"  recommended_action: {recommended_action}")
            freshness_summary = _render_freshness_summary(
                _extract_entry_freshness(repo), max_field_chars=max_field_chars
            )
            if freshness_summary:
                lines.append(f"  freshness: {freshness_summary}")
        lines.append("")

    agent_workspaces = snapshot.get("agent_workspaces") or []
    if agent_workspaces:
        lines.append("Agent Workspaces:")
        for workspace in list(agent_workspaces)[: max(0, max_repos)]:
            workspace_id = _truncate(str(workspace.get("id", "")), max_field_chars)
            display_name = _truncate(
                str(workspace.get("display_name", "")),
                max_field_chars,
            )
            runtime = _truncate(str(workspace.get("runtime", "")), max_field_chars)
            enabled = str(bool(workspace.get("enabled"))).lower()
            exists_on_disk = str(bool(workspace.get("exists_on_disk"))).lower()
            destination_text = _render_destination_summary(
                workspace.get("effective_destination"),
                max_field_chars=max_field_chars,
            )
            lines.append(
                f"- {workspace_id} ({display_name}): runtime={runtime} "
                f"destination={destination_text} enabled={enabled} "
                f"exists_on_disk={exists_on_disk}"
            )
            freshness_summary = _render_freshness_summary(
                workspace.get("freshness"), max_field_chars=max_field_chars
            )
            if freshness_summary:
                lines.append(f"  freshness: {freshness_summary}")
        lines.append("")

    templates = snapshot.get("templates") or {}
    template_repos = templates.get("repos") or []
    template_scan = templates.get("last_scan")
    if templates.get("enabled") or template_repos or template_scan:
        templates_enabled = bool(templates.get("enabled"))
        lines.append("Templates:")
        lines.append(f"- enabled={str(templates_enabled).lower()}")
        if template_repos:
            items: list[str] = []
            for repo in list(template_repos)[: max(0, max_template_repos)]:
                repo_id = _truncate(str(repo.get("id", "")), max_field_chars)
                default_ref = _truncate(
                    str(repo.get("default_ref", "")), max_field_chars
                )
                trusted = bool(repo.get("trusted"))
                items.append(f"{repo_id}@{default_ref} trusted={str(trusted).lower()}")
            lines.append(f"- repos: [{', '.join(items)}]")
        if template_scan:
            repo_id = _truncate(str(template_scan.get("repo_id", "")), max_field_chars)
            decision = _truncate(
                str(template_scan.get("decision", "")), max_field_chars
            )
            severity = _truncate(
                str(template_scan.get("severity", "")), max_field_chars
            )
            scanned_at = _truncate(
                str(template_scan.get("scanned_at", "")), max_field_chars
            )
            lines.append(
                f"- last_scan: {repo_id} {decision} {severity} {scanned_at}".strip()
            )
        lines.append("")

    pma_files = snapshot.get("pma_files") or {}
    inbox_files = pma_files.get("inbox") or []
    outbox_files = pma_files.get("outbox") or []
    pma_files_detail = snapshot.get("pma_files_detail") or {}
    if inbox_files or outbox_files:
        if inbox_files:
            lines.append("PMA File Inbox:")
            files = [
                _truncate(str(name), max_field_chars)
                for name in list(inbox_files)[: max(0, max_pma_files)]
            ]
            lines.append(f"- inbox: [{', '.join(files)}]")
            inbox_detail = [
                entry
                for entry in (pma_files_detail.get("inbox") or [])
                if isinstance(entry, Mapping)
            ]
            visible_inbox_detail = inbox_detail[: max(0, max_pma_files)]
            fresh_files = [
                _truncate(str(entry.get("name") or ""), max_field_chars)
                for entry in visible_inbox_detail
                if str(entry.get("next_action") or "") == PMA_FILE_NEXT_ACTION_PROCESS
            ]
            stale_files = [
                _truncate(str(entry.get("name") or ""), max_field_chars)
                for entry in visible_inbox_detail
                if str(entry.get("next_action") or "")
                == PMA_FILE_NEXT_ACTION_REVIEW_STALE
            ]
            if fresh_files:
                lines.append(
                    "- next_action: "
                    f"{PMA_FILE_NEXT_ACTION_PROCESS} "
                    f"(fresh_uploads=[{', '.join(fresh_files)}])"
                )
            if stale_files:
                lines.append(
                    "- next_action: "
                    f"{PMA_FILE_NEXT_ACTION_REVIEW_STALE} "
                    f"(likely_leftovers=[{', '.join(stale_files)}])"
                )
                lines.append(
                    "- note: stale inbox files are usually false positives from prior "
                    "work that was never cleared"
                )
        if outbox_files:
            lines.append("PMA File Outbox:")
            files = [
                _truncate(str(name), max_field_chars)
                for name in list(outbox_files)[: max(0, max_pma_files)]
            ]
            lines.append(f"- outbox: [{', '.join(files)}]")
        lines.append("")

    pma_threads = snapshot.get("pma_threads") or []
    if pma_threads:
        lines.append("PMA Managed Threads:")
        for thread in list(pma_threads)[: max(0, max_pma_threads)]:
            managed_thread_id = _truncate(
                str(thread.get("managed_thread_id", "")),
                max_field_chars,
            )
            owner_summary = _render_resource_owner_summary(
                thread,
                max_field_chars=max_field_chars,
            )
            agent = _truncate(str(thread.get("agent") or ""), max_field_chars)
            raw_status = str(thread.get("status") or "")
            lifecycle_status = str(thread.get("lifecycle_status") or "-")
            operator_status = derive_managed_thread_operator_status(
                normalized_status=raw_status,
                lifecycle_status=lifecycle_status,
            )
            status_display = _truncate(operator_status, max_field_chars)
            last_turn_outcome = _truncate(
                (
                    raw_status
                    if raw_status in {"completed", "interrupted", "failed"}
                    else "-"
                ),
                max_field_chars,
            )
            status_reason = _truncate(
                str(thread.get("status_reason") or "-"),
                max_field_chars,
            )
            name = _truncate(str(thread.get("name") or "-"), max_field_chars)
            preview = _truncate(
                str(thread.get("last_message_preview") or "-"),
                max_field_chars,
            )
            lines.append(
                f"- {managed_thread_id} {owner_summary} agent={agent} "
                f"status={status_display} last_turn={last_turn_outcome} "
                f"reason={status_reason} name={name} last={preview}"
                + (" chat_bound=true" if bool(thread.get("chat_bound")) else "")
                + (
                    f" binding_kind={_truncate(str(thread.get('binding_kind') or ''), max_field_chars)}"
                    if thread.get("binding_kind")
                    else ""
                )
                + (
                    f" binding_id={_truncate(str(thread.get('binding_id') or ''), max_field_chars)}"
                    if thread.get("binding_id")
                    else ""
                )
                + (
                    " cleanup_protected=true"
                    if bool(thread.get("cleanup_protected"))
                    else ""
                )
            )
            freshness_summary = _render_freshness_summary(
                thread.get("freshness"), max_field_chars=max_field_chars
            )
            if freshness_summary:
                lines.append(f"  freshness: {freshness_summary}")
        lines.append("")

    automation = snapshot.get("automation") or {}
    subscriptions = automation.get("subscriptions") or {}
    timers = automation.get("timers") or {}
    wakeups = automation.get("wakeups") or {}
    subscriptions_active = int(subscriptions.get("active_count") or 0)
    timers_pending = int(timers.get("pending_count") or 0)
    wakeups_pending = int(wakeups.get("pending_count") or 0)
    wakeups_dispatched = int(wakeups.get("dispatched_recent_count") or 0)
    if automation:
        lines.append("PMA Automation:")
        lines.append(
            "- subscriptions_active="
            f"{subscriptions_active} timers_pending={timers_pending} "
            f"wakeups_pending={wakeups_pending} wakeups_dispatched_recent={wakeups_dispatched}"
        )

        subscription_sample = subscriptions.get("sample") or []
        if subscription_sample:
            lines.append("- subscriptions_sample:")
            for item in list(subscription_sample)[: max(0, max_automation_items)]:
                if not isinstance(item, dict):
                    continue
                sub_id = _truncate(
                    str(item.get("subscription_id", "")), max_field_chars
                )
                event_types = item.get("event_types")
                event_types_text = (
                    ", ".join(
                        _truncate(str(entry), max_field_chars)
                        for entry in event_types
                        if entry is not None
                    )
                    if isinstance(event_types, list)
                    else _truncate(str(event_types or ""), max_field_chars)
                )
                lane_id = _truncate(str(item.get("lane_id") or ""), max_field_chars)
                repo_id = _truncate(str(item.get("repo_id") or "-"), max_field_chars)
                run_id = _truncate(str(item.get("run_id") or "-"), max_field_chars)
                thread_id = _truncate(
                    str(item.get("thread_id") or "-"), max_field_chars
                )
                from_state = _truncate(
                    str(item.get("from_state") or "-"), max_field_chars
                )
                to_state = _truncate(str(item.get("to_state") or "-"), max_field_chars)
                lines.append(
                    "  - id="
                    f"{sub_id} events=[{event_types_text}] repo_id={repo_id} run_id={run_id} "
                    f"thread_id={thread_id} lane_id={lane_id} from={from_state} to={to_state}"
                )

        timer_sample = timers.get("sample") or []
        if timer_sample:
            lines.append("- timers_sample:")
            for item in list(timer_sample)[: max(0, max_automation_items)]:
                if not isinstance(item, dict):
                    continue
                timer_id = _truncate(str(item.get("timer_id") or ""), max_field_chars)
                timer_type = _truncate(
                    str(item.get("timer_type") or ""), max_field_chars
                )
                due_at = _truncate(str(item.get("due_at") or ""), max_field_chars)
                lane_id = _truncate(str(item.get("lane_id") or ""), max_field_chars)
                repo_id = _truncate(str(item.get("repo_id") or "-"), max_field_chars)
                run_id = _truncate(str(item.get("run_id") or "-"), max_field_chars)
                thread_id = _truncate(
                    str(item.get("thread_id") or "-"), max_field_chars
                )
                lines.append(
                    "  - id="
                    f"{timer_id} type={timer_type} due_at={due_at} repo_id={repo_id} "
                    f"run_id={run_id} thread_id={thread_id} lane_id={lane_id}"
                )

        wakeup_sample = wakeups.get("pending_sample") or []
        if wakeup_sample:
            lines.append("- pending_wakeups_sample:")
            for item in list(wakeup_sample)[: max(0, max_automation_items)]:
                if not isinstance(item, dict):
                    continue
                wakeup_id = _truncate(str(item.get("wakeup_id") or ""), max_field_chars)
                source = _truncate(str(item.get("source") or ""), max_field_chars)
                event_type = _truncate(
                    str(item.get("event_type") or ""), max_field_chars
                )
                timer_id = _truncate(str(item.get("timer_id") or "-"), max_field_chars)
                subscription_id = _truncate(
                    str(item.get("subscription_id") or "-"), max_field_chars
                )
                lane_id = _truncate(str(item.get("lane_id") or ""), max_field_chars)
                to_state = _truncate(str(item.get("to_state") or "-"), max_field_chars)
                reason = _truncate(str(item.get("reason") or "-"), max_field_chars)
                lines.append(
                    "  - id="
                    f"{wakeup_id} source={source} event_type={event_type} "
                    f"subscription_id={subscription_id} timer_id={timer_id} "
                    f"lane_id={lane_id} to={to_state} reason={reason}"
                )
        if not subscription_sample and not timer_sample and not wakeup_sample:
            lines.append(
                "- no automation configured; create rules via "
                "/hub/pma/subscriptions and /hub/pma/timers"
            )
        lines.append("")

    lifecycle_events = snapshot.get("lifecycle_events") or []
    if lifecycle_events:
        lines.append("Lifecycle events (recent):")
        for event in list(lifecycle_events)[: max(0, max_lifecycle_events)]:
            timestamp = _truncate(str(event.get("timestamp", "")), max_field_chars)
            event_type = _truncate(str(event.get("event_type", "")), max_field_chars)
            repo_id = _truncate(str(event.get("repo_id", "")), max_field_chars)
            run_id = _truncate(str(event.get("run_id", "")), max_field_chars)
            lines.append(
                f"- {timestamp} {event_type} repo_id={repo_id} run_id={run_id}"
            )
        lines.append("")

    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


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
        except Exception as exc:
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
        except Exception as exc:
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


def _get_ticket_flow_summary(repo_path: Path) -> Optional[dict[str, Any]]:
    return build_ticket_flow_summary(repo_path, include_failure=False)


def _resolve_workspace_root(record_input: dict[str, Any], repo_root: Path) -> Path:
    return resolve_ticket_flow_workspace_root(
        record_input,
        repo_root,
        enforce_repo_boundary=True,
    )


def _latest_reply_history_seq(
    repo_root: Path, run_id: str, record_input: dict[str, Any]
) -> int:
    try:
        workspace_root = _resolve_workspace_root(record_input, repo_root)
        reply_paths = resolve_reply_paths(workspace_root=workspace_root, run_id=run_id)
        history_dir = reply_paths.reply_history_dir
        if not history_dir.exists() or not history_dir.is_dir():
            return 0
        latest = 0
        for child in history_dir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if len(name) == 4 and name.isdigit():
                latest = max(latest, int(name))
        return latest
    except Exception as exc:
        _logger.warning("Could not get latest reply history seq: %s", exc)
        return 0


def _dispatch_dict(dispatch: Dispatch, *, max_text_chars: int) -> dict[str, Any]:
    return {
        "mode": dispatch.mode,
        "title": _truncate(dispatch.title, max_text_chars),
        "body": _truncate(dispatch.body, max_text_chars),
        "extra": _trim_extra(dispatch.extra, max_text_chars),
        "is_handoff": dispatch.is_handoff,
    }


def _dispatch_is_actionable(dispatch_payload: Any) -> bool:
    if not isinstance(dispatch_payload, dict):
        return False
    if bool(dispatch_payload.get("is_handoff")):
        return True
    mode = str(dispatch_payload.get("mode") or "").strip().lower()
    return mode == "pause"


def _paused_dispatch_resume_invalid_reason(repo_root: Path) -> Optional[str]:
    preflight = ticket_flow_inbox_preflight(repo_root)
    if preflight.is_recoverable:
        return None
    if preflight.reason_code != "no_tickets":
        return None
    return (
        "Latest dispatch is stale; ticket flow resume preflight would fail because "
        f"no tickets remain in {safe_relpath(repo_root / '.codex-autorunner' / 'tickets', repo_root)}"
    )


def _ticket_flow_recommended_actions(
    *,
    state: str,
    record_status: FlowRunStatus,
    has_pending_dispatch: bool,
    status_cmd: str,
    resume_cmd: str,
    start_cmd: str,
    stop_cmd: str,
) -> list[str]:
    if state == "completed":
        return [start_cmd]
    if state == "dead":
        return [f"{resume_cmd} --force", status_cmd, stop_cmd]
    if record_status == FlowRunStatus.PAUSED:
        if has_pending_dispatch:
            return [resume_cmd, status_cmd, stop_cmd]
        return [f"{resume_cmd} --force", status_cmd, stop_cmd]
    if state == "blocked":
        return [f"{resume_cmd} --force", status_cmd, stop_cmd]
    return [status_cmd]


def _resolve_paused_dispatch_state(
    *,
    repo_root: Path,
    record_status: FlowRunStatus,
    latest_payload: Mapping[str, Any],
    latest_reply_seq: int,
) -> tuple[bool, Optional[str]]:
    seq = int(latest_payload.get("seq") or 0)
    latest_seq = int(latest_payload.get("latest_seq") or 0)
    dispatch_payload = latest_payload.get("dispatch")
    dispatch_is_actionable = _dispatch_is_actionable(dispatch_payload)
    has_dispatch = bool(dispatch_is_actionable and seq > 0 and latest_reply_seq < seq)
    if record_status == FlowRunStatus.PAUSED and has_dispatch and latest_seq > seq:
        preflight_invalid_reason = _paused_dispatch_resume_invalid_reason(repo_root)
        if preflight_invalid_reason:
            return False, preflight_invalid_reason

    if record_status != FlowRunStatus.PAUSED or has_dispatch:
        return has_dispatch, None

    if latest_payload.get("errors"):
        return False, "Paused run has unreadable dispatch metadata"
    if dispatch_is_actionable and seq > 0 and latest_reply_seq >= seq:
        return False, "Latest dispatch already replied; run is still paused"
    if (
        dispatch_payload
        and not dispatch_is_actionable
        and seq > 0
        and latest_reply_seq < seq
    ):
        return False, "Latest dispatch is informational and does not require reply"
    return False, "Run is paused without an actionable dispatch"


def _latest_dispatch(
    repo_root: Path, run_id: str, input_data: dict, *, max_text_chars: int
) -> Optional[dict[str, Any]]:
    try:
        workspace_root = _resolve_workspace_root(input_data, repo_root)
        outbox_paths = resolve_outbox_paths(
            workspace_root=workspace_root, run_id=run_id
        )
        history_dir = outbox_paths.dispatch_history_dir
        if not history_dir.exists() or not history_dir.is_dir():
            return None
        seq_dirs: list[Path] = []
        for child in history_dir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if len(name) == 4 and name.isdigit():
                seq_dirs.append(child)
        if not seq_dirs:
            return None

        def _list_files(dispatch_dir: Path) -> list[str]:
            files: list[str] = []
            for child in sorted(dispatch_dir.iterdir(), key=lambda p: p.name):
                if child.name.startswith("."):
                    continue
                if child.name == "DISPATCH.md":
                    continue
                if child.is_file():
                    files.append(child.name)
            return files

        seq_dirs = sorted(seq_dirs, key=lambda p: p.name, reverse=True)
        latest_seq = int(seq_dirs[0].name) if seq_dirs else None
        handoff_candidate: Optional[dict[str, Any]] = None
        non_summary_candidate: Optional[dict[str, Any]] = None
        turn_summary_candidate: Optional[dict[str, Any]] = None
        error_candidate: Optional[dict[str, Any]] = None

        for seq_dir in seq_dirs:
            seq = int(seq_dir.name)
            dispatch_path = seq_dir / "DISPATCH.md"
            dispatch, errors = parse_dispatch(dispatch_path)
            if errors or dispatch is None:
                # Fail closed: if the newest dispatch is unreadable, surface that
                # corruption instead of silently falling back to older prompts.
                if latest_seq is not None and seq == latest_seq:
                    return {
                        "seq": seq,
                        "dir": safe_relpath(seq_dir, repo_root),
                        "dispatch": None,
                        "errors": errors,
                        "files": [],
                    }
                if error_candidate is None:
                    error_candidate = {"seq": seq, "dir": seq_dir, "errors": errors}
                continue
            candidate = {"seq": seq, "dir": seq_dir, "dispatch": dispatch}
            if dispatch.is_handoff and handoff_candidate is None:
                handoff_candidate = candidate
            if dispatch.mode != "turn_summary" and non_summary_candidate is None:
                non_summary_candidate = candidate
            if dispatch.mode == "turn_summary" and turn_summary_candidate is None:
                turn_summary_candidate = candidate
            if handoff_candidate and non_summary_candidate and turn_summary_candidate:
                break

        selected = handoff_candidate or non_summary_candidate or turn_summary_candidate
        if not selected:
            if error_candidate:
                return {
                    "seq": error_candidate["seq"],
                    "dir": safe_relpath(error_candidate["dir"], repo_root),
                    "dispatch": None,
                    "errors": error_candidate["errors"],
                    "files": [],
                }
            return None

        selected_dir = selected["dir"]
        selected_dispatch = selected["dispatch"]
        return {
            "seq": selected["seq"],
            "latest_seq": latest_seq,
            "dir": safe_relpath(selected_dir, repo_root),
            "dispatch": _dispatch_dict(
                selected_dispatch, max_text_chars=max_text_chars
            ),
            "errors": [],
            "files": _list_files(selected_dir),
        }
    except Exception as exc:
        _logger.warning("Could not get latest dispatch: %s", exc)
        return None


def build_ticket_flow_run_state(
    *,
    repo_root: Path,
    repo_id: str,
    record: FlowRunRecord,
    store: FlowStore,
    has_pending_dispatch: bool,
    dispatch_state_reason: Optional[str] = None,
) -> TicketFlowRunState:
    run_id = str(record.id)
    quoted_repo = shlex.quote(str(repo_root))
    status_cmd = f"car flow ticket_flow status --repo {quoted_repo} --run-id {run_id}"
    resume_cmd = f"car flow ticket_flow resume --repo {quoted_repo} --run-id {run_id}"
    start_cmd = f"car flow ticket_flow start --repo {quoted_repo}"
    stop_cmd = f"car flow ticket_flow stop --repo {quoted_repo} --run-id {run_id}"

    failure_payload = get_failure_payload(record)
    failure_summary = (
        format_failure_summary(failure_payload) if failure_payload is not None else None
    )
    state_payload = record.state if isinstance(record.state, Mapping) else {}
    reason_summary = state_payload.get("reason_summary")
    if not isinstance(reason_summary, str):
        reason_summary = None
    if reason_summary:
        reason_summary = reason_summary.strip() or None
    error_message = (
        record.error_message.strip()
        if isinstance(record.error_message, str) and record.error_message.strip()
        else None
    )

    current_ticket = store.get_latest_step_progress_current_ticket(run_id)
    if not current_ticket:
        engine = state_payload.get("ticket_engine")
        if isinstance(engine, dict):
            candidate = engine.get("current_ticket")
            if isinstance(candidate, str) and candidate.strip():
                current_ticket = candidate.strip()

    _, last_event_at = store.get_last_event_meta(run_id)
    last_progress_at = (
        last_event_at or record.started_at or record.created_at or record.finished_at
    )
    duration_seconds = flow_run_duration_seconds(record)

    health = None
    dead_worker = False
    if record.status in (
        FlowRunStatus.PAUSED,
        FlowRunStatus.RUNNING,
        FlowRunStatus.STOPPING,
    ):
        try:
            health = check_worker_health(repo_root, run_id)
            dead_worker = health.status in {"dead", "invalid", "mismatch"}
        except Exception as exc:
            _logger.warning("Could not check worker health: %s", exc)
            health = None
            dead_worker = False

    crash_info = None
    crash_summary = None
    if dead_worker:
        try:
            crash_info = read_worker_crash_info(repo_root, run_id)
        except Exception as exc:
            _logger.warning("Could not read worker crash info: %s", exc)
            crash_info = None
        if isinstance(crash_info, dict):
            parts: list[str] = []
            exception = crash_info.get("exception")
            if isinstance(exception, str) and exception.strip():
                parts.append(exception.strip())
            last_event = crash_info.get("last_event")
            if isinstance(last_event, str) and last_event.strip():
                parts.append(f"last_event={last_event.strip()}")
            exit_code = crash_info.get("exit_code")
            if isinstance(exit_code, int):
                parts.append(f"exit_code={exit_code}")
            signal = crash_info.get("signal")
            if isinstance(signal, str) and signal.strip():
                parts.append(f"signal={signal.strip()}")
            if parts:
                crash_summary = " | ".join(parts)

    state = "running"
    if record.status == FlowRunStatus.COMPLETED:
        state = "completed"
    elif dead_worker:
        state = "dead"
    elif record.status == FlowRunStatus.PAUSED:
        state = "paused" if has_pending_dispatch else "blocked"
    elif record.status in (FlowRunStatus.FAILED, FlowRunStatus.STOPPED):
        state = "blocked"

    is_terminal = record.status.is_terminal()
    attention_required = not is_terminal and (
        state in ("dead", "blocked") or record.status == FlowRunStatus.PAUSED
    )

    worker_status = None
    if is_terminal:
        worker_status = "exited_expected"
    elif dead_worker:
        worker_status = "dead_unexpected"
    elif health is not None and health.is_alive:
        worker_status = "alive"

    blocking_reason = None
    if state == "dead":
        detail = crash_summary or (health.message if health is not None else None)
        blocking_reason = (
            f"Worker not running ({detail})"
            if isinstance(detail, str) and detail.strip()
            else "Worker not running"
        )
    elif state == "blocked":
        blocking_reason = (
            dispatch_state_reason
            or failure_summary
            or reason_summary
            or error_message
            or "Run is blocked and needs operator attention"
        )
    elif record.status == FlowRunStatus.PAUSED:
        blocking_reason = reason_summary or "Waiting for user input"

    recommended_actions = _ticket_flow_recommended_actions(
        state=state,
        record_status=record.status,
        has_pending_dispatch=has_pending_dispatch,
        status_cmd=status_cmd,
        resume_cmd=resume_cmd,
        start_cmd=start_cmd,
        stop_cmd=stop_cmd,
    )

    return {
        "state": state,
        "blocking_reason": blocking_reason,
        "current_ticket": current_ticket,
        "last_progress_at": last_progress_at,
        "recommended_action": recommended_actions[0] if recommended_actions else None,
        "recommended_actions": recommended_actions,
        "attention_required": attention_required,
        "worker_status": worker_status,
        "crash": (
            {
                "summary": crash_summary,
                "open_url": f"/repos/{repo_id}/api/flows/{run_id}/artifact?kind=worker_crash",
                "path": f".codex-autorunner/flows/{run_id}/crash.json",
            }
            if isinstance(crash_info, dict)
            else None
        ),
        "flow_status": record.status.value,
        "duration_seconds": duration_seconds,
        "repo_id": repo_id,
        "run_id": run_id,
    }


def get_latest_ticket_flow_run_state_with_record(
    repo_root: Path, repo_id: str
) -> tuple[Optional[TicketFlowRunState], Optional[FlowRunRecord]]:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None, None
    try:
        config = load_repo_config(repo_root)
        with FlowStore(db_path, durable=config.durable_writes) as store:
            records = store.list_flow_runs(flow_type="ticket_flow")
            if not records:
                return None, None
            record = select_authoritative_run_record(records)
            if record is None:
                return None, None
            latest = _latest_dispatch(
                repo_root,
                str(record.id),
                dict(record.input_data or {}),
                max_text_chars=PMA_MAX_TEXT,
            )
            reply_seq = _latest_reply_history_seq(
                repo_root, str(record.id), dict(record.input_data or {})
            )
            latest_payload = latest if isinstance(latest, dict) else {}
            has_dispatch, reason = _resolve_paused_dispatch_state(
                repo_root=repo_root,
                record_status=record.status,
                latest_payload=latest_payload,
                latest_reply_seq=reply_seq,
            )
            run_state = build_ticket_flow_run_state(
                repo_root=repo_root,
                repo_id=repo_id,
                record=record,
                store=store,
                has_pending_dispatch=has_dispatch,
                dispatch_state_reason=reason,
            )
            return run_state, record
    except Exception as exc:
        _logger.warning(
            "Failed to get latest ticket flow run state for repo %s: %s", repo_id, exc
        )
        return None, None


def _ticket_flow_inbox_item_type_and_next_action(
    *, repo_root: Path, record: FlowRunRecord
) -> tuple[str, str]:
    if record.status == FlowRunStatus.RUNNING:
        health = check_worker_health(repo_root, str(record.id))
        if health.status in {"dead", "invalid", "mismatch"}:
            return "worker_dead", "restart_worker"
    if record.status == FlowRunStatus.FAILED:
        return "run_failed", "diagnose_or_restart"
    if record.status == FlowRunStatus.STOPPED:
        return "run_stopped", "diagnose_or_restart"
    return "run_state_attention", "inspect_and_resume"


def _gather_inbox(
    supervisor: HubSupervisor,
    *,
    max_text_chars: int,
    stale_threshold_seconds: Optional[int] = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    stale_threshold_seconds = resolve_stale_threshold_seconds(stale_threshold_seconds)
    try:
        snapshots = supervisor.list_repos()
    except Exception as exc:
        _logger.warning("Could not list repos for inbox: %s", exc)
        return []
    for snap in snapshots:
        if not (snap.initialized and snap.exists_on_disk):
            continue
        repo_root = snap.path
        db_path = repo_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            continue
        try:
            config = load_repo_config(repo_root)
            with FlowStore(db_path, durable=config.durable_writes) as store:
                dismissals = load_hub_inbox_dismissals(repo_root)
                active_statuses = [
                    FlowRunStatus.PAUSED,
                    FlowRunStatus.RUNNING,
                    FlowRunStatus.FAILED,
                    FlowRunStatus.STOPPED,
                ]
                all_runs = store.list_flow_runs(flow_type="ticket_flow")
                newest_record = select_authoritative_run_record(
                    all_runs, preferred_run_id=str(snap.last_run_id or "")
                )
                newest_run_id = str(newest_record.id) if newest_record else None
                active_run_id: Optional[str] = None
                if newest_record and newest_record.status in (
                    FlowRunStatus.RUNNING,
                    FlowRunStatus.PAUSED,
                ):
                    active_run_id = str(newest_record.id)
                for record in all_runs:
                    if record.status not in active_statuses:
                        continue
                    record_id = str(record.id)
                    if newest_run_id and record_id != newest_run_id:
                        continue
                    record_input = dict(record.input_data or {})
                    latest = _latest_dispatch(
                        repo_root,
                        record_id,
                        record_input,
                        max_text_chars=max_text_chars,
                    )
                    latest_payload = latest if isinstance(latest, dict) else {}
                    latest_reply_seq = _latest_reply_history_seq(
                        repo_root, record_id, record_input
                    )
                    seq = int(latest_payload.get("seq") or 0)
                    dispatch_payload = latest_payload.get("dispatch")
                    has_dispatch, dispatch_state_reason = (
                        _resolve_paused_dispatch_state(
                            repo_root=repo_root,
                            record_status=record.status,
                            latest_payload=latest_payload,
                            latest_reply_seq=latest_reply_seq,
                        )
                    )
                    if record.status == FlowRunStatus.FAILED:
                        dispatch_state_reason = record.error_message or "Run failed"
                    elif record.status == FlowRunStatus.STOPPED:
                        dispatch_state_reason = "Run was stopped"
                    run_state = build_ticket_flow_run_state(
                        repo_root=repo_root,
                        repo_id=snap.id,
                        record=record,
                        store=store,
                        has_pending_dispatch=has_dispatch,
                        dispatch_state_reason=dispatch_state_reason,
                    )
                    run_state["active_run_id"] = active_run_id
                    is_terminal_failed = record.status in (
                        FlowRunStatus.FAILED,
                        FlowRunStatus.STOPPED,
                    )
                    if (
                        is_terminal_failed
                        and active_run_id
                        and active_run_id != record_id
                    ):
                        continue
                    if (
                        not run_state.get("attention_required")
                        and not is_terminal_failed
                    ):
                        if has_dispatch:
                            pass
                        else:
                            continue
                    base_item = {
                        "repo_id": snap.id,
                        "repo_display_name": snap.display_name,
                        "run_id": record.id,
                        "run_created_at": record.created_at,
                        "status": record.status.value,
                        "open_url": f"/repos/{snap.id}/?tab=inbox&run_id={record_id}",
                        "run_state": run_state,
                        "canonical_state_v1": build_canonical_state_v1(
                            repo_root=repo_root,
                            repo_id=snap.id,
                            run_state=cast(dict[str, Any], run_state),
                            record=record,
                            store=store,
                            preferred_run_id=newest_run_id,
                            stale_threshold_seconds=stale_threshold_seconds,
                        ),
                        "active_run_id": active_run_id,
                    }
                    if has_dispatch:
                        if find_message_resolution(
                            dismissals,
                            run_id=record_id,
                            item_type="run_dispatch",
                            seq=seq if seq > 0 else None,
                        ):
                            continue
                        messages.append(
                            {
                                **base_item,
                                "item_type": "run_dispatch",
                                "next_action": "reply_and_resume",
                                "seq": seq,
                                "dispatch": dispatch_payload,
                                "dispatch_actionable": True,
                                "files": latest_payload.get("files") or [],
                            }
                        )
                    else:
                        item_type, next_action = (
                            _ticket_flow_inbox_item_type_and_next_action(
                                repo_root=repo_root, record=record
                            )
                        )
                        _maybe_auto_dismiss_terminal_inbox_item(
                            supervisor=supervisor,
                            repo_root=repo_root,
                            repo_id=snap.id,
                            dismissals=dismissals,
                            record=record,
                            item_type=item_type,
                            seq=seq if seq > 0 else None,
                        )
                        if find_message_resolution(
                            dismissals,
                            run_id=record_id,
                            item_type=item_type,
                            seq=seq if seq > 0 else None,
                        ):
                            continue
                        item_resolution = find_message_resolution_entry(
                            dismissals,
                            run_id=record_id,
                            item_type=item_type,
                            seq=seq if seq > 0 else None,
                            include_unresolved=True,
                        )
                        item_reason = dispatch_state_reason
                        if (
                            isinstance(item_resolution, dict)
                            and str(item_resolution.get("resolution_state") or "")
                            .strip()
                            .lower()
                            == MESSAGE_PENDING_AUTO_DISMISS_STATE
                        ):
                            pending_reason = str(
                                item_resolution.get("reason") or ""
                            ).strip()
                            if pending_reason:
                                item_reason = pending_reason
                        messages.append(
                            {
                                **base_item,
                                "item_type": item_type,
                                "next_action": next_action,
                                "seq": seq if seq > 0 else None,
                                "dispatch": dispatch_payload,
                                "dispatch_actionable": False,
                                "files": latest_payload.get("files") or [],
                                "reason": item_reason,
                                "available_actions": run_state.get(
                                    "recommended_actions", []
                                ),
                            }
                        )
        except Exception as exc:
            _logger.warning("Failed to gather inbox for repo %s: %s", snap.id, exc)
            continue
    messages.sort(key=lambda m: m.get("run_created_at") or "", reverse=True)
    return messages


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


def _coerce_automation_items(payload: Any, *, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [entry for entry in candidate if isinstance(entry, dict)]
    return []


def _call_automation_list(
    method: Any, *, key: str, **kwargs: Any
) -> list[dict[str, Any]]:
    if not callable(method):
        return []
    try:
        result = method(**kwargs)
    except TypeError:
        try:
            result = method()
        except Exception:
            return []
    except Exception:
        return []
    return _coerce_automation_items(result, key=key)


def _snapshot_pma_automation(
    supervisor: HubSupervisor, *, max_items: int = PMA_MAX_AUTOMATION_ITEMS
) -> dict[str, Any]:
    out = {
        "subscriptions": {"active_count": 0, "sample": []},
        "timers": {"pending_count": 0, "sample": []},
        "wakeups": {
            "pending_count": 0,
            "dispatched_recent_count": 0,
            "pending_sample": [],
        },
    }
    try:
        store = supervisor.ensure_pma_automation_store()
    except Exception:
        return out

    subscriptions = _call_automation_list(
        getattr(store, "list_subscriptions", None), key="subscriptions"
    )
    subscriptions_sample = _call_automation_list(
        getattr(store, "list_subscriptions", None),
        key="subscriptions",
        limit=max_items,
    )
    timers = _call_automation_list(getattr(store, "list_timers", None), key="timers")
    timers_sample = _call_automation_list(
        getattr(store, "list_timers", None),
        key="timers",
        limit=max_items,
    )
    pending_wakeups = _call_automation_list(
        getattr(store, "list_wakeups", None), key="wakeups", state_filter="pending"
    )
    pending_wakeups_sample = _call_automation_list(
        getattr(store, "list_pending_wakeups", None),
        key="wakeups",
        limit=max_items,
    )
    if not pending_wakeups:
        pending_wakeups = _call_automation_list(
            getattr(store, "list_pending_wakeups", None), key="wakeups"
        )
    if not pending_wakeups_sample:
        pending_wakeups_sample = _call_automation_list(
            getattr(store, "list_wakeups", None),
            key="wakeups",
            state_filter="pending",
            limit=max_items,
        )
    dispatched_wakeups = _call_automation_list(
        getattr(store, "list_wakeups", None),
        key="wakeups",
        state_filter="dispatched",
    )

    def _pick(entry: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        picked: dict[str, Any] = {}
        for field in fields:
            value = entry.get(field)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            picked[field] = value
        return picked

    out["subscriptions"] = {
        "active_count": len(subscriptions),
        "sample": [
            _pick(
                entry,
                (
                    "subscription_id",
                    "event_types",
                    "repo_id",
                    "run_id",
                    "thread_id",
                    "lane_id",
                    "from_state",
                    "to_state",
                    "reason",
                ),
            )
            for entry in subscriptions_sample[:max_items]
        ],
    }
    out["timers"] = {
        "pending_count": len(timers),
        "sample": [
            _pick(
                entry,
                (
                    "timer_id",
                    "timer_type",
                    "due_at",
                    "idle_seconds",
                    "repo_id",
                    "run_id",
                    "thread_id",
                    "lane_id",
                    "reason",
                ),
            )
            for entry in timers_sample[:max_items]
        ],
    }
    out["wakeups"] = {
        "pending_count": len(pending_wakeups),
        "dispatched_recent_count": len(dispatched_wakeups),
        "pending_sample": [
            _pick(
                entry,
                (
                    "wakeup_id",
                    "source",
                    "event_type",
                    "subscription_id",
                    "timer_id",
                    "repo_id",
                    "run_id",
                    "thread_id",
                    "lane_id",
                    "from_state",
                    "to_state",
                    "reason",
                    "timestamp",
                ),
            )
            for entry in pending_wakeups_sample[:max_items]
        ],
    }
    return out


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
            summary["ticket_flow"] = _get_ticket_flow_summary(snap.path)
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
