from __future__ import annotations

import logging
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Optional

from ...tickets.outbox import archive_dispatch, ensure_outbox_dirs, resolve_outbox_paths
from ...tickets.replies import resolve_reply_paths
from ..config import ConfigError, load_repo_config
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
from .lifecycle_reducer import (
    NO_CHANGE,
    EffectKind,
    reduce_flow_lifecycle,
)
from .models import FlowEventType, FlowRunRecord, FlowRunStatus, parse_flow_timestamp
from .store import UNSET, FlowStore, now_iso
from .supervisor import (
    CommitBarrierObservation,
    RestartPolicyObservation,
    SupervisorEffectKind,
    supervise_reconcile_flow,
    worker_observation_from_health,
)
from .worker_process import (
    FlowWorkerHealth,
    check_worker_health,
    clear_worker_metadata,
    read_worker_crash_info,
    spawn_flow_worker,
    write_worker_crash_info,
)
from .workspace_root import resolve_ticket_flow_workspace_root

_logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = (
    FlowRunStatus.PENDING,
    FlowRunStatus.RUNNING,
    FlowRunStatus.STOPPING,
    FlowRunStatus.PAUSED,
)
_DEFAULT_STALE_ALIVE_THRESHOLD_SECONDS = 30 * 60

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
    if health.status in {"dead", "mismatch", "invalid", "stale_alive"}:
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


def _git_status_porcelain(repo_root: Path) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "").strip()


def _ticket_marked_done(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end < 0:
        return False
    for raw_line in text[3:end].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() == "done":
            return value.strip().strip("\"'").lower() == "true"
    return False


def _commit_barrier_observation(
    repo_root: Path, record: FlowRunRecord
) -> CommitBarrierObservation:
    state = record.state if isinstance(record.state, dict) else {}
    engine = state.get("ticket_engine") if isinstance(state, dict) else {}
    engine = engine if isinstance(engine, dict) else {}
    current_ticket = engine.get("current_ticket")
    if not isinstance(current_ticket, str) or not current_ticket.strip():
        return CommitBarrierObservation()

    commit = engine.get("commit")
    commit = commit if isinstance(commit, dict) else {}
    ticket_path = repo_root / current_ticket
    current_ticket_done = ticket_path.exists() and _ticket_marked_done(ticket_path)
    dirty_status = _git_status_porcelain(repo_root) if current_ticket_done else None
    return CommitBarrierObservation(
        current_ticket=current_ticket,
        current_ticket_done=current_ticket_done,
        worktree_dirty=bool(dirty_status),
        commit_pending=bool(commit.get("pending")),
    )


def _restart_state(record: FlowRunRecord) -> dict[str, Any]:
    state = record.state if isinstance(record.state, dict) else {}
    recovery = state.get("recovery") if isinstance(state, dict) else {}
    recovery = recovery if isinstance(recovery, dict) else {}
    restart = recovery.get("restart") if isinstance(recovery, dict) else {}
    return dict(restart) if isinstance(restart, dict) else {}


def _restart_attempt_count(record: FlowRunRecord) -> int:
    raw = _restart_state(record).get("count")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0


def _load_restart_config(repo_root: Path) -> tuple[bool, int, float]:
    try:
        ticket_flow = load_repo_config(repo_root).ticket_flow
    except ConfigError:
        return True, 2, 0.0
    return (
        bool(getattr(ticket_flow, "restart_recoverable_failures", True)),
        max(0, int(getattr(ticket_flow, "restart_max_attempts", 2))),
        max(0.0, float(getattr(ticket_flow, "restart_backoff_seconds", 0.0))),
    )


def _load_stale_alive_threshold_seconds(repo_root: Path) -> int:
    try:
        ticket_flow = load_repo_config(repo_root).ticket_flow
    except ConfigError:
        return _DEFAULT_STALE_ALIVE_THRESHOLD_SECONDS
    try:
        raw_value: Any = getattr(ticket_flow, "stale_alive_threshold_seconds", None)
        value = int(raw_value)
    except (TypeError, ValueError):
        return _DEFAULT_STALE_ALIVE_THRESHOLD_SECONDS
    return value if value > 0 else _DEFAULT_STALE_ALIVE_THRESHOLD_SECONDS


def _latest_semantic_progress_at(
    record: FlowRunRecord, store: FlowStore
) -> Optional[str]:
    candidates: list[str] = []
    try:
        _seq, last_event_at = store.get_last_event_meta(record.id)
    except (sqlite3.Error, ValueError, TypeError, RuntimeError, AttributeError):
        last_event_at = None
    for candidate in (
        last_event_at,
        record.started_at,
        record.created_at,
        record.finished_at,
    ):
        if isinstance(candidate, str) and candidate.strip():
            candidates.append(candidate.strip())
    parsed = [parse_flow_timestamp(candidate) for candidate in candidates]
    normalized = [dt.astimezone(timezone.utc) for dt in parsed if dt is not None]
    if not normalized:
        return None
    return max(normalized).isoformat()


def _annotate_stale_alive_health(
    repo_root: Path,
    record: FlowRunRecord,
    store: FlowStore,
    health: Any,
    *,
    now: str,
) -> Any:
    if (
        record.flow_type != "ticket_flow"
        or record.status != FlowRunStatus.RUNNING
        or getattr(health, "status", None) != "alive"
        or getattr(health, "active_tool", None) is not None
    ):
        return health
    last_progress_at = _latest_semantic_progress_at(record, store)
    last_progress_dt = parse_flow_timestamp(last_progress_at)
    now_dt = parse_flow_timestamp(now)
    if last_progress_dt is None or now_dt is None:
        return health
    age_seconds = int(max(0.0, (now_dt - last_progress_dt).total_seconds()))
    threshold_seconds = _load_stale_alive_threshold_seconds(repo_root)
    if age_seconds <= threshold_seconds:
        return health

    health.status = "stale_alive"
    health.message = "worker alive but no active tool and semantic progress is stale"
    health.last_semantic_progress_at = last_progress_at
    health.last_tool_activity_at = None
    health.current_phase = record.current_step
    health.stale_reason = "semantic_progress_stale_without_active_tool"
    health.stale_threshold_seconds = threshold_seconds
    health.semantic_stale_age_seconds = age_seconds
    return health


def _restart_backoff_ready(record: FlowRunRecord, backoff_seconds: float) -> bool:
    if backoff_seconds <= 0:
        return True
    last_attempted_at = _restart_state(record).get("last_attempted_at")
    if not isinstance(last_attempted_at, str) or not last_attempted_at.strip():
        return True
    last_dt = parse_flow_timestamp(last_attempted_at)
    now_dt = parse_flow_timestamp(now_iso())
    if last_dt is None or now_dt is None:
        return True
    return (now_dt - last_dt).total_seconds() >= backoff_seconds


def _restart_policy_observation(
    repo_root: Path, record: FlowRunRecord, health: Optional[Any] = None
) -> RestartPolicyObservation:
    enabled, max_attempts, backoff_seconds = _load_restart_config(repo_root)
    recoverable_shutdown = worker_observation_from_health(
        health
    ).exit.recoverable_shutdown
    if (
        record.flow_type != "ticket_flow"
        or (record.stop_requested and not recoverable_shutdown)
        or (
            record.status != FlowRunStatus.RUNNING
            and not (record.status == FlowRunStatus.STOPPING and recoverable_shutdown)
        )
    ):
        enabled = False
    return RestartPolicyObservation(
        enabled=enabled,
        attempts=_restart_attempt_count(record),
        max_attempts=max_attempts,
        backoff_ready=_restart_backoff_ready(record, backoff_seconds),
    )


def _with_restart_attempt(
    state: dict[str, Any],
    *,
    max_attempts: int,
    reason: str,
    failure_reason: Optional[str] = None,
    health: Optional[Any] = None,
) -> dict[str, Any]:
    updated = dict(state)
    recovery = updated.get("recovery")
    recovery = dict(recovery) if isinstance(recovery, dict) else {}
    restart = recovery.get("restart")
    restart = dict(restart) if isinstance(restart, dict) else {}
    count = restart.get("count")
    count = count if isinstance(count, int) and not isinstance(count, bool) else 0
    count += 1
    restart.update(
        {
            "count": count,
            "max_attempts": max_attempts,
            "last_attempted_at": now_iso(),
            "last_failure_reason": failure_reason,
            "last_reason": reason,
            "exhausted": count >= max_attempts if max_attempts > 0 else False,
        }
    )
    recovery["restart"] = restart
    if health is not None and getattr(health, "status", None) == "stale_alive":
        recovery["stale_alive"] = _stale_alive_recovery_payload(health)
    updated["recovery"] = recovery
    return updated


def _with_restart_exhausted(
    state: dict[str, Any],
    *,
    max_attempts: int,
    reason: str,
    health: Optional[Any] = None,
) -> dict[str, Any]:
    updated = dict(state)
    recovery = updated.get("recovery")
    recovery = dict(recovery) if isinstance(recovery, dict) else {}
    restart = recovery.get("restart")
    restart = dict(restart) if isinstance(restart, dict) else {}
    restart.setdefault("count", 0)
    restart["max_attempts"] = max_attempts
    restart["last_failure_reason"] = reason
    restart["exhausted"] = True
    recovery["restart"] = restart
    if health is not None and getattr(health, "status", None) == "stale_alive":
        recovery["stale_alive"] = _stale_alive_recovery_payload(health)
    updated["recovery"] = recovery
    return updated


def _stale_alive_recovery_payload(health: Any) -> dict[str, Any]:
    return {
        "reason": getattr(health, "stale_reason", None),
        "last_semantic_progress_at": getattr(health, "last_semantic_progress_at", None),
        "last_tool_activity_at": getattr(health, "last_tool_activity_at", None),
        "current_phase": getattr(health, "current_phase", None),
        "stale_threshold_seconds": getattr(health, "stale_threshold_seconds", None),
        "semantic_stale_age_seconds": getattr(
            health, "semantic_stale_age_seconds", None
        ),
        "worker_pid": getattr(health, "pid", None),
    }


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
        health.status in {"dead", "stale_alive"} and _is_stale_crash_info(crash_info)
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
            exception=(
                getattr(health, "stale_reason", None)
                if health.status == "stale_alive"
                else record.error_message
            ),
            exit_origin=getattr(health, "exit_origin", None),
            exit_kind=getattr(health, "exit_kind", None),
            reap_reason=getattr(health, "reap_reason", None),
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
    stale_alive = (
        isinstance(crash_info, dict)
        and crash_info.get("exception") == "semantic_progress_stale_without_active_tool"
    )
    lines = [
        (
            "The ticket worker appears stale while still alive: no active child tool "
            "was detected and semantic progress was stale."
            if stale_alive
            else "The ticket worker stopped unexpectedly and no actionable dispatch was available."
        ),
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
            "Diagnostic artifact:" if stale_alive else "Crash artifact:",
            f"- `.codex-autorunner/flows/{record.id}/crash.json`",
            "",
            (
                "Please inspect the stale-alive diagnostic and decide whether to "
                "resume or intentionally start a replacement run."
                if stale_alive
                else "Please inspect the crash artifact and decide whether to resume or restart the run."
            ),
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

    stale_alive = (
        isinstance(crash_info, dict)
        and crash_info.get("exception") == "semantic_progress_stale_without_active_tool"
    )
    title = "Worker stale-alive stall" if stale_alive else "Worker crashed"
    dispatch_frontmatter = f"---\nmode: pause\ntitle: {title}\n---\n\n"
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
            now = now_iso()
            health = _annotate_stale_alive_health(
                repo_root, record, store, health, now=now
            )
            crash_info = None
            pending_stop_requested = (
                record.status == FlowRunStatus.PENDING and record.stop_requested
            )
            if (
                health.status in {"dead", "invalid", "mismatch", "stale_alive"}
                and not pending_stop_requested
            ):
                crash_info = _ensure_crash_payload(repo_root, record, store, health)

            commit_barrier = _commit_barrier_observation(repo_root, record)
            restart_policy = _restart_policy_observation(repo_root, record, health)
            decision = supervise_reconcile_flow(
                record,
                health,
                commit_barrier=commit_barrier,
                restart=restart_policy,
            )
            trigger = decision.first_lifecycle_trigger()

            wants_restart = any(
                effect.kind == SupervisorEffectKind.SPAWN_WORKER
                for effect in decision.effects
            )
            restart_enabled_waiting_for_backoff = (
                restart_policy.enabled
                and not restart_policy.backoff_ready
                and health.status in {"dead", "invalid", "mismatch", "stale_alive"}
                and trigger is not None
            )

            if restart_enabled_waiting_for_backoff:
                emit_reconcile_noop(
                    store=store,
                    run_id=record.id,
                    status=record.status,
                    note="restart-backoff-wait",
                    worker_status=health.status,
                )
                return record, False, False

            restart_state_override: Optional[dict[str, Any]] = None

            if wants_restart:
                restart_state = _with_restart_attempt(
                    dict(record.state or {}),
                    max_attempts=restart_policy.max_attempts,
                    reason=decision.note,
                    health=health,
                )
                try:
                    clear_worker_metadata(health.artifact_path.parent)
                    proc, stdout_handle, stderr_handle = spawn_flow_worker(
                        repo_root, record.id
                    )
                    for stream in (stdout_handle, stderr_handle):
                        try:
                            stream.close()
                        except OSError:
                            pass
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    subprocess.SubprocessError,
                ) as exc:
                    restart_state_override = _with_restart_attempt(
                        dict(record.state or {}),
                        max_attempts=restart_policy.max_attempts,
                        reason=decision.note,
                        failure_reason=f"spawn_failed: {exc}",
                        health=health,
                    )
                else:
                    store.set_stop_requested(record.id, False)
                    updated = store.update_flow_run_status(
                        run_id=record.id,
                        status=FlowRunStatus.RUNNING,
                        state=restart_state,
                        error_message=None,
                        finished_at=UNSET,
                    )
                    emit_recovery_takeover(
                        store=store,
                        run_id=record.id,
                        previous_status=record.status,
                        resulting_status=FlowRunStatus.RUNNING,
                        note="restart-worker-spawned",
                        worker_status=health.status,
                        crash_info=crash_info,
                    )
                    (logger or _logger).info(
                        "Restarted flow worker for %s after %s (pid=%s)",
                        record.id,
                        decision.note,
                        getattr(proc, "pid", None),
                    )
                    return (updated or record), bool(updated), False

            if trigger is None:
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
                        note="reconcile-noop",
                        worker_status=health.status,
                    )
                return record, False, False

            result = reduce_flow_lifecycle(
                record.status,
                record.state or {},
                trigger,
                now=now,
                current_step=record.current_step,
            )

            is_recovery = health.status in {
                "dead",
                "invalid",
                "mismatch",
                "stale_alive",
            }

            (logger or _logger).info(
                "Reconciling flow %s: %s -> %s (%s)",
                record.id,
                record.status.value,
                result.status.value,
                result.note or "reconcile",
            )

            state = (
                result.state
                if result.state is not NO_CHANGE
                else dict(record.state or {})
            )
            if restart_state_override is not None:
                state = restart_state_override
            elif restart_policy.exhausted:
                state = _with_restart_exhausted(
                    state,
                    max_attempts=restart_policy.max_attempts,
                    reason="restart-attempts-exhausted",
                    health=health,
                )
            for effect in result.effects:
                if effect.kind == EffectKind.ENRICH_FAILURE_PAYLOAD:
                    reconcile_ctx = ReconcileContext(
                        worker_exit_code=getattr(health, "exit_code", None),
                        worker_stderr_tail=getattr(health, "stderr_tail", None),
                        crash_info=crash_info,
                    )
                    state = ensure_failure_payload(
                        state,
                        record=record,
                        step_id=effect.step_id,
                        error_message=effect.error_message,
                        store=store,
                        note=effect.note,
                        failed_at=(
                            result.finished_at
                            if result.finished_at is not NO_CHANGE
                            else now
                        ),
                        reconcile_context=reconcile_ctx,
                    )

            update_kwargs: dict[str, Any] = {
                "run_id": record.id,
                "status": result.status,
                "state": state,
            }
            if result.current_step is not NO_CHANGE:
                update_kwargs["current_step"] = result.current_step
            if result.error_message is not NO_CHANGE:
                update_kwargs["error_message"] = result.error_message
            if result.finished_at is not NO_CHANGE:
                update_kwargs["finished_at"] = result.finished_at
            else:
                update_kwargs["finished_at"] = UNSET
            updated = store.update_flow_run_status(**update_kwargs)

            if is_recovery:
                emit_recovery_takeover(
                    store=store,
                    run_id=record.id,
                    previous_status=record.status,
                    resulting_status=result.status,
                    note=result.note or "reconcile-recovery",
                    worker_status=health.status,
                    crash_info=crash_info,
                    error_message=(
                        result.error_message
                        if result.error_message is not NO_CHANGE
                        else None
                    ),
                )
            else:
                emit_reconcile_transition(
                    store=store,
                    run_id=record.id,
                    previous_status=record.status,
                    resulting_status=result.status,
                    note=result.note or "reconcile",
                    worker_status=health.status,
                    error_message=(
                        result.error_message
                        if result.error_message is not NO_CHANGE
                        else None
                    ),
                )

            if result.status == FlowRunStatus.FAILED and isinstance(state, dict):
                failure = state.get("failure")
                reason_code = (
                    failure.get(CANONICAL_FAILURE_REASON_CODE_FIELD)
                    if isinstance(failure, dict)
                    else None
                )
                emit_failure_projection(
                    store=store,
                    run_id=record.id,
                    status=result.status,
                    failure_reason_code=reason_code,
                    step_id=record.current_step,
                    error_message=(
                        result.error_message
                        if result.error_message is not NO_CHANGE
                        else None
                    ),
                    origin="reconciler",
                )

            if result.status == FlowRunStatus.FAILED and (
                result.error_message is not NO_CHANGE and result.error_message
            ):
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
                    error_message=(
                        result.error_message
                        if result.error_message is not NO_CHANGE
                        else None
                    ),
                    note=result.note,
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

            if (
                record.status == FlowRunStatus.PAUSED
                or (
                    health.status == "stale_alive"
                    and result.status == FlowRunStatus.FAILED
                )
            ) and health.status in {
                "dead",
                "invalid",
                "mismatch",
                "stale_alive",
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
                    from .flow_telemetry_hooks import (
                        build_store_event_emitter,
                        handle_run_terminal_side_effects,
                    )

                    handle_run_terminal_side_effects(
                        repo_root,
                        updated,
                        emit_event=build_store_event_emitter(store, updated.id),
                    )
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
