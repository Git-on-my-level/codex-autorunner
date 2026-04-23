from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path
from typing import Any, Mapping, Optional, TypedDict

from ..flows.ticket_flow.runtime_helpers import ticket_flow_inbox_preflight
from ..tickets.files import safe_relpath
from ..tickets.models import Dispatch
from ..tickets.outbox import parse_dispatch, resolve_outbox_paths
from ..tickets.replies import resolve_reply_paths
from .config import load_repo_config
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
from .state_roots import resolve_repo_flows_db_path
from .ticket_flow_projection import select_authoritative_run_record
from .ticket_flow_summary import build_ticket_flow_summary

_logger = logging.getLogger(__name__)


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
    except (TypeError, ValueError):
        raw = str(extra)
    if len(raw) <= limit:
        return extra
    return {
        "_omitted": True,
        "note": "extra omitted due to size",
        "preview": _truncate(raw, limit),
    }


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


PMA_MAX_TEXT = 800


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
    except (ValueError, OSError) as exc:
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
    except (ValueError, OSError) as exc:
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
    archive_cmd = f"car ticket-flow archive --repo {quoted_repo} --run-id {run_id}"
    status_cmd = f"car ticket-flow status --repo {quoted_repo} --run-id {run_id}"
    resume_cmd = f"car ticket-flow start --repo {quoted_repo}"
    start_cmd = f"car ticket-flow start --repo {quoted_repo}"
    stop_cmd = f"car ticket-flow stop --repo {quoted_repo} --run-id {run_id}"

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
        except (ValueError, OSError) as exc:
            _logger.warning("Could not check worker health: %s", exc)
            health = None
            dead_worker = False

    crash_info = None
    crash_summary = None
    if dead_worker:
        try:
            crash_info = read_worker_crash_info(repo_root, run_id)
        except (
            Exception
        ) as exc:  # intentional: defensive guard; read_worker_crash_info handles known errors internally
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
    repo_root: Path,
    repo_id: str,
    *,
    store: Optional[FlowStore] = None,
) -> tuple[Optional[TicketFlowRunState], Optional[FlowRunRecord]]:
    def _load_from_store(
        active_store: FlowStore,
    ) -> tuple[Optional[TicketFlowRunState], Optional[FlowRunRecord]]:
        records = active_store.list_flow_runs(flow_type="ticket_flow")
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
            store=active_store,
            has_pending_dispatch=has_dispatch,
            dispatch_state_reason=reason,
        )
        return run_state, record

    db_path = resolve_repo_flows_db_path(repo_root)
    if store is None and not db_path.exists():
        return None, None
    try:
        if store is not None:
            return _load_from_store(store)
        config = load_repo_config(repo_root)
        with FlowStore(db_path, durable=config.durable_writes) as local_store:
            return _load_from_store(local_store)
    except (
        Exception
    ) as exc:  # intentional: top-level guard spanning config, db, fs, and validation errors
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
