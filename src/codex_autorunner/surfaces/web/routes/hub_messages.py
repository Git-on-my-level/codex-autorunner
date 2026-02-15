from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from ....core.config import load_repo_config
from ....core.flows.failure_diagnostics import (
    format_failure_summary,
    get_failure_payload,
)
from ....core.flows.models import FlowRunStatus
from ....core.flows.store import FlowStore
from ....core.pma_context import build_ticket_flow_run_state
from ....tickets.files import safe_relpath
from ....tickets.models import Dispatch
from ....tickets.outbox import parse_dispatch, resolve_outbox_paths
from ..app_state import (
    HubAppContext,
    _find_message_resolution,
    _latest_reply_history_seq,
    _load_hub_inbox_dismissals,
    _message_resolution_state,
    _message_resolvable_actions,
    _record_message_resolution,
)


def build_hub_messages_routes(context: HubAppContext) -> APIRouter:
    router = APIRouter()
    hub_dismissal_locks: dict[str, asyncio.Lock] = {}
    hub_dismissal_locks_guard = asyncio.Lock()

    async def _repo_dismissal_lock(repo_root: Path) -> asyncio.Lock:
        key = str(repo_root.resolve())
        async with hub_dismissal_locks_guard:
            lock = hub_dismissal_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                hub_dismissal_locks[key] = lock
            return lock

    @router.get("/hub/messages")
    async def hub_messages(limit: int = 100):
        """Return paused ticket_flow dispatches across all repos.

        The hub inbox is intentionally simple: it surfaces the latest archived
        dispatch for each paused ticket_flow run.
        """

        def _latest_dispatch(
            repo_root: Path, run_id: str, input_data: dict
        ) -> Optional[dict]:
            try:
                workspace_root = Path(input_data.get("workspace_root") or repo_root)
                runs_dir = Path(input_data.get("runs_dir") or ".codex-autorunner/runs")
                outbox_paths = resolve_outbox_paths(
                    workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
                )
                history_dir = outbox_paths.dispatch_history_dir
                if not history_dir.exists() or not history_dir.is_dir():
                    return None

                def _dispatch_dict(dispatch: Dispatch) -> dict:
                    return {
                        "mode": dispatch.mode,
                        "title": dispatch.title,
                        "body": dispatch.body,
                        "extra": dispatch.extra,
                        "is_handoff": dispatch.is_handoff,
                    }

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

                seq_dirs: list[Path] = []
                for child in history_dir.iterdir():
                    if not child.is_dir():
                        continue
                    name = child.name
                    if len(name) == 4 and name.isdigit():
                        seq_dirs.append(child)
                if not seq_dirs:
                    return None

                seq_dirs = sorted(seq_dirs, key=lambda p: p.name, reverse=True)
                latest_seq = int(seq_dirs[0].name) if seq_dirs else None
                handoff_candidate: Optional[dict] = None
                non_summary_candidate: Optional[dict] = None
                turn_summary_candidate: Optional[dict] = None
                error_candidate: Optional[dict] = None

                for seq_dir in seq_dirs:
                    seq = int(seq_dir.name)
                    dispatch_path = seq_dir / "DISPATCH.md"
                    dispatch, errors = parse_dispatch(dispatch_path)
                    if errors or dispatch is None:
                        if latest_seq is not None and seq == latest_seq:
                            return {
                                "seq": seq,
                                "dir": safe_relpath(seq_dir, repo_root),
                                "dispatch": None,
                                "errors": errors,
                                "files": [],
                            }
                        if error_candidate is None:
                            error_candidate = {
                                "seq": seq,
                                "dir": seq_dir,
                                "errors": errors,
                            }
                        continue
                    candidate = {"seq": seq, "dir": seq_dir, "dispatch": dispatch}
                    if dispatch.is_handoff and handoff_candidate is None:
                        handoff_candidate = candidate
                    if (
                        dispatch.mode != "turn_summary"
                        and non_summary_candidate is None
                    ):
                        non_summary_candidate = candidate
                    if (
                        dispatch.mode == "turn_summary"
                        and turn_summary_candidate is None
                    ):
                        turn_summary_candidate = candidate
                    if (
                        handoff_candidate
                        and non_summary_candidate
                        and turn_summary_candidate
                    ):
                        break

                selected = (
                    handoff_candidate or non_summary_candidate or turn_summary_candidate
                )
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
                dispatch = selected["dispatch"]
                result = {
                    "seq": selected["seq"],
                    "dir": safe_relpath(selected_dir, repo_root),
                    "dispatch": _dispatch_dict(dispatch),
                    "errors": [],
                    "files": _list_files(selected_dir),
                }
                if turn_summary_candidate is not None:
                    result["turn_summary_seq"] = turn_summary_candidate["seq"]
                    result["turn_summary"] = _dispatch_dict(
                        turn_summary_candidate["dispatch"]
                    )
                return result
            except Exception:
                return None

        def _gather() -> list[dict]:
            messages: list[dict] = []
            try:
                snapshots = context.supervisor.list_repos()
            except Exception:
                return []

            for snap in snapshots:
                if not (snap.initialized and snap.exists_on_disk):
                    continue
                dismissals = _load_hub_inbox_dismissals(snap.path)
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
                except Exception:
                    continue
                newest_run_id: Optional[str] = None
                newest_created_at: Optional[str] = None
                for rec in all_runs:
                    rec_created = str(rec.created_at or "")
                    rec_id = str(rec.id)
                    if (
                        newest_created_at is None
                        or rec_created > newest_created_at
                        or (
                            rec_created == newest_created_at
                            and rec_id > (newest_run_id or "")
                        )
                    ):
                        newest_created_at = rec_created
                        newest_run_id = rec_id
                for record in all_runs:
                    if record.status not in active_statuses:
                        continue
                    if (
                        newest_run_id is not None
                        and str(record.id) != newest_run_id
                        and record.status == FlowRunStatus.PAUSED
                    ):
                        continue
                    record_input = dict(record.input_data or {})
                    latest = _latest_dispatch(repo_root, str(record.id), record_input)
                    seq = int(latest.get("seq") or 0) if isinstance(latest, dict) else 0
                    latest_reply_seq = _latest_reply_history_seq(
                        repo_root, str(record.id), record_input
                    )
                    dispatch_mode = None
                    if latest and latest.get("dispatch"):
                        dispatch_mode = latest["dispatch"].get("mode")
                    has_pending_dispatch = bool(
                        latest
                        and latest.get("dispatch")
                        and seq > 0
                        and latest_reply_seq < seq
                        and dispatch_mode != "turn_summary"
                    )

                    dispatch_state_reason = None
                    if (
                        record.status == FlowRunStatus.PAUSED
                        and not has_pending_dispatch
                    ):
                        if dispatch_mode == "turn_summary":
                            dispatch_state_reason = (
                                "Run is paused with an informational turn summary"
                            )
                        elif latest and latest.get("errors"):
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
                        has_pending_dispatch=has_pending_dispatch,
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
                        if has_pending_dispatch:
                            pass
                        else:
                            continue

                    failure_payload = get_failure_payload(record)
                    failure_summary = (
                        format_failure_summary(failure_payload)
                        if failure_payload
                        else None
                    )
                    base_item = {
                        "repo_id": snap.id,
                        "repo_display_name": snap.display_name,
                        "repo_path": str(snap.path),
                        "run_id": record.id,
                        "run_created_at": record.created_at,
                        "status": record.status.value,
                        "failure": failure_payload,
                        "failure_summary": failure_summary,
                        "open_url": f"/repos/{snap.id}/?tab=inbox&run_id={record.id}",
                        "run_state": run_state,
                    }
                    if has_pending_dispatch:
                        item_payload: dict[str, Any] = {
                            **base_item,
                            "item_type": "run_dispatch",
                            "next_action": "reply_and_resume",
                            "seq": latest["seq"],
                            "dispatch": latest["dispatch"],
                            "message": latest["dispatch"],
                            "files": latest.get("files") or [],
                            "dispatch_actionable": True,
                        }
                    else:
                        fallback_dispatch = latest.get("dispatch") if latest else None
                        item_type = "run_state_attention"
                        next_action = "inspect_and_resume"
                        if record.status == FlowRunStatus.FAILED:
                            item_type = "run_failed"
                            next_action = "diagnose_or_restart"
                        elif record.status == FlowRunStatus.STOPPED:
                            item_type = "run_stopped"
                            next_action = "diagnose_or_restart"
                        item_payload = {
                            **base_item,
                            "item_type": item_type,
                            "next_action": next_action,
                            "seq": seq if seq > 0 else None,
                            "dispatch": fallback_dispatch,
                            "message": fallback_dispatch
                            or {
                                "title": "Run requires attention",
                                "body": dispatch_state_reason or "",
                            },
                            "files": latest.get("files") if latest else [],
                            "reason": dispatch_state_reason,
                            "available_actions": run_state.get(
                                "recommended_actions", []
                            ),
                            "dispatch_actionable": False,
                        }

                    item_type = str(item_payload.get("item_type") or "run_dispatch")
                    item_seq_raw = item_payload.get("seq")
                    item_seq = (
                        int(item_seq_raw)
                        if isinstance(item_seq_raw, int)
                        else (
                            int(item_seq_raw)
                            if isinstance(item_seq_raw, str)
                            and item_seq_raw.isdigit()
                            and int(item_seq_raw) > 0
                            else None
                        )
                    )
                    if _find_message_resolution(
                        dismissals,
                        run_id=str(record.id),
                        item_type=item_type,
                        seq=item_seq,
                    ):
                        continue

                    item_payload["resolution_state"] = _message_resolution_state(
                        item_type
                    )
                    item_payload["resolvable_actions"] = _message_resolvable_actions(
                        item_type
                    )
                    messages.append(item_payload)

            messages.sort(key=lambda m: m.get("run_created_at") or "", reverse=True)
            if limit and limit > 0:
                return messages[: int(limit)]
            return messages

        items = await asyncio.to_thread(_gather)
        return {"items": items}

    @router.post("/hub/messages/dismiss")
    async def dismiss_hub_message(payload: dict[str, Any]):
        repo_id = str(payload.get("repo_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        seq_raw = payload.get("seq")
        reason_raw = payload.get("reason")
        reason = str(reason_raw).strip() if isinstance(reason_raw, str) else ""
        if not repo_id:
            raise HTTPException(status_code=400, detail="Missing repo_id")
        if not run_id:
            raise HTTPException(status_code=400, detail="Missing run_id")
        try:
            seq = int(seq_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid seq") from None
        if seq <= 0:
            raise HTTPException(status_code=400, detail="Invalid seq")

        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        snapshot = next((s for s in snapshots if s.id == repo_id), None)
        if snapshot is None or not snapshot.exists_on_disk:
            raise HTTPException(status_code=404, detail="Repo not found")

        repo_lock = await _repo_dismissal_lock(snapshot.path)
        async with repo_lock:
            dismissed = _record_message_resolution(
                repo_root=snapshot.path,
                repo_id=repo_id,
                run_id=run_id,
                item_type="run_dispatch",
                seq=seq,
                action="dismiss",
                reason=reason or None,
                actor="hub_messages_dismiss",
            )
            dismissed_at = str(dismissed.get("resolved_at") or "")
            dismissed["dismissed_at"] = dismissed_at
        return {
            "status": "ok",
            "dismissed": dismissed,
        }

    @router.post("/hub/messages/resolve")
    async def resolve_hub_message(payload: dict[str, Any]):
        repo_id = str(payload.get("repo_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        item_type = str(payload.get("item_type") or "").strip()
        action = str(payload.get("action") or "dismiss").strip() or "dismiss"
        reason_raw = payload.get("reason")
        actor_raw = payload.get("actor")
        reason = str(reason_raw).strip() if isinstance(reason_raw, str) else ""
        actor = str(actor_raw).strip() if isinstance(actor_raw, str) else ""
        seq_raw = payload.get("seq")
        seq: Optional[int] = None
        if seq_raw is not None and seq_raw != "":
            try:
                parsed = int(seq_raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Invalid seq") from None
            if parsed <= 0:
                raise HTTPException(status_code=400, detail="Invalid seq")
            seq = parsed

        if not repo_id:
            raise HTTPException(status_code=400, detail="Missing repo_id")
        if not run_id:
            raise HTTPException(status_code=400, detail="Missing run_id")
        if action not in {"dismiss"}:
            raise HTTPException(status_code=400, detail="Unsupported action")

        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        snapshot = next((s for s in snapshots if s.id == repo_id), None)
        if snapshot is None or not snapshot.exists_on_disk:
            raise HTTPException(status_code=404, detail="Repo not found")

        if not item_type:
            hub_payload = await hub_messages(limit=2000)
            items_raw = (
                hub_payload.get("items", []) if isinstance(hub_payload, dict) else []
            )
            matched = None
            for item in items_raw if isinstance(items_raw, list) else []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("repo_id") or "") != repo_id:
                    continue
                if str(item.get("run_id") or "") != run_id:
                    continue
                candidate_seq = item.get("seq")
                if seq is not None and candidate_seq != seq:
                    continue
                matched = item
                break
            if matched is None:
                raise HTTPException(status_code=404, detail="Hub message not found")
            item_type = str(matched.get("item_type") or "").strip() or "run_dispatch"
            if seq is None:
                matched_seq = matched.get("seq")
                if isinstance(matched_seq, int) and matched_seq > 0:
                    seq = matched_seq
        elif item_type == "run_dispatch" and seq is None:
            raise HTTPException(status_code=400, detail="Missing seq for run_dispatch")

        repo_lock = await _repo_dismissal_lock(snapshot.path)
        async with repo_lock:
            resolved = _record_message_resolution(
                repo_root=snapshot.path,
                repo_id=repo_id,
                run_id=run_id,
                item_type=item_type,
                seq=seq,
                action=action,
                reason=reason or None,
                actor=actor or "hub_messages_resolve",
            )
        return {"status": "ok", "resolved": resolved}

    return router


__all__ = ["build_hub_messages_routes"]
