from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
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
)
from .flows.models import (
    FlowRunRecord,
    FlowRunStatus,
    flow_run_duration_seconds,
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
from .locks import file_lock
from .pma_active_context import (
    PMA_ACTIVE_CONTEXT_MAX_LINES,
    get_active_context_auto_prune_meta,
    maybe_auto_prune_active_context,
)
from .pma_thread_store import PmaThreadStore, default_pma_threads_db_path
from .state_roots import resolve_hub_templates_root
from .text_utils import _parse_iso_timestamp
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
                "handled, move it out of the active inbox with `car pma file "
                "consume` or `car pma file dismiss`."
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


def _timestamp_sort_value(value: Any) -> float:
    parsed = _parse_iso_timestamp(value)
    if parsed is None:
        return 0.0
    return parsed.timestamp()


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
    except Exception as exc:
        _logger.warning("Could not ensure PMA docs for prompt: %s", exc)
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        _logger.warning("Could not read prompt file: %s", exc)
        return ""


from .pma_rendering import _render_hub_snapshot  # noqa: E402


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
    if preflight.reason_code == "no_tickets":
        return (
            "Latest dispatch is stale; ticket flow resume preflight would fail because "
            f"no tickets remain in {safe_relpath(repo_root / '.codex-autorunner' / 'tickets', repo_root)}"
        )
    # deleted_context / invalid_state — workspace or ticket dir is gone
    if preflight.reason:
        return (
            "Latest dispatch is stale; ticket flow resume preflight would fail: "
            + preflight.reason
        )
    return (
        "Latest dispatch is stale; ticket flow resume preflight would fail "
        f"in {safe_relpath(repo_root, repo_root)}"
    )


def _ticket_flow_recommended_actions(
    *,
    state: str,
    record_status: FlowRunStatus,
    has_pending_dispatch: bool,
    archive_cmd: str,
    status_cmd: str,
    resume_cmd: str,
    start_cmd: str,
    stop_cmd: str,
) -> list[str]:
    if state == "completed":
        return [start_cmd]
    if record_status in {FlowRunStatus.FAILED, FlowRunStatus.STOPPED}:
        return [archive_cmd, status_cmd]
    if state == "dead":
        return [f"{resume_cmd} --force-new", status_cmd, stop_cmd]
    if record_status == FlowRunStatus.PAUSED:
        if has_pending_dispatch:
            return [resume_cmd, status_cmd, stop_cmd]
        return [f"{resume_cmd} --force-new", status_cmd, stop_cmd]
    if state == "blocked":
        return [f"{resume_cmd} --force-new", status_cmd, stop_cmd]
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
    archive_cmd = f"car flow ticket_flow archive --repo {quoted_repo} --run-id {run_id}"
    status_cmd = f"car flow ticket_flow status --repo {quoted_repo} --run-id {run_id}"
    resume_cmd = f"car flow ticket_flow start --repo {quoted_repo}"
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
        archive_cmd=archive_cmd,
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
        store = supervisor.pma_automation_store
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


from .pma_action_queue import build_pma_action_queue  # noqa: E402
from .pma_inbox import _gather_inbox  # noqa: E402
