from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

from .....core.file_chat_keys import ticket_stable_id
from .....core.flows import FlowEventType
from .....tickets.files import list_ticket_paths, read_ticket, safe_relpath
from .....tickets.frontmatter import (
    deterministic_ticket_id,
    parse_markdown_frontmatter,
    sanitize_ticket_id,
    split_markdown_frontmatter,
)
from .....tickets.lint import parse_ticket_index

if TYPE_CHECKING:
    from fastapi import APIRouter

    from ...app_state import HubAppContext


def _ticket_status(frontmatter: dict[str, object] | None, errors: list[str]) -> str:
    if errors:
        return "invalid"
    if bool((frontmatter or {}).get("done")):
        return "done"
    return "idle"


def _ticket_number_sort_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def _string_values(*values: object) -> set[str]:
    result: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            result.add(text)
            name = Path(text).name
            result.add(name)
            if name.endswith(".md"):
                result.add(name[:-3])
    return {value.lower() for value in result if value}


def _ticket_aliases(payload: dict[str, object]) -> set[str]:
    frontmatter = payload.get("frontmatter")
    fm = frontmatter if isinstance(frontmatter, dict) else {}
    aliases = _string_values(
        payload.get("id"),
        payload.get("ticket_id"),
        payload.get("source_ticket_id"),
        payload.get("path"),
        payload.get("ticket_path"),
        fm.get("ticket_id"),
    )
    ticket_number = payload.get("ticket_number") or payload.get("index")
    if isinstance(ticket_number, int):
        aliases.update({str(ticket_number), f"ticket-{ticket_number:03d}".lower()})
    return aliases


def _run_ticket_aliases(run_state: object, record: object) -> set[str]:
    state = run_state if isinstance(run_state, dict) else {}
    ticket_engine = state.get("ticket_engine")
    engine = ticket_engine if isinstance(ticket_engine, dict) else {}
    record_state = getattr(record, "state", None)
    raw_record_state = record_state if isinstance(record_state, dict) else {}
    raw_record_engine = raw_record_state.get("ticket_engine")
    record_engine = raw_record_engine if isinstance(raw_record_engine, dict) else {}
    return _string_values(
        state.get("current_ticket"),
        state.get("current_ticket_id"),
        state.get("effective_current_ticket"),
        engine.get("current_ticket"),
        engine.get("current_ticket_id"),
        raw_record_state.get("current_ticket"),
        raw_record_state.get("current_ticket_id"),
        record_engine.get("current_ticket"),
        record_engine.get("current_ticket_id"),
    )


def _run_diff_stats(store: object, run_id: str) -> Optional[dict[str, int]]:
    get_events = getattr(store, "get_events_by_type", None)
    if get_events is None:
        return None
    totals = {"insertions": 0, "deletions": 0, "files_changed": 0}
    try:
        events = get_events(run_id, FlowEventType.DIFF_UPDATED)
    except Exception:
        return None
    for event in events:
        data = getattr(event, "data", None) or {}
        if not isinstance(data, dict):
            continue
        totals["insertions"] += int(data.get("insertions") or 0)
        totals["deletions"] += int(data.get("deletions") or 0)
        totals["files_changed"] += int(data.get("files_changed") or 0)
    return totals if any(totals.values()) else None


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


def _enrich_current_ticket_payload(
    payload: dict[str, object],
    *,
    run_state: object,
    run_record: object,
) -> None:
    from .....core.flows.models import FlowRunRecord, flow_run_duration_seconds

    if not run_record:
        return
    if not (_ticket_aliases(payload) & _run_ticket_aliases(run_state, run_record)):
        return
    payload["run_id"] = getattr(run_record, "id", None)
    payload["duration_seconds"] = flow_run_duration_seconds(
        cast(FlowRunRecord, run_record)
    )
    flow_status = (
        run_state.get("flow_status")
        if isinstance(run_state, dict)
        else getattr(run_record, "status", None)
    )
    if isinstance(flow_status, str) and flow_status.strip():
        payload["status"] = flow_status


def _mark_duplicate_ticket_numbers(payloads: list[dict[str, object]]) -> None:
    by_number: dict[int, list[dict[str, object]]] = {}
    for payload in payloads:
        number = payload.get("ticket_number")
        if not isinstance(number, int):
            continue
        by_number.setdefault(number, []).append(payload)

    for number, duplicates in by_number.items():
        if len(duplicates) < 2:
            continue
        paths = ", ".join(
            str(item.get("ticket_path") or item.get("path")) for item in duplicates
        )
        message = (
            f"Duplicate ticket index {number:03d}: multiple files share the same index "
            f"({paths}). Rename or remove duplicates to ensure deterministic ordering."
        )
        for payload in duplicates:
            errors = payload.get("errors")
            if not isinstance(errors, list):
                errors = []
                payload["errors"] = errors
            errors.append(message)
            payload["status"] = _ticket_status(None, [message])


def _ticket_payload(
    *,
    hub_root: Path,
    workspace_root: Path,
    ticket_dir: Path,
    workspace_kind: str,
    workspace_id: str,
    repo_id: Optional[str],
    worktree_id: Optional[str],
    path: Path,
) -> dict[str, object]:
    resolved_path = path.resolve()
    try:
        if not resolved_path.is_relative_to(ticket_dir.resolve()):
            return {}
    except ValueError:
        return {}
    doc, errors = read_ticket(path)
    idx = getattr(doc, "index", None) or parse_ticket_index(path.name)
    parsed_frontmatter: dict[str, object] = {}
    parsed_body: str | None = None
    raw_frontmatter_yaml: str | None = None
    if doc is None:
        try:
            raw_body = path.read_text(encoding="utf-8")
            raw_frontmatter_yaml, _ = split_markdown_frontmatter(raw_body)
            parsed_frontmatter, parsed_body = parse_markdown_frontmatter(raw_body)
        except (OSError, ValueError):
            parsed_frontmatter, parsed_body, raw_frontmatter_yaml = {}, None, None
    else:
        try:
            raw_body = path.read_text(encoding="utf-8")
            raw_frontmatter_yaml, _ = split_markdown_frontmatter(raw_body)
        except OSError:
            raw_frontmatter_yaml = None

    frontmatter = asdict(doc.frontmatter) if doc else parsed_frontmatter
    source_ticket_id = sanitize_ticket_id(
        frontmatter.get("ticket_id")
    ) or deterministic_ticket_id(path)
    ticket_path = safe_relpath(path, workspace_root)
    global_ticket_id = f"{workspace_kind}:{workspace_id}:{source_ticket_id}"
    try:
        workspace_path = str(workspace_root.relative_to(hub_root))
    except ValueError:
        workspace_path = str(workspace_root)

    return {
        "id": global_ticket_id,
        "ticket_id": source_ticket_id,
        "source_ticket_id": source_ticket_id,
        "path": ticket_path,
        "ticket_path": ticket_path,
        "index": idx,
        "ticket_number": idx,
        "chat_key": f"ticket:{idx}:{source_ticket_id}" if idx else None,
        "frontmatter": frontmatter,
        "frontmatter_yaml": raw_frontmatter_yaml,
        "body": doc.body if doc else parsed_body,
        "errors": errors,
        "status": _ticket_status(frontmatter, errors),
        "workspace_kind": workspace_kind,
        "workspace_id": workspace_id,
        "workspace_path": workspace_path,
        "hub_root": str(hub_root),
        "workspace_root": str(workspace_root),
        "repo_id": repo_id,
        "worktree_id": worktree_id,
        "base_repo_id": repo_id if workspace_kind == "worktree" else None,
    }


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
