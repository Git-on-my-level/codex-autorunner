from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .....core.file_chat_keys import ticket_stable_id
from .....core.flows import FlowEventType
from .....tickets.files import list_ticket_paths
from ...services.ticket_read_models import (
    enrich_current_ticket_payload as _enrich_current_ticket_payload,
)
from ...services.ticket_read_models import (
    mark_duplicate_ticket_numbers as _mark_duplicate_ticket_numbers,
)
from ...services.ticket_read_models import (
    run_diff_stats as _run_diff_stats,
)
from ...services.ticket_read_models import (
    run_ticket_aliases as _run_ticket_aliases,
)
from ...services.ticket_read_models import (
    ticket_aliases as _ticket_aliases,
)
from ...services.ticket_read_models import (
    ticket_number_sort_value as _ticket_number_sort_value,
)
from ...services.ticket_read_models import (
    ticket_payload as _ticket_payload,
)

if TYPE_CHECKING:
    from fastapi import APIRouter

    from ...app_state import HubAppContext


def _aggregate_diff_stats_by_ticket_ref(
    store: object, workspace_root: Path
) -> dict[str, dict[str, int]]:
    """Cumulative diff totals keyed by ticket_key/ticket_path/ticket_id from DIFF_UPDATED events."""
    from ...services import flow_store as flow_store_service
    from ..flows import _ticket_diff_event_ref

    diff_by_ref: dict[str, dict[str, int]] = {}
    runs = flow_store_service.safe_list_flow_runs(
        workspace_root.resolve(),
        flow_type="ticket_flow",
        recover_stuck=True,
    )
    get_events = getattr(store, "get_events_by_type", None)
    if get_events is None:
        return diff_by_ref
    for run in runs:
        try:
            events = get_events(run.id, FlowEventType.DIFF_UPDATED)
        except sqlite3.Error:
            continue
        for ev in events:
            data = getattr(ev, "data", None) or {}
            if not isinstance(data, dict):
                continue
            ref = _ticket_diff_event_ref(data)
            if not ref:
                continue
            stats = diff_by_ref.setdefault(
                ref,
                {"insertions": 0, "deletions": 0, "files_changed": 0},
            )
            stats["insertions"] += int(data.get("insertions") or 0)
            stats["deletions"] += int(data.get("deletions") or 0)
            stats["files_changed"] += int(data.get("files_changed") or 0)
    return diff_by_ref


def build_hub_ticket_router(context: HubAppContext) -> APIRouter:
    from fastapi import APIRouter

    from ..flows import _merge_ticket_diff_stats

    router = APIRouter()

    @router.get("/hub/tickets")
    async def list_hub_tickets(
        repo: Optional[str] = None,
        worktree: Optional[str] = None,
        status: Optional[str] = None,
    ):
        from .....core.flows.store import FlowStore
        from .....core.pma_context import get_latest_ticket_flow_run_state_with_record

        requested_status = (status or "").strip().lower() or None
        snapshots = context.supervisor.list_repos(use_cache=True)
        tickets: list[dict[str, object]] = []
        for snapshot in snapshots:
            workspace_kind = "worktree" if snapshot.kind == "worktree" else "repo"
            workspace_root = snapshot.path.resolve()
            ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
            if not ticket_dir.exists():
                continue
            repo_id = (
                snapshot.worktree_of if workspace_kind == "worktree" else snapshot.id
            )
            worktree_id = snapshot.id if workspace_kind == "worktree" else None
            if repo and (workspace_kind != "repo" or snapshot.id != repo):
                continue
            if worktree and (workspace_kind != "worktree" or worktree_id != worktree):
                continue
            store = None
            run_state: Any = None
            run_record: Any = None
            diff_by_ref: dict[str, dict[str, int]] = {}
            db_path = workspace_root / ".codex-autorunner" / "flows.db"
            if db_path.exists():
                try:
                    store = FlowStore.connect_readonly(db_path)
                    store.initialize()
                    run_state, run_record = (
                        get_latest_ticket_flow_run_state_with_record(
                            workspace_root,
                            snapshot.id,
                            store=store,
                        )
                    )
                    try:
                        diff_by_ref = _aggregate_diff_stats_by_ticket_ref(
                            store, workspace_root
                        )
                    except Exception:
                        diff_by_ref = {}
                except Exception:
                    run_state = None
                    run_record = None
                    diff_by_ref = {}
            workspace_payloads: list[dict[str, object]] = []
            for path in list_ticket_paths(ticket_dir):
                payload = _ticket_payload(
                    hub_root=context.config.root,
                    workspace_root=workspace_root,
                    ticket_dir=ticket_dir,
                    workspace_kind=workspace_kind,
                    workspace_id=snapshot.id,
                    repo_id=repo_id,
                    worktree_id=worktree_id,
                    path=path,
                )
                if not payload:
                    continue
                stable = ticket_stable_id(path.resolve())
                ticket_path_val = payload.get("ticket_path") or payload.get("path")
                diff_refs: list[str] = []
                if stable:
                    diff_refs.append(stable)
                if isinstance(ticket_path_val, str) and ticket_path_val.strip():
                    raw_path = ticket_path_val.strip()
                    if raw_path not in diff_refs:
                        diff_refs.append(raw_path)
                merged = _merge_ticket_diff_stats(diff_refs, diff_by_ref)
                if merged is not None:
                    payload["diff_stats"] = merged
                elif (
                    store is not None
                    and run_record is not None
                    and (
                        _ticket_aliases(payload)
                        & _run_ticket_aliases(run_state, run_record)
                    )
                ):
                    fallback = _run_diff_stats(store, str(run_record.id))
                    if fallback is not None:
                        payload["diff_stats"] = fallback
                _enrich_current_ticket_payload(
                    payload,
                    run_state=run_state,
                    run_record=run_record,
                )
                workspace_payloads.append(payload)
            _mark_duplicate_ticket_numbers(workspace_payloads)
            for payload in workspace_payloads:
                if requested_status and str(payload.get("status")) != requested_status:
                    continue
                tickets.append(payload)
            if store is not None:
                try:
                    store.close()
                except OSError:
                    pass

        tickets.sort(
            key=lambda item: (
                str(item.get("workspace_kind") or ""),
                str(item.get("workspace_id") or ""),
                _ticket_number_sort_value(item.get("ticket_number")),
                str(item.get("ticket_path") or ""),
            )
        )
        return {"tickets": tickets}

    return router
