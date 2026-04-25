from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ...tickets.outbox import archive_dispatch, ensure_outbox_dirs, resolve_outbox_paths
from ...tickets.replies import resolve_reply_paths
from ..locks import FileLockBusy, file_lock
from ..state_roots import resolve_repo_flows_db_path
from .failure_diagnostics import (
    CANONICAL_FAILURE_REASON_CODE_FIELD,
    ReconcileContext,
    build_failure_event_data,
    ensure_failure_payload,
)
from .flow_transition_telemetry import (
    emit_failure_projection,
    emit_reconcile_noop,
    emit_reconcile_transition,
    emit_recovery_takeover,
)
from .models import FlowEventType, FlowRunRecord, FlowRunStatus
from .store import UNSET, FlowStore
from .transition import resolve_flow_transition
from .worker_process import (
    FlowWorkerHealth,
    check_worker_health,
    clear_worker_metadata,
    read_worker_crash_info,
    write_worker_crash_info,
)
from .workspace_root import resolve_ticket_flow_workspace_root

_logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = (
    FlowRunStatus.RUNNING,
    FlowRunStatus.STOPPING,
    FlowRunStatus.PAUSED,
)

_mtime_cache: dict[Path, tuple[float, int, int, int]] = {}


def _db_mtime_key(db_path: Path) -> tuple[float, int]:
    try:
        st = db_path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, 0)


def _reconcile_skip_signature(store: FlowStore) -> tuple[float, int, int, int]:
    mtime, size = _db_mtime_key(store.db_path)
    return (
        mtime,
        size,
        store.count_flow_runs_total(),
        store.count_flow_events_total(),
    )


def _should_skip_reconcile(db_path: Path, current: tuple[float, int, int, int]) -> bool:
    cached = _mtime_cache.get(db_path)
    if cached is None:
        return False
    if current != cached:
        return False
    return True


def _record_reconcile_mtime(
    db_path: Path, signature: tuple[float, int, int, int]
) -> None:
    _mtime_cache[db_path] = signature


@dataclass
class FlowReconcileSummary:
    checked: int = 0
    active: int = 0
    updated: int = 0
    locked: int = 0
    superseded: int = 0
    errors: int = 0


@dataclass
class FlowReconcileResult:
    records: list[FlowRunRecord]
    summary: FlowReconcileSummary


def _reconcile_lock_path(repo_root: Path, run_id: str) -> Path:
    return repo_root / ".codex-autorunner" / "flows" / run_id / "reconcile.lock"


def _ensure_worker_not_stale(health: FlowWorkerHealth) -> None:
    if health.status in {"dead", "mismatch", "invalid"}:
        try:
            clear_worker_metadata(health.artifact_path.parent)
        except (OSError, RuntimeError):
            _logger.debug("Failed to clear worker metadata: %s", health.artifact_path)


def _latest_app_server_event_details(
    store: FlowStore, run_id: str
) -> tuple[Optional[str], Optional[str]]:
    try:
        event = store.get_last_telemetry_by_type(run_id, FlowEventType.APP_SERVER_EVENT)
    except (sqlite3.Error, ValueError, TypeError, RuntimeError) as exc:
        _logger.debug("Failed to get last app server event: %s", exc)
        return None, None
    if event is None:
        return None, None
    data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
    message_raw = data.get("message")
    message: dict[str, Any] = message_raw if isinstance(message_raw, dict) else {}
    method_raw = message.get("method")
    method = (
        method_raw.strip()
        if isinstance(method_raw, str) and method_raw.strip()
        else None
    )
    turn_raw = data.get("turn_id")
    turn_id = (
        turn_raw.strip() if isinstance(turn_raw, str) and turn_raw.strip() else None
    )
    if turn_id is None:
        params_raw = message.get("params")
        params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}
        candidate = params.get("turn_id") or params.get("turnId")
        if isinstance(candidate, str) and candidate.strip():
            turn_id = candidate.strip()
    return method, turn_id


def _latest_seq(history_dir: Path) -> int:
    if not history_dir.exists() or not history_dir.is_dir():
        return 0
    latest = 0
    for child in history_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name.strip()
        if not name.isdigit():
            continue
        latest = max(latest, int(name))
    return latest


def _resolve_workspace_root(repo_root: Path, record: FlowRunRecord) -> Path:
    input_data = record.input_data if isinstance(record.input_data, dict) else {}
    return resolve_ticket_flow_workspace_root(input_data, repo_root)


def _ensure_worker_crash_artifact(
    store: FlowStore,
    run_id: str,
    crash_path: Path,
    *,
    crash_info: Optional[dict[str, Any]] = None,
) -> None:
    try:
        existing = store.get_artifacts(run_id)
    except (sqlite3.Error, ValueError, TypeError, RuntimeError) as exc:
        _logger.debug("Failed to get artifacts for %s: %s", run_id, exc)
        existing = []
    for art in existing:
        if art.kind == "worker_crash":
            return
    try:
        store.create_artifact(
            artifact_id=str(uuid.uuid4()),
            run_id=run_id,
            kind="worker_crash",
            path=str(crash_path),
            metadata={
                "summary": (
                    crash_info.get("exception")
                    if isinstance(crash_info, dict)
                    else None
                ),
                "timestamp": (
                    crash_info.get("timestamp")
                    if isinstance(crash_info, dict)
                    else None
                ),
            },
        )
    except (sqlite3.Error, ValueError, TypeError) as exc:
        _logger.warning("Failed to create crash artifact for %s: %s", run_id, exc)


def _is_stale_crash_info(crash_info: Optional[dict[str, Any]]) -> bool:
    if not isinstance(crash_info, dict):
        return True
    useful_fields = (
        "exit_code",
        "signal",
        "stderr_tail",
        "exception",
        "stack_trace",
        "last_event",
    )
    has_useful_data = False
    for field in useful_fields:
        value = crash_info.get(field)
        if value is not None and value != "":
            has_useful_data = True
            break
    return not has_useful_data


def _ensure_crash_payload(
    repo_root: Path,
    record: FlowRunRecord,
    store: FlowStore,
    health: FlowWorkerHealth,
) -> Optional[dict[str, Any]]:
    raw_crash_info = getattr(health, "crash_info", None)
    crash_info = dict(raw_crash_info) if isinstance(raw_crash_info, dict) else None
    if crash_info is None:
        crash_info = read_worker_crash_info(repo_root, record.id)
    should_write = crash_info is None or (
        health.status == "dead" and _is_stale_crash_info(crash_info)
    )
    if should_write:
        last_method, _ = _latest_app_server_event_details(store, record.id)
        crash_path = write_worker_crash_info(
            repo_root,
            record.id,
            worker_pid=health.pid,
            exit_code=getattr(health, "exit_code", None),
            last_event=last_method,
            stderr_tail=getattr(health, "stderr_tail", None),
            exception=record.error_message,
        )
        if crash_path is not None:
            crash_info = read_worker_crash_info(repo_root, record.id)
    crash_path = (
        repo_root / ".codex-autorunner" / "flows" / str(record.id) / "crash.json"
    )
    if crash_path.exists():
        _ensure_worker_crash_artifact(
            store, record.id, crash_path, crash_info=crash_info
        )
    return crash_info


def _crash_dispatch_body(
    record: FlowRunRecord,
    *,
    crash_info: Optional[dict[str, Any]],
) -> str:
    lines = [
        "The ticket worker stopped unexpectedly and no actionable dispatch was available.",
        "",
        f"run_id: {record.id}",
    ]
    if isinstance(crash_info, dict):
        last_event = crash_info.get("last_event")
        if isinstance(last_event, str) and last_event.strip():
            lines.append(f"last_event: {last_event.strip()}")
        exit_code = crash_info.get("exit_code")
        if isinstance(exit_code, int):
            lines.append(f"exit_code: {exit_code}")
        signal = crash_info.get("signal")
        if isinstance(signal, str) and signal.strip():
            lines.append(f"signal: {signal.strip()}")
        stderr_tail = crash_info.get("stderr_tail")
        if isinstance(stderr_tail, str) and stderr_tail.strip():
            lines.extend(["", "stderr tail:", "```", stderr_tail.strip(), "```"])
        exception = crash_info.get("exception")
        if isinstance(exception, str) and exception.strip():
            lines.append(f"exception: {exception.strip()}")
    lines.extend(
        [
            "",
            "Crash artifact:",
            f"- `.codex-autorunner/flows/{record.id}/crash.json`",
            "",
            "Please inspect the crash artifact and decide whether to resume or restart the run.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _ensure_crash_dispatch(
    repo_root: Path,
    record: FlowRunRecord,
    *,
    crash_info: Optional[dict[str, Any]],
) -> None:
    if record.flow_type != "ticket_flow":
        return
    workspace_root = _resolve_workspace_root(repo_root, record)
    outbox_paths = resolve_outbox_paths(workspace_root=workspace_root, run_id=record.id)
    reply_paths = resolve_reply_paths(workspace_root=workspace_root, run_id=record.id)
    ensure_outbox_dirs(outbox_paths)
    reply_paths.reply_history_dir.mkdir(parents=True, exist_ok=True)
    latest_dispatch = _latest_seq(outbox_paths.dispatch_history_dir)
    latest_reply = _latest_seq(reply_paths.reply_history_dir)
    if latest_dispatch > latest_reply:
        return

    dispatch_frontmatter = "---\nmode: pause\ntitle: Worker crashed\n---\n\n"
    outbox_paths.dispatch_path.write_text(
        dispatch_frontmatter + _crash_dispatch_body(record, crash_info=crash_info),
        encoding="utf-8",
    )
    current_ticket = None
    state = record.state if isinstance(record.state, dict) else {}
    ticket_engine = state.get("ticket_engine")
    if isinstance(ticket_engine, dict):
        candidate = ticket_engine.get("current_ticket_id")
        if not (isinstance(candidate, str) and candidate.strip()):
            candidate = ticket_engine.get("current_ticket")
        if isinstance(candidate, str) and candidate.strip():
            current_ticket = candidate.strip()
    archive_dispatch(
        outbox_paths,
        next_seq=latest_dispatch + 1,
        ticket_id=current_ticket,
        repo_id=(
            str(record.metadata.get("repo_id")).strip()
            if isinstance(record.metadata, dict) and record.metadata.get("repo_id")
            else ""
        ),
        run_id=record.id,
        origin="reconcile",
    )


def reconcile_flow_run(
    repo_root: Path,
    record: FlowRunRecord,
    store: FlowStore,
    *,
    logger: Optional[logging.Logger] = None,
) -> tuple[FlowRunRecord, bool, bool]:
    if record.status not in _ACTIVE_STATUSES:
        return record, False, False

    lock_path = _reconcile_lock_path(repo_root, record.id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(lock_path, blocking=False):
            health = check_worker_health(repo_root, record.id)
            crash_info = None
            if health.status in {"dead", "invalid", "mismatch"}:
                crash_info = _ensure_crash_payload(repo_root, record, store, health)
            decision = resolve_flow_transition(record, health)

            if (
                decision.status == record.status
                and decision.finished_at == record.finished_at
                and decision.state == (record.state or {})
                and decision.error_message == record.error_message
            ):
                if record.status == FlowRunStatus.PAUSED and health.status in {
                    "dead",
                    "invalid",
                    "mismatch",
                }:
                    try:
                        _ensure_crash_dispatch(repo_root, record, crash_info=crash_info)
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        (logger or _logger).warning(
                            "Failed to create crash dispatch for %s: %s",
                            record.id,
                            exc,
                        )
                    emit_recovery_takeover(
                        store=store,
                        run_id=record.id,
                        previous_status=record.status,
                        resulting_status=record.status,
                        note="paused-worker-dead-noop",
                        worker_status=health.status,
                        crash_info=crash_info,
                    )
                else:
                    emit_reconcile_noop(
                        store=store,
                        run_id=record.id,
                        status=record.status,
                        note=decision.note or "reconcile-noop",
                        worker_status=health.status,
                    )
                return record, False, False

            is_recovery = health.status in {"dead", "invalid", "mismatch"}

            (logger or _logger).info(
                "Reconciling flow %s: %s -> %s (%s)",
                record.id,
                record.status.value,
                decision.status.value,
                decision.note or "reconcile",
            )

            state = decision.state
            if decision.status == FlowRunStatus.FAILED:
                reconcile_ctx = ReconcileContext(
                    worker_exit_code=getattr(health, "exit_code", None),
                    worker_stderr_tail=getattr(health, "stderr_tail", None),
                    crash_info=crash_info,
                )
                state = ensure_failure_payload(
                    state,
                    record=record,
                    step_id=record.current_step,
                    error_message=decision.error_message,
                    store=store,
                    note=decision.note,
                    failed_at=decision.finished_at,
                    reconcile_context=reconcile_ctx,
                )
            updated = store.update_flow_run_status(
                run_id=record.id,
                status=decision.status,
                state=state,
                finished_at=decision.finished_at if decision.finished_at else UNSET,
                error_message=decision.error_message,
            )

            if is_recovery:
                emit_recovery_takeover(
                    store=store,
                    run_id=record.id,
                    previous_status=record.status,
                    resulting_status=decision.status,
                    note=decision.note or "reconcile-recovery",
                    worker_status=health.status,
                    crash_info=crash_info,
                    error_message=decision.error_message,
                )
            else:
                emit_reconcile_transition(
                    store=store,
                    run_id=record.id,
                    previous_status=record.status,
                    resulting_status=decision.status,
                    note=decision.note or "reconcile",
                    worker_status=health.status,
                    error_message=decision.error_message,
                )

            if decision.status == FlowRunStatus.FAILED and isinstance(state, dict):
                failure = state.get("failure")
                reason_code = (
                    failure.get(CANONICAL_FAILURE_REASON_CODE_FIELD)
                    if isinstance(failure, dict)
                    else None
                )
                emit_failure_projection(
                    store=store,
                    run_id=record.id,
                    status=decision.status,
                    failure_reason_code=reason_code,
                    step_id=record.current_step,
                    error_message=decision.error_message,
                    origin="reconciler",
                )

            if decision.status == FlowRunStatus.FAILED and decision.error_message:
                reconcile_ctx_for_event = ReconcileContext(
                    crash_info=crash_info,
                    last_app_event_method=None,
                    last_turn_id=None,
                )
                last_method, last_turn_id = _latest_app_server_event_details(
                    store, record.id
                )
                reconcile_ctx_for_event.last_app_event_method = last_method
                reconcile_ctx_for_event.last_turn_id = last_turn_id
                failure = state.get("failure") if isinstance(state, dict) else None
                event_data = build_failure_event_data(
                    failure if isinstance(failure, dict) else {},
                    error_message=decision.error_message,
                    note=decision.note,
                    reconcile_context=reconcile_ctx_for_event,
                )
                try:
                    store.create_event(
                        event_id=str(uuid.uuid4()),
                        run_id=record.id,
                        event_type=FlowEventType.FLOW_FAILED,
                        data=event_data,
                    )
                except (sqlite3.Error, ValueError, TypeError) as exc:
                    (logger or _logger).warning(
                        "Failed to emit flow_failed event for %s: %s", record.id, exc
                    )

            if record.status == FlowRunStatus.PAUSED and health.status in {
                "dead",
                "invalid",
                "mismatch",
            }:
                try:
                    _ensure_crash_dispatch(repo_root, record, crash_info=crash_info)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    (logger or _logger).warning(
                        "Failed to create crash dispatch for %s: %s", record.id, exc
                    )

            _ensure_worker_not_stale(health)

            if updated is not None and updated.status.is_terminal():
                try:
                    from .flow_telemetry_hooks import housekeep_on_run_terminal

                    housekeep_on_run_terminal(repo_root, updated.id)
                except Exception:
                    pass

            return (updated or record), bool(updated), False
    except FileLockBusy:
        return record, False, True
    except Exception as exc:
        (logger or _logger).warning("Failed to reconcile flow %s: %s", record.id, exc)
        return record, False, False


def reconcile_flow_runs(
    repo_root: Path,
    *,
    flow_type: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> FlowReconcileResult:
    db_path = resolve_repo_flows_db_path(repo_root)
    if not db_path.exists():
        return FlowReconcileResult(records=[], summary=FlowReconcileSummary())
    from ..config import ConfigError, load_repo_config

    try:
        config = load_repo_config(repo_root)
        durable_writes = config.durable_writes
    except ConfigError:
        durable_writes = False
    store = FlowStore(db_path, durable=durable_writes)
    summary = FlowReconcileSummary()
    records: list[FlowRunRecord] = []
    try:
        store.initialize()
        skip_sig = _reconcile_skip_signature(store)
        if _should_skip_reconcile(db_path, skip_sig):
            active_count = store.count_active_flow_runs(flow_type=flow_type)
            if active_count == 0:
                return FlowReconcileResult(
                    records=records, summary=FlowReconcileSummary()
                )
        for record in store.list_flow_runs(flow_type=flow_type):
            if record.status in _ACTIVE_STATUSES:
                summary.active += 1
                summary.checked += 1
                record, updated, locked = reconcile_flow_run(
                    repo_root, record, store, logger=logger
                )
                if updated:
                    summary.updated += 1
                if locked:
                    summary.locked += 1
            records.append(record)
        _record_reconcile_mtime(db_path, _reconcile_skip_signature(store))
    except (
        sqlite3.Error,
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:  # intentional: top-level reconcile loop must not raise
        summary.errors += 1
        (logger or _logger).warning("Flow reconcile run failed: %s", exc)
    finally:
        try:
            store.close()
        except (sqlite3.Error, ValueError, TypeError) as exc:
            _logger.debug("Failed to close store: %s", exc)
    return FlowReconcileResult(records=records, summary=summary)
