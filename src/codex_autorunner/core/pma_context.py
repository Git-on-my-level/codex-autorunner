from __future__ import annotations

import asyncio
import json
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..bootstrap import (
    ensure_pma_docs,
    pma_active_context_content,
    pma_doc_path,
    pma_docs_dir,
)
from ..tickets.files import safe_relpath
from ..tickets.models import Dispatch
from ..tickets.outbox import parse_dispatch, resolve_outbox_paths
from ..tickets.replies import resolve_reply_paths
from .config import load_hub_config, load_repo_config
from .flows.failure_diagnostics import format_failure_summary, get_failure_payload
from .flows.models import FlowRunRecord, FlowRunStatus
from .flows.store import FlowStore
from .flows.worker_process import check_worker_health
from .hub import HubSupervisor
from .state_roots import resolve_hub_templates_root
from .ticket_flow_summary import build_ticket_flow_summary
from .utils import atomic_write

PMA_MAX_REPOS = 25
PMA_MAX_MESSAGES = 10
PMA_MAX_TEXT = 800
PMA_MAX_TEMPLATE_REPOS = 25
PMA_MAX_TEMPLATE_FIELD_CHARS = 120
PMA_MAX_PMA_FILES = 50
PMA_MAX_LIFECYCLE_EVENTS = 20
PMA_ACTIVE_CONTEXT_STATE_FILENAME = ".active_context_state.json"

# Keep this short and stable; see ticket TICKET-001 for rationale.
PMA_FASTPATH = """<pma_fastpath>
You are PMA inside Codex Autorunner (CAR). Treat the filesystem as truth; prefer creating/updating CAR artifacts over "chat-only" plans.

First-turn routine:
1) Read <user_message> and <hub_snapshot>.
2) If hub_snapshot.inbox has entries, handle them first (these are paused runs needing input):
   - Summarize the dispatch question.
   - Answer it or propose the next minimal action.
   - Include the item.open_url so the user can jump straight to the repo Inbox tab.
3) If the request is new work:
   - Identify the target repo(s).
   - Prefer hub-owned worktrees for changes.
   - Prefer one-shot setup/repair commands: `car hub tickets setup-pack`, `car hub tickets fmt`, `car hub tickets doctor --fix`.
   - Create/adjust repo tickets under each repo's `.codex-autorunner/tickets/`.

Web UI map (user perspective):
- Hub root: `/` (repos list + global notifications).
- Repo view: `/repos/<repo_id>/` tabs: Tickets | Inbox | Contextspace | Terminal | Analytics | Archive.
  - Tickets: edit queue; Inbox: paused run dispatches; Contextspace: active_context/spec/decisions.

Ticket planning constraints (state machine):
- Ticket flow processes `.codex-autorunner/tickets/TICKET-###*.md` in ascending numeric order.
- On each turn it picks the first ticket where `done != true`; when that ticket is completed, it advances to the next.
- `depends_on` frontmatter is not supported; filename order is the only execution contract.
- If prerequisites are discovered late, reorder/split tickets so prerequisite work appears earlier.

What each ticket agent turn can already see:
- The current ticket file (full markdown + frontmatter).
- Pinned contextspace docs when present: `active_context.md`, `decisions.md`, `spec.md` (truncated).
- Reply context from prior user dispatches and prior agent output (if present).
</pma_fastpath>
"""

# Defaults used when hub config is not available (should be rare).
PMA_DOCS_MAX_CHARS = 12_000
PMA_ACTIVE_CONTEXT_MAX_LINES = 200
PMA_CONTEXT_LOG_TAIL_LINES = 120


def _tail_lines(text: str, max_lines: int) -> str:
    if max_lines <= 0:
        return ""
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[-max_lines:])


def _active_context_state_path(hub_root: Path) -> Path:
    return pma_docs_dir(hub_root) / PMA_ACTIVE_CONTEXT_STATE_FILENAME


def _load_active_context_state(hub_root: Path) -> dict[str, Any]:
    path = _active_context_state_path(hub_root)
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {}


def _save_active_context_state(hub_root: Path, payload: dict[str, Any]) -> None:
    path = _active_context_state_path(hub_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def get_active_context_auto_prune_meta(hub_root: Path) -> Optional[dict[str, Any]]:
    payload = _load_active_context_state(hub_root)
    if not payload:
        return None
    last_at = payload.get("last_auto_pruned_at")
    line_before = payload.get("line_count_before")
    line_budget = payload.get("line_budget")
    if not isinstance(last_at, str) or not last_at.strip():
        return None
    if not isinstance(line_before, int):
        line_before = 0
    if not isinstance(line_budget, int):
        line_budget = PMA_ACTIVE_CONTEXT_MAX_LINES
    return {
        "last_auto_pruned_at": last_at.strip(),
        "line_count_before": line_before,
        "line_budget": line_budget,
    }


def maybe_auto_prune_active_context(
    hub_root: Path,
    *,
    max_lines: int,
) -> Optional[dict[str, Any]]:
    try:
        parsed_max_lines = int(max_lines)
    except Exception:
        parsed_max_lines = PMA_ACTIVE_CONTEXT_MAX_LINES
    max_lines = (
        parsed_max_lines if parsed_max_lines > 0 else PMA_ACTIVE_CONTEXT_MAX_LINES
    )
    docs_dir = pma_docs_dir(hub_root)
    active_context_path = docs_dir / "active_context.md"
    context_log_path = docs_dir / "context_log.md"
    try:
        active_content = active_context_path.read_text(encoding="utf-8")
    except Exception:
        return None
    line_count = len(active_content.splitlines())
    if line_count <= max_lines:
        return None

    timestamp = datetime.now(timezone.utc).isoformat()
    snapshot_header = f"\n\n## Snapshot: {timestamp}\n\n"
    snapshot_content = snapshot_header + active_content

    try:
        with context_log_path.open("a", encoding="utf-8") as f:
            f.write(snapshot_content)
    except Exception:
        return None

    pruned_content = (
        f"{pma_active_context_content().rstrip()}\n\n"
        f"> Auto-pruned on {timestamp} (had {line_count} lines; budget: {max_lines}).\n"
    )
    try:
        atomic_write(active_context_path, pruned_content)
    except Exception:
        return None

    state = {
        "version": 1,
        "last_auto_pruned_at": timestamp,
        "line_count_before": line_count,
        "line_budget": max_lines,
    }
    try:
        _save_active_context_state(hub_root, state)
    except Exception:
        pass
    return state


def load_pma_workspace_docs(hub_root: Path) -> dict[str, Any]:
    """Load hub-level PMA context docs for prompt injection.

    These docs act as durable memory and working context for PMA.
    """
    try:
        ensure_pma_docs(hub_root)
    except Exception:
        pass

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
    except Exception:
        pass

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
        except Exception:
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
    except Exception:
        return None


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


def load_pma_prompt(hub_root: Path) -> str:
    path = pma_doc_path(hub_root, "prompt.md")
    try:
        ensure_pma_docs(hub_root)
    except Exception:
        pass
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
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
) -> str:
    lines: list[str] = []

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
            lines.append(
                f"- type={item_type} next_action={next_action} repo_id={repo_id} "
                f"run_id={run_id} seq={seq} mode={mode} handoff={str(handoff).lower()} "
                f"state={state} current_ticket={current_ticket} last_progress_at={last_progress_at}"
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
            recommended_action = run_state.get("recommended_action")
            if recommended_action:
                lines.append(
                    f"  recommended_action: {_truncate(str(recommended_action), max_text_chars)}"
                )
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
            ticket_flow = _render_ticket_flow_summary(repo.get("ticket_flow"))
            run_state = repo.get("run_state") or {}
            state = _truncate(str(run_state.get("state", "")), max_field_chars)
            blocking_reason = _truncate(
                str(run_state.get("blocking_reason", "")), max_text_chars
            )
            recommended_action = _truncate(
                str(run_state.get("recommended_action", "")), max_text_chars
            )
            lines.append(
                f"- {repo_id} ({display_name}): status={status} "
                f"last_run_id={last_run_id} last_exit_code={last_exit} "
                f"ticket_flow={ticket_flow} state={state}"
            )
            if blocking_reason:
                lines.append(f"  blocking_reason: {blocking_reason}")
            if recommended_action:
                lines.append(f"  recommended_action: {recommended_action}")
        lines.append("")

    templates = snapshot.get("templates") or {}
    template_repos = templates.get("repos") or []
    template_scan = templates.get("last_scan")
    if templates.get("enabled") or template_repos or template_scan:
        enabled = bool(templates.get("enabled"))
        lines.append("Templates:")
        lines.append(f"- enabled={str(enabled).lower()}")
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
            if pma_files_detail.get("inbox"):
                lines.append("- next_action: process_uploaded_file")
        if outbox_files:
            lines.append("PMA File Outbox:")
            files = [
                _truncate(str(name), max_field_chars)
                for name in list(outbox_files)[: max(0, max_pma_files)]
            ]
            lines.append(f"- outbox: [{', '.join(files)}]")
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


def format_pma_prompt(
    base_prompt: str,
    snapshot: dict[str, Any],
    message: str,
    hub_root: Optional[Path] = None,
) -> str:
    limits = snapshot.get("limits") or {}
    snapshot_text = _render_hub_snapshot(
        snapshot,
        max_repos=limits.get("max_repos", PMA_MAX_REPOS),
        max_messages=limits.get("max_messages", PMA_MAX_MESSAGES),
        max_text_chars=limits.get("max_text_chars", PMA_MAX_TEXT),
    )

    pma_docs: Optional[dict[str, Any]] = None
    if hub_root is not None:
        try:
            pma_docs = load_pma_workspace_docs(hub_root)
        except Exception:
            pma_docs = None

    prompt = f"{base_prompt}\n\n"
    prompt += (
        "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`.\n"
        "Durable guidance: `.codex-autorunner/pma/docs/AGENTS.md`.\n"
        "Working context: `.codex-autorunner/pma/docs/active_context.md`.\n"
        "History: `.codex-autorunner/pma/docs/context_log.md`.\n"
        "To send a file to the user, write it to `.codex-autorunner/pma/outbox/`.\n"
        "User uploaded files are in `.codex-autorunner/pma/inbox/`.\n\n"
    )

    if pma_docs:
        max_lines = pma_docs.get("active_context_max_lines")
        line_count = pma_docs.get("active_context_line_count")
        auto_prune = pma_docs.get("active_context_auto_prune") or {}
        auto_pruned_at = auto_prune.get("last_auto_pruned_at")
        auto_pruned_before = auto_prune.get("line_count_before")
        auto_pruned_budget = auto_prune.get("line_budget")
        prompt += (
            "<pma_workspace_docs>\n"
            "<AGENTS_MD>\n"
            f"{pma_docs.get('agents', '')}\n"
            "</AGENTS_MD>\n"
            "<ACTIVE_CONTEXT_MD>\n"
            f"{pma_docs.get('active_context', '')}\n"
            "</ACTIVE_CONTEXT_MD>\n"
            f"<ACTIVE_CONTEXT_BUDGET lines='{max_lines}' current_lines='{line_count}' />\n"
            f"<ACTIVE_CONTEXT_AUTO_PRUNE last_at='{auto_pruned_at}' line_count_before='{auto_pruned_before}' line_budget='{auto_pruned_budget}' triggered_now='{str(bool(pma_docs.get('active_context_auto_pruned'))).lower()}' />\n"
            "<CONTEXT_LOG_TAIL_MD>\n"
            f"{pma_docs.get('context_log_tail', '')}\n"
            "</CONTEXT_LOG_TAIL_MD>\n"
            "</pma_workspace_docs>\n\n"
        )

    prompt += f"{PMA_FASTPATH}\n\n"
    prompt += (
        "<hub_snapshot>\n"
        f"{snapshot_text}\n"
        "</hub_snapshot>\n\n"
        "<user_message>\n"
        f"{message}\n"
        "</user_message>\n"
    )
    return prompt


def _get_ticket_flow_summary(repo_path: Path) -> Optional[dict[str, Any]]:
    return build_ticket_flow_summary(repo_path, include_failure=False)


def _resolve_workspace_and_runs(
    record_input: dict[str, Any], repo_root: Path
) -> tuple[Path, Path]:
    workspace_raw = record_input.get("workspace_root")
    workspace_root = Path(workspace_raw) if workspace_raw else repo_root
    if not workspace_root.is_absolute():
        workspace_root = (repo_root / workspace_root).resolve()
    else:
        workspace_root = workspace_root.resolve()
    resolved_repo = repo_root.resolve()
    try:
        workspace_root.relative_to(resolved_repo)
    except ValueError as exc:
        raise ValueError(
            f"workspace_root escapes repo boundary: {workspace_root}"
        ) from exc
    runs_raw = record_input.get("runs_dir") or ".codex-autorunner/runs"
    runs_dir = Path(runs_raw)
    if not runs_dir.is_absolute():
        runs_dir = (workspace_root / runs_dir).resolve()
    return workspace_root, runs_dir


def _latest_reply_history_seq(
    repo_root: Path, run_id: str, record_input: dict[str, Any]
) -> int:
    try:
        workspace_root, runs_dir = _resolve_workspace_and_runs(record_input, repo_root)
        reply_paths = resolve_reply_paths(
            workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
        )
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
    except Exception:
        return 0


def _dispatch_dict(dispatch: Dispatch, *, max_text_chars: int) -> dict[str, Any]:
    return {
        "mode": dispatch.mode,
        "title": _truncate(dispatch.title, max_text_chars),
        "body": _truncate(dispatch.body, max_text_chars),
        "extra": _trim_extra(dispatch.extra, max_text_chars),
        "is_handoff": dispatch.is_handoff,
    }


def _latest_dispatch(
    repo_root: Path, run_id: str, input_data: dict, *, max_text_chars: int
) -> Optional[dict[str, Any]]:
    try:
        workspace_root, runs_dir = _resolve_workspace_and_runs(input_data, repo_root)
        outbox_paths = resolve_outbox_paths(
            workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
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
            "dir": safe_relpath(selected_dir, repo_root),
            "dispatch": _dispatch_dict(
                selected_dispatch, max_text_chars=max_text_chars
            ),
            "errors": [],
            "files": _list_files(selected_dir),
        }
    except Exception:
        return None


def build_ticket_flow_run_state(
    *,
    repo_root: Path,
    repo_id: str,
    record: FlowRunRecord,
    store: FlowStore,
    has_pending_dispatch: bool,
    dispatch_state_reason: Optional[str] = None,
) -> dict[str, Any]:
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
    state_payload = record.state if isinstance(record.state, dict) else {}
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
        except Exception:
            health = None
            dead_worker = False

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
        detail = health.message if health is not None else None
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

    recommended_actions: list[str] = []
    if state == "completed":
        recommended_actions = [start_cmd]
    elif state == "dead":
        recommended_actions = [f"{resume_cmd} --force", status_cmd, stop_cmd]
    elif record.status == FlowRunStatus.PAUSED:
        if has_pending_dispatch:
            recommended_actions = [resume_cmd, status_cmd, stop_cmd]
        else:
            recommended_actions = [f"{resume_cmd} --force", status_cmd, stop_cmd]
    elif state == "blocked":
        recommended_actions = [f"{resume_cmd} --force", status_cmd, stop_cmd]
    else:
        recommended_actions = [status_cmd]

    return {
        "state": state,
        "blocking_reason": blocking_reason,
        "current_ticket": current_ticket,
        "last_progress_at": last_progress_at,
        "recommended_action": recommended_actions[0] if recommended_actions else None,
        "recommended_actions": recommended_actions,
        "attention_required": attention_required,
        "worker_status": worker_status,
        "flow_status": record.status.value,
        "repo_id": repo_id,
        "run_id": run_id,
    }


def get_latest_ticket_flow_run_state(
    repo_root: Path, repo_id: str
) -> Optional[dict[str, Any]]:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None
    try:
        config = load_repo_config(repo_root)
        with FlowStore(db_path, durable=config.durable_writes) as store:
            records = store.list_flow_runs(flow_type="ticket_flow")
            if not records:
                return None
            record = records[0]
            latest = _latest_dispatch(
                repo_root,
                str(record.id),
                dict(record.input_data or {}),
                max_text_chars=PMA_MAX_TEXT,
            )
            reply_seq = _latest_reply_history_seq(
                repo_root, str(record.id), dict(record.input_data or {})
            )
            dispatch_seq = (
                int(latest.get("seq") or 0) if isinstance(latest, dict) else 0
            )
            has_dispatch = bool(
                latest
                and latest.get("dispatch")
                and dispatch_seq > 0
                and reply_seq < dispatch_seq
            )
            reason = None
            if record.status == FlowRunStatus.PAUSED and not has_dispatch:
                if (
                    latest
                    and isinstance(latest.get("errors"), list)
                    and latest.get("errors")
                ):
                    reason = "Paused run has unreadable dispatch metadata"
                elif dispatch_seq > 0 and reply_seq >= dispatch_seq:
                    reason = "Latest dispatch already replied; run is still paused"
                else:
                    reason = "Run is paused without an actionable dispatch"
            return build_ticket_flow_run_state(
                repo_root=repo_root,
                repo_id=repo_id,
                record=record,
                store=store,
                has_pending_dispatch=has_dispatch,
                dispatch_state_reason=reason,
            )
    except Exception:
        return None


def _gather_inbox(
    supervisor: HubSupervisor, *, max_text_chars: int
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    try:
        snapshots = supervisor.list_repos()
    except Exception:
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
                active_statuses = [
                    FlowRunStatus.PAUSED,
                    FlowRunStatus.RUNNING,
                    FlowRunStatus.FAILED,
                    FlowRunStatus.STOPPED,
                ]
                all_runs = store.list_flow_runs(flow_type="ticket_flow")
                for record in all_runs:
                    if record.status not in active_statuses:
                        continue
                    record_input = dict(record.input_data or {})
                    latest = _latest_dispatch(
                        repo_root,
                        str(record.id),
                        record_input,
                        max_text_chars=max_text_chars,
                    )
                    latest_payload = latest if isinstance(latest, dict) else {}
                    latest_reply_seq = _latest_reply_history_seq(
                        repo_root, str(record.id), record_input
                    )
                    seq = int(latest_payload.get("seq") or 0)
                    has_dispatch = bool(
                        latest_payload.get("dispatch")
                        and seq > 0
                        and latest_reply_seq < seq
                    )
                    dispatch_state_reason = None
                    if record.status == FlowRunStatus.PAUSED and not has_dispatch:
                        if latest_payload.get("errors"):
                            dispatch_state_reason = (
                                "Paused run has unreadable dispatch metadata"
                            )
                        elif seq > 0 and latest_reply_seq >= seq:
                            dispatch_state_reason = (
                                "Latest dispatch already replied; run is still paused"
                            )
                        else:
                            dispatch_state_reason = (
                                "Run is paused without an actionable dispatch"
                            )
                    elif record.status == FlowRunStatus.FAILED:
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
                    is_terminal_failed = record.status in (
                        FlowRunStatus.FAILED,
                        FlowRunStatus.STOPPED,
                    )
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
                        "open_url": f"/repos/{snap.id}/?tab=inbox&run_id={record.id}",
                        "run_state": run_state,
                    }
                    if has_dispatch:
                        dispatch_payload = latest_payload.get("dispatch")
                        messages.append(
                            {
                                **base_item,
                                "item_type": "run_dispatch",
                                "next_action": "reply_and_resume",
                                "seq": seq,
                                "dispatch": dispatch_payload,
                                "files": latest_payload.get("files") or [],
                            }
                        )
                    else:
                        item_type = "run_state_attention"
                        next_action = "inspect_and_resume"
                        if record.status == FlowRunStatus.RUNNING:
                            health = check_worker_health(repo_root, str(record.id))
                            if health.status in {"dead", "invalid", "mismatch"}:
                                item_type = "worker_dead"
                                next_action = "restart_worker"
                        elif record.status == FlowRunStatus.FAILED:
                            item_type = "run_failed"
                            next_action = "diagnose_or_restart"
                        elif record.status == FlowRunStatus.STOPPED:
                            item_type = "run_stopped"
                            next_action = "diagnose_or_restart"
                        messages.append(
                            {
                                **base_item,
                                "item_type": item_type,
                                "next_action": next_action,
                                "seq": seq if seq > 0 else None,
                                "dispatch": latest_payload.get("dispatch"),
                                "files": latest_payload.get("files") or [],
                                "reason": dispatch_state_reason,
                                "available_actions": run_state.get(
                                    "recommended_actions", []
                                ),
                            }
                        )
        except Exception:
            continue
    messages.sort(key=lambda m: (m.get("run_created_at") or ""), reverse=True)
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


async def build_hub_snapshot(
    supervisor: Optional[HubSupervisor],
    hub_root: Optional[Path] = None,
) -> dict[str, Any]:
    if supervisor is None:
        return {
            "repos": [],
            "inbox": [],
            "templates": {"enabled": False, "repos": []},
            "lifecycle_events": [],
            "pma_files_detail": {"inbox": [], "outbox": []},
        }

    snapshots = await asyncio.to_thread(supervisor.list_repos)
    snapshots = sorted(snapshots, key=lambda snap: snap.id)
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
        summary: dict[str, Any] = {
            "id": snap.id,
            "display_name": snap.display_name,
            "status": snap.status.value,
            "last_run_id": snap.last_run_id,
            "last_run_started_at": snap.last_run_started_at,
            "last_run_finished_at": snap.last_run_finished_at,
            "last_exit_code": snap.last_exit_code,
            "ticket_flow": None,
            "run_state": None,
        }
        if snap.initialized and snap.exists_on_disk:
            summary["ticket_flow"] = _get_ticket_flow_summary(snap.path)
            summary["run_state"] = get_latest_ticket_flow_run_state(snap.path, snap.id)
        repos.append(summary)

    inbox = await asyncio.to_thread(
        _gather_inbox, supervisor, max_text_chars=max_text_chars
    )
    inbox = inbox[:max_messages]

    lifecycle_events = await asyncio.to_thread(
        _gather_lifecycle_events, supervisor, limit=20
    )

    templates = _build_templates_snapshot(supervisor, hub_root=hub_root)

    pma_files: dict[str, list[str]] = {"inbox": [], "outbox": []}
    pma_files_detail: dict[str, list[dict[str, str]]] = {
        "inbox": [],
        "outbox": [],
    }
    if hub_root:
        try:
            pma_dir = hub_root / ".codex-autorunner" / "pma"
            for box in ["inbox", "outbox"]:
                box_dir = pma_dir / box
                if box_dir.exists():
                    files = [
                        f.name
                        for f in box_dir.iterdir()
                        if f.is_file() and not f.name.startswith(".")
                    ]
                    pma_files[box] = sorted(files)
                    pma_files_detail[box] = [
                        {
                            "item_type": "pma_file",
                            "next_action": "process_uploaded_file",
                            "box": box,
                            "name": name,
                        }
                        for name in pma_files[box]
                    ]
        except Exception:
            pass

    return {
        "repos": repos,
        "inbox": inbox,
        "templates": templates,
        "pma_files": pma_files,
        "pma_files_detail": pma_files_detail,
        "lifecycle_events": lifecycle_events,
        "limits": {
            "max_repos": max_repos,
            "max_messages": max_messages,
            "max_text_chars": max_text_chars,
        },
    }
