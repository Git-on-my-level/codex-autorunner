from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ....core.config import load_repo_config
from ....core.flows.failure_diagnostics import (
    format_failure_summary,
    get_failure_payload,
)
from ....core.flows.models import FlowRunStatus
from ....core.flows.store import FlowStore
from ....core.freshness import resolve_stale_threshold_seconds
from ....core.pma_context import build_ticket_flow_run_state
from ....core.ticket_flow_projection import (
    build_canonical_state_v1,
    select_authoritative_run_record,
)
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
)


def latest_dispatch(repo_root: Path, run_id: str, input_data: dict) -> Optional[dict]:
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
            result["turn_summary"] = _dispatch_dict(turn_summary_candidate["dispatch"])
        return result
    except Exception:
        return None


def gather_hub_messages(context: HubAppContext, *, limit: int = 100) -> list[dict]:
    messages: list[dict] = []
    pma_config = getattr(getattr(context, "config", None), "pma", None)
    stale_threshold_seconds = resolve_stale_threshold_seconds(
        getattr(pma_config, "freshness_stale_threshold_seconds", None)
    )
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
                authoritative_record = select_authoritative_run_record(
                    all_runs,
                    preferred_run_id=(
                        str(snap.last_run_id) if snap.last_run_id is not None else None
                    ),
                )
                if authoritative_record is None:
                    continue
                newest_run_id = str(authoritative_record.id)

                for record in [authoritative_record]:
                    if record.status not in active_statuses:
                        continue
                    record_input = dict(record.input_data or {})
                    latest = latest_dispatch(repo_root, str(record.id), record_input)
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
                        "canonical_state_v1": build_canonical_state_v1(
                            repo_root=repo_root,
                            repo_id=snap.id,
                            run_state=run_state,
                            record=record,
                            store=store,
                            preferred_run_id=newest_run_id,
                            stale_threshold_seconds=stale_threshold_seconds,
                        ),
                    }
                    if has_pending_dispatch:
                        latest_dict = latest if latest else {}
                        item_payload: dict[str, Any] = {
                            **base_item,
                            "item_type": "run_dispatch",
                            "next_action": "reply_and_resume",
                            "seq": latest_dict["seq"],
                            "dispatch": latest_dict["dispatch"],
                            "message": latest_dict["dispatch"],
                            "files": latest_dict.get("files") or [],
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
        except Exception:
            continue

    messages.sort(key=lambda m: m.get("run_created_at") or "", reverse=True)
    if limit and limit > 0:
        return messages[: int(limit)]
    return messages
