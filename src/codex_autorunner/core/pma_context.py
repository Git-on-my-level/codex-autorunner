from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from ..tickets.files import list_ticket_paths, safe_relpath, ticket_is_done
from ..tickets.outbox import parse_dispatch, resolve_outbox_paths
from .config import load_repo_config
from .flows.models import FlowRunStatus
from .flows.store import FlowStore
from .hub import HubSupervisor

PMA_MAX_REPOS = 25
PMA_MAX_MESSAGES = 10
PMA_MAX_TEXT = 800


def _truncate(text: Optional[str], limit: int) -> str:
    raw = text or ""
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 3)] + "..."


def load_pma_prompt(hub_root: Path) -> str:
    path = hub_root / ".codex-autorunner" / "pma" / "prompt.md"
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def format_pma_prompt(base_prompt: str, snapshot: dict[str, Any], message: str) -> str:
    snapshot_text = json.dumps(snapshot, sort_keys=True)
    return (
        f"{base_prompt}\n\n"
        "<hub_snapshot>\n"
        f"{snapshot_text}\n"
        "</hub_snapshot>\n\n"
        "<user_message>\n"
        f"{message}\n"
        "</user_message>\n"
    )


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
    repo_root: Path, run_id: str, input_data: dict
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
            "title": _truncate(dispatch.title, PMA_MAX_TEXT),
            "body": _truncate(dispatch.body, PMA_MAX_TEXT),
            "extra": dispatch.extra,
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


def _gather_inbox(supervisor: HubSupervisor) -> list[dict[str, Any]]:
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
                repo_root, str(record.id), dict(record.input_data or {})
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
                }
            )
    messages.sort(key=lambda m: (m.get("run_created_at") or ""), reverse=True)
    return messages


async def build_hub_snapshot(
    supervisor: Optional[HubSupervisor],
) -> dict[str, Any]:
    if supervisor is None:
        return {"repos": [], "inbox": []}

    snapshots = await asyncio.to_thread(supervisor.list_repos)
    snapshots = sorted(snapshots, key=lambda snap: snap.id)
    repos: list[dict[str, Any]] = []
    for snap in snapshots[:PMA_MAX_REPOS]:
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

    inbox = await asyncio.to_thread(_gather_inbox, supervisor)
    inbox = inbox[:PMA_MAX_MESSAGES]
    return {"repos": repos, "inbox": inbox}
