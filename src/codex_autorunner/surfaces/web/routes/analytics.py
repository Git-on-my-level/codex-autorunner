"""Analytics summary routes.

This module aggregates run/ticket/message metadata for the analytics dashboard
without relying on legacy autorunner endpoints. It intentionally reads from the
filesystem-backed ticket_flow store and ticket files to keep the UI consistent
with the rest of the app.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter

from ...core.flows.models import FlowRunRecord, FlowRunStatus
from ...core.flows.store import FlowStore
from ...core.utils import find_repo_root
from ...tickets.files import list_ticket_paths, read_ticket, ticket_is_done
from ...tickets.outbox import resolve_outbox_paths
from ...tickets.replies import resolve_reply_paths


def _flows_db_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "flows.db"


def _load_flow_store(repo_root: Path) -> Optional[FlowStore]:
    db_path = _flows_db_path(repo_root)
    if not db_path.exists():
        return None
    store = FlowStore(db_path)
    try:
        store.initialize()
    except Exception:
        return None
    return store


def _select_primary_run(records: list[FlowRunRecord]) -> Optional[FlowRunRecord]:
    """Select the primary run for analytics display.

    Only considers the newest run (records[0]). If it's active or paused, return it.
    If the newest run is terminal (completed/stopped/failed), return None to show idle.
    This matches the backend's _active_or_paused_run() logic and prevents showing
    stale data from old paused runs when newer runs have completed.
    """
    if not records:
        return None
    newest = records[0]
    if (
        FlowRunStatus(newest.status).is_active()
        or FlowRunStatus(newest.status).is_paused()
    ):
        return newest
    return None


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration_seconds(
    started_at: Optional[str], finished_at: Optional[str], status: str
) -> Optional[float]:
    start_dt = _parse_timestamp(started_at)
    if not start_dt:
        return None
    end_dt = _parse_timestamp(finished_at)
    if not end_dt and status in {
        FlowRunStatus.RUNNING.value,
        FlowRunStatus.PAUSED.value,
        FlowRunStatus.PENDING.value,
    }:
        end_dt = datetime.now(timezone.utc)
    if not end_dt:
        return None
    return (end_dt - start_dt).total_seconds()


def _ticket_counts(ticket_dir: Path) -> dict[str, int]:
    total = 0
    done = 0
    for path in list_ticket_paths(ticket_dir):
        total += 1
        try:
            if ticket_is_done(path):
                done += 1
        except Exception:
            # Treat unreadable/invalid tickets as not-done but still count them.
            continue
    todo = max(total - done, 0)
    return {"todo": todo, "done": done, "total": total}


def _count_history_dirs(history_dir: Path) -> int:
    if not history_dir.exists() or not history_dir.is_dir():
        return 0
    count = 0
    try:
        for child in history_dir.iterdir():
            try:
                if child.is_dir() and len(child.name) == 4 and child.name.isdigit():
                    count += 1
            except OSError:
                continue
    except OSError:
        return count
    return count


def _build_summary(repo_root: Path) -> Dict[str, Any]:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    store = _load_flow_store(repo_root)
    records: list[FlowRunRecord] = []
    if store:
        try:
            records = store.list_flow_runs(flow_type="ticket_flow")
        except Exception:
            records = []
        finally:
            try:
                store.close()
            except Exception:
                pass

    run_record = _select_primary_run(records)

    default_run = {
        "id": None,
        "short_id": None,
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "current_step": None,
        "created_at": None,
    }

    run_data: Dict[str, Any] = default_run
    turns: Dict[str, Optional[int]] = {
        "total": None,
        "current_ticket": None,
        "dispatches": 0,
        "replies": 0,
    }
    current_ticket: Optional[str] = None
    agent_id: Optional[str] = None

    if run_record:
        run_data = {
            "id": run_record.id,
            "short_id": run_record.id.split("-")[0] if run_record.id else None,
            "status": run_record.status.value,
            "started_at": run_record.started_at,
            "finished_at": run_record.finished_at,
            "duration_seconds": _duration_seconds(
                run_record.started_at, run_record.finished_at, run_record.status.value
            ),
            "current_step": run_record.current_step,
            "created_at": run_record.created_at,
        }

        state = run_record.state if isinstance(run_record.state, dict) else {}
        ticket_state = state.get("ticket_engine") if isinstance(state, dict) else {}
        if isinstance(ticket_state, dict):
            turns["total"] = ticket_state.get("total_turns")  # type: ignore[index]
            turns["current_ticket"] = ticket_state.get("ticket_turns")  # type: ignore[index]
            current_ticket = ticket_state.get("current_ticket")  # type: ignore[assignment]
            agent_id = ticket_state.get("last_agent_id")  # type: ignore[assignment]

        workspace_value = run_record.input_data.get("workspace_root")
        workspace_root = Path(workspace_value) if workspace_value else repo_root
        runs_dir = Path(
            run_record.input_data.get("runs_dir") or ".codex-autorunner/runs"
        )
        outbox_paths = resolve_outbox_paths(
            workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_record.id
        )
        reply_paths = resolve_reply_paths(
            workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_record.id
        )
        turns["dispatches"] = _count_history_dirs(outbox_paths.dispatch_history_dir)
        turns["replies"] = _count_history_dirs(reply_paths.reply_history_dir)

        # If current ticket is known, read its frontmatter to pick agent id when available.
        if current_ticket:
            current_path = (workspace_root / current_ticket).resolve()
            try:
                doc, _errors = read_ticket(current_path)
                if doc and doc.frontmatter and getattr(doc.frontmatter, "agent", None):
                    agent_id = doc.frontmatter.agent
            except Exception:
                pass

    ticket_counts = _ticket_counts(ticket_dir)

    return {
        "run": run_data,
        "tickets": {
            "todo_count": ticket_counts["todo"],
            "done_count": ticket_counts["done"],
            "total_count": ticket_counts["total"],
            "current_ticket": current_ticket,
        },
        "turns": {
            "total": turns.get("total"),
            "current_ticket": turns.get("current_ticket"),
            "dispatches": turns.get("dispatches"),
            "replies": turns.get("replies"),
        },
        "agent": {
            "id": agent_id,
            "model": None,
        },
    }


def build_analytics_routes() -> APIRouter:
    router = APIRouter(prefix="/api/analytics", tags=["analytics"])

    @router.get("/summary")
    def get_analytics_summary():
        repo_root = find_repo_root()
        data = _build_summary(repo_root)
        return data

    return router


__all__ = ["build_analytics_routes"]
