from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from .....core.flows import FlowStore
from .....core.flows.models import FlowRunRecord, FlowRunStatus
from .....core.orchestration import (
    OrchestrationFlowService,
    build_ticket_flow_orchestration_service,
)
from .....core.orchestration.models import FlowRunTarget

if TYPE_CHECKING:
    from . import FlowRoutesState

_logger = logging.getLogger(__name__)

_FLOW_DB_CORRUPT_SUFFIX = ".corrupt"
_FLOW_DB_NOTICE_SUFFIX = ".corrupt.json"


def flow_paths(repo_root: Path) -> tuple[Path, Path]:
    from ...services import flow_store as flow_store_service

    return flow_store_service.flow_paths(repo_root)


def build_flow_orchestration_service(
    repo_root: Path, flow_type: str
) -> OrchestrationFlowService:
    if flow_type != "ticket_flow":
        raise KeyError(f"Unknown flow type: {flow_type}")
    return build_ticket_flow_orchestration_service(workspace_root=repo_root)


def flow_run_record_from_target(target: FlowRunTarget) -> FlowRunRecord:
    return FlowRunRecord(
        id=target.run_id,
        flow_type=target.flow_type,
        status=FlowRunStatus(target.status),
        input_data={},
        state=dict(target.state or {}),
        current_step=target.current_step,
        stop_requested=False,
        created_at=target.created_at or "",
        started_at=target.started_at,
        finished_at=target.finished_at,
        error_message=target.error_message,
        metadata=dict(target.metadata or {}),
    )


def resolve_flow_run_record(
    repo_root: Path,
    target: FlowRunTarget,
    *,
    store: Optional[FlowStore] = None,
) -> FlowRunRecord:
    record: Optional[FlowRunRecord] = None
    if store is not None:
        try:
            record = store.get_flow_run(target.run_id)
        except Exception:
            record = None
    if record is not None:
        return record
    return flow_run_record_from_target(target)


def list_orchestration_flow_run_records(
    repo_root: Path,
    *,
    flow_type: str,
    flow_target_id: Optional[str] = None,
    store: Optional[FlowStore] = None,
    active_only: bool = False,
    build_service: Callable[
        [Path, str], OrchestrationFlowService
    ] = build_flow_orchestration_service,
) -> list[FlowRunRecord]:
    service = build_service(repo_root, flow_type)
    if active_only:
        targets = service.list_active_flow_runs(flow_target_id=flow_target_id)
    else:
        targets = service.list_flow_runs(flow_target_id=flow_target_id)
    return [
        resolve_flow_run_record(repo_root, target, store=store) for target in targets
    ]


def get_orchestration_flow_run_record(
    repo_root: Path,
    run_id: str,
    *,
    flow_type: str = "ticket_flow",
    store: Optional[FlowStore] = None,
    build_service: Callable[
        [Path, str], OrchestrationFlowService
    ] = build_flow_orchestration_service,
) -> Optional[FlowRunRecord]:
    service = build_service(repo_root, flow_type)
    target = service.get_flow_run(run_id)
    if target is None:
        return None
    return resolve_flow_run_record(repo_root, target, store=store)


def load_flow_run_records(
    repo_root: Path,
    *,
    flow_type: Optional[str],
    reconcile: bool,
    store: Optional[FlowStore],
    safe_list_flow_runs: Callable[..., list[FlowRunRecord]],
    build_flow_orchestration_service_fn: Callable[
        [Path, str], OrchestrationFlowService
    ] = build_flow_orchestration_service,
) -> list[FlowRunRecord]:
    try:
        records = list_orchestration_flow_run_records(
            repo_root,
            flow_type=flow_type or "ticket_flow",
            flow_target_id=flow_type if flow_type else None,
            store=store,
            build_service=build_flow_orchestration_service_fn,
        )
    except Exception:
        records = []

    if not records:
        if store:
            records = store.list_flow_runs(flow_type=flow_type)
        else:
            records = safe_list_flow_runs(
                repo_root, flow_type=flow_type, recover_stuck=reconcile
            )

    if reconcile and store:
        from .....core.flows import reconciler as flow_reconciler

        return [
            flow_reconciler.reconcile_flow_run(
                repo_root, record, store, logger=_logger
            )[0]
            for record in records
        ]
    if reconcile and not records:
        return safe_list_flow_runs(
            repo_root, flow_type=flow_type, recover_stuck=reconcile
        )
    return records


def load_flow_run_record(
    repo_root: Path,
    run_id: str,
    *,
    reconcile: bool,
    store: Optional[FlowStore],
    flow_type: str = "ticket_flow",
    build_flow_orchestration_service_fn: Callable[
        [Path, str], OrchestrationFlowService
    ] = build_flow_orchestration_service,
) -> Optional[FlowRunRecord]:
    record = get_orchestration_flow_run_record(
        repo_root,
        run_id,
        flow_type=flow_type,
        store=store,
        build_service=build_flow_orchestration_service_fn,
    )
    if record is None:
        return None
    if reconcile and store:
        from .....core.flows import reconciler as flow_reconciler

        return flow_reconciler.reconcile_flow_run(
            repo_root, record, store, logger=_logger
        )[0]
    return record


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def is_probably_corrupt_flow_db_error(exc: Exception, db_path: Path) -> bool:
    if not isinstance(exc, sqlite3.Error):
        return False
    msg = str(exc).lower()
    if "file is not a database" in msg or "database disk image is malformed" in msg:
        return True
    if "disk i/o error" in msg:
        try:
            header = db_path.read_bytes()[:16]
        except Exception:
            return False
        return header not in (b"", b"SQLite format 3\x00")
    return False


def rotate_corrupt_flow_db(db_path: Path, detail: str) -> Optional[Path]:
    from .....core.utils import atomic_write

    stamp = utc_stamp()
    backup_path = db_path.with_name(f"{db_path.name}{_FLOW_DB_CORRUPT_SUFFIX}.{stamp}")
    notice_path = db_path.with_name(f"{db_path.name}{_FLOW_DB_NOTICE_SUFFIX}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup_value: str = ""
    if db_path.exists():
        try:
            db_path.replace(backup_path)
            backup_value = str(backup_path)
        except Exception:
            backup_value = ""

    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        try:
            sidecar.unlink(missing_ok=True)
        except Exception:
            pass

    notice = {
        "status": "corrupt",
        "message": "Flow store reset due to corrupted flows.db.",
        "detail": detail,
        "detected_at": stamp,
        "backup_path": backup_value,
    }
    try:
        atomic_write(notice_path, json.dumps(notice, indent=2) + "\n")
    except Exception:
        _logger.warning("Failed to write flow DB corruption notice at %s", notice_path)
    return backup_path if backup_value else None


def evict_cached_controller(
    repo_root: Path, flow_type: str, state: FlowRoutesState
) -> None:
    key = (repo_root.resolve(), flow_type)
    with state.lock:
        controller = state.controller_cache.pop(key, None)
    if not controller:
        return
    try:
        controller.shutdown()
    except Exception:
        pass


def cleanup_worker_handle(run_id: str, state: FlowRoutesState) -> None:
    with state.lock:
        handle = state.active_workers.pop(run_id, None)
    if not handle:
        return

    proc, stdout, stderr = handle
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    for stream in (stdout, stderr):
        if stream and not stream.closed:
            try:
                stream.flush()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass


def recover_flow_store_if_possible(
    repo_root: Path,
    flow_type: str,
    state: FlowRoutesState,
    exc: Exception,
) -> bool:
    db_path, _ = flow_paths(repo_root)
    if not is_probably_corrupt_flow_db_error(exc, db_path):
        return False

    backup_path = rotate_corrupt_flow_db(db_path, str(exc))
    evict_cached_controller(repo_root, flow_type, state)
    store = FlowStore(db_path)
    try:
        store.initialize()
        _logger.warning(
            "Recovered corrupted flow DB at %s (backup=%s, reason=%s)",
            db_path,
            str(backup_path) if backup_path else "unavailable",
            exc,
        )
        return True
    except Exception as recover_exc:
        _logger.warning(
            "Flow DB recovery failed at %s after error %s: %s",
            db_path,
            exc,
            recover_exc,
        )
        return False
    finally:
        try:
            store.close()
        except Exception:
            pass
