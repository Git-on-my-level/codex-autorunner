from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from ..bootstrap import ensure_pma_docs
from ..tickets.files import list_ticket_paths, safe_relpath, ticket_is_done
from ..tickets.outbox import parse_dispatch, resolve_outbox_paths
from .config import load_hub_config, load_repo_config
from .flows.models import FlowRunStatus
from .flows.store import FlowStore
from .hub import HubSupervisor
from .state_roots import resolve_hub_templates_root

PMA_MAX_REPOS = 25
PMA_MAX_MESSAGES = 10
PMA_MAX_TEXT = 800
PMA_MAX_TEMPLATE_REPOS = 25
PMA_MAX_TEMPLATE_FIELD_CHARS = 120
PMA_MAX_PMA_FILES = 50
PMA_MAX_LIFECYCLE_EVENTS = 20

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
   - Create/adjust repo tickets under each repo's `.codex-autorunner/tickets/`.

Web UI map (user perspective):
- Hub root: `/` (repos list + global notifications).
- Repo view: `/repos/<repo_id>/` tabs: Tickets | Inbox | Contextspace | Terminal | Analytics | Archive.
  - Tickets: edit queue; Inbox: paused run dispatches; Contextspace: active_context/spec/decisions.
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

    pma_dir = hub_root / ".codex-autorunner" / "pma"
    agents_path = pma_dir / "AGENTS.md"
    active_context_path = pma_dir / "active_context.md"
    context_log_path = pma_dir / "context_log.md"

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
    path = hub_root / ".codex-autorunner" / "pma" / "prompt.md"
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
    parts: list[str] = []
    if status is not None:
        parts.append(f"status={status}")
    if done_count is not None and total_count is not None:
        parts.append(f"done={done_count}/{total_count}")
    if current_step is not None:
        parts.append(f"step={current_step}")
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
        lines.append("Inbox (paused runs needing attention):")
        for item in list(inbox)[: max(0, max_messages)]:
            repo_id = _truncate(str(item.get("repo_id", "")), max_field_chars)
            run_id = _truncate(str(item.get("run_id", "")), max_field_chars)
            seq = _truncate(str(item.get("seq", "")), max_field_chars)
            dispatch = item.get("dispatch") or {}
            mode = _truncate(str(dispatch.get("mode", "")), max_field_chars)
            handoff = bool(dispatch.get("is_handoff"))
            lines.append(
                f"- repo_id={repo_id} run_id={run_id} seq={seq} mode={mode} "
                f"handoff={str(handoff).lower()}"
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
            lines.append(
                f"- {repo_id} ({display_name}): status={status} "
                f"last_run_id={last_run_id} last_exit_code={last_exit} "
                f"ticket_flow={ticket_flow}"
            )
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
    if inbox_files or outbox_files:
        lines.append("PMA files:")
        if inbox_files:
            files = [
                _truncate(str(name), max_field_chars)
                for name in list(inbox_files)[: max(0, max_pma_files)]
            ]
            lines.append(f"- inbox: [{', '.join(files)}]")
        if outbox_files:
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
        "Ops guide: `.codex-autorunner/pma/ABOUT_CAR.md`.\n"
        "Durable guidance: `.codex-autorunner/pma/AGENTS.md`.\n"
        "Working context: `.codex-autorunner/pma/active_context.md`.\n"
        "History: `.codex-autorunner/pma/context_log.md`.\n"
        "To send a file to the user, write it to `.codex-autorunner/pma/outbox/`.\n"
        "User uploaded files are in `.codex-autorunner/pma/inbox/`.\n\n"
    )

    if pma_docs:
        max_lines = pma_docs.get("active_context_max_lines")
        line_count = pma_docs.get("active_context_line_count")
        prompt += (
            "<pma_workspace_docs>\n"
            "<AGENTS_MD>\n"
            f"{pma_docs.get('agents', '')}\n"
            "</AGENTS_MD>\n"
            "<ACTIVE_CONTEXT_MD>\n"
            f"{pma_docs.get('active_context', '')}\n"
            "</ACTIVE_CONTEXT_MD>\n"
            f"<ACTIVE_CONTEXT_BUDGET lines='{max_lines}' current_lines='{line_count}' />\n"
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
    db_path = repo_path / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None
    try:
        config = load_repo_config(repo_path)
        with FlowStore(db_path, durable=config.durable_writes) as store:
            runs = store.list_flow_runs(flow_type="ticket_flow")
            if not runs:
                return None
            latest = runs[0]

            ticket_dir = repo_path / ".codex-autorunner" / "tickets"
            total = 0
            done = 0
            for path in list_ticket_paths(ticket_dir):
                total += 1
                try:
                    if ticket_is_done(path):
                        done += 1
                except Exception:
                    continue

            if total == 0:
                return None

            state = latest.state if isinstance(latest.state, dict) else {}
            engine = state.get("ticket_engine") if isinstance(state, dict) else {}
            engine = engine if isinstance(engine, dict) else {}
            current_step = engine.get("total_turns")

            return {
                "status": latest.status.value,
                "done_count": done,
                "total_count": total,
                "current_step": current_step,
            }
    except Exception:
        return None


def _latest_dispatch(
    repo_root: Path, run_id: str, input_data: dict, *, max_text_chars: int
) -> Optional[dict[str, Any]]:
    try:
        workspace_root = Path(input_data.get("workspace_root") or repo_root)
        runs_dir = Path(input_data.get("runs_dir") or ".codex-autorunner/runs")
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
        latest_dir = sorted(seq_dirs, key=lambda p: p.name)[-1]
        seq = int(latest_dir.name)
        dispatch_path = latest_dir / "DISPATCH.md"
        dispatch, errors = parse_dispatch(dispatch_path)
        if errors or dispatch is None:
            return {
                "seq": seq,
                "dir": safe_relpath(latest_dir, repo_root),
                "dispatch": None,
                "errors": errors,
                "files": [],
            }
        files: list[str] = []
        for child in sorted(latest_dir.iterdir(), key=lambda p: p.name):
            if child.name.startswith("."):
                continue
            if child.name == "DISPATCH.md":
                continue
            if child.is_file():
                files.append(child.name)
        dispatch_dict = {
            "mode": dispatch.mode,
            "title": _truncate(dispatch.title, max_text_chars),
            "body": _truncate(dispatch.body, max_text_chars),
            "extra": _trim_extra(dispatch.extra, max_text_chars),
            "is_handoff": dispatch.is_handoff,
        }
        return {
            "seq": seq,
            "dir": safe_relpath(latest_dir, repo_root),
            "dispatch": dispatch_dict,
            "errors": [],
            "files": files,
        }
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
                paused = store.list_flow_runs(
                    flow_type="ticket_flow", status=FlowRunStatus.PAUSED
                )
        except Exception:
            continue
        if not paused:
            continue
        for record in paused:
            latest = _latest_dispatch(
                repo_root,
                str(record.id),
                dict(record.input_data or {}),
                max_text_chars=max_text_chars,
            )
            if not latest or not latest.get("dispatch"):
                continue
            messages.append(
                {
                    "repo_id": snap.id,
                    "repo_display_name": snap.display_name,
                    "run_id": record.id,
                    "run_created_at": record.created_at,
                    "seq": latest["seq"],
                    "dispatch": latest["dispatch"],
                    "files": latest.get("files") or [],
                    "open_url": f"/repos/{snap.id}/?tab=inbox&run_id={record.id}",
                }
            )
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
        }
        if snap.initialized and snap.exists_on_disk:
            summary["ticket_flow"] = _get_ticket_flow_summary(snap.path)
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
        except Exception:
            pass

    return {
        "repos": repos,
        "inbox": inbox,
        "templates": templates,
        "pma_files": pma_files,
        "lifecycle_events": lifecycle_events,
        "limits": {
            "max_repos": max_repos,
            "max_messages": max_messages,
            "max_text_chars": max_text_chars,
        },
    }
