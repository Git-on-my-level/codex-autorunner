from __future__ import annotations

import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, cast

from .....core.flows import FlowStore
from .....core.flows.models import FlowRunRecord, FlowRunStatus
from .....core.orchestration import (
    OrchestrationFlowService,
    build_ticket_flow_orchestration_service,
)
from .....core.orchestration.models import FlowRunTarget
from .....flows.controller_provider import (
    recover_flow_store,
)

if TYPE_CHECKING:
    from . import FlowRoutesState

_logger = logging.getLogger(__name__)

_WorkerHandle = tuple[Optional[subprocess.Popen[Any]], Any, Any]


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
        except sqlite3.Error:
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
    flow_target_id: Optional[str] = None,
    reconcile: bool,
    store: Optional[FlowStore],
    safe_list_flow_runs: Callable[..., list[FlowRunRecord]],
    build_flow_orchestration_service_fn: Callable[
        [Path, str], OrchestrationFlowService
    ] = build_flow_orchestration_service,
) -> list[FlowRunRecord]:
    listing_flow_type = flow_type or flow_target_id
    try:
        records = list_orchestration_flow_run_records(
            repo_root,
            flow_type=listing_flow_type or "ticket_flow",
            flow_target_id=flow_target_id or flow_type,
            store=store,
            build_service=build_flow_orchestration_service_fn,
        )
    except (
        Exception
    ):  # intentional: orchestration layer errors are heterogeneous; fallback to empty
        records = []

    if not records:
        if store:
            records = store.list_flow_runs(flow_type=listing_flow_type)
        else:
            records = safe_list_flow_runs(
                repo_root, flow_type=listing_flow_type, recover_stuck=reconcile
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
            repo_root, flow_type=listing_flow_type, recover_stuck=reconcile
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


def evict_cached_controller(
    repo_root: Path, flow_type: str, state: FlowRoutesState
) -> None:
    key = (repo_root.resolve(), flow_type)
    with state.lock:
        controller = cast(Optional[object], state.controller_cache.pop(key, None))
    if not controller:
        return
    try:
        cast(Any, controller).shutdown()
    except (
        Exception
    ):  # intentional: cached controller type is unknown; shutdown must not propagate
        _logger.debug("Failed to shutdown cached flow controller", exc_info=True)


def cleanup_worker_handle(run_id: str, state: FlowRoutesState) -> None:
    with state.lock:
        handle = cast(Optional[_WorkerHandle], state.active_workers.pop(run_id, None))
    if not handle:
        return

    proc, stdout, stderr = handle
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    for stream in (stdout, stderr):
        if stream and not stream.closed:
            try:
                stream.flush()
            except OSError:
                pass
            try:
                stream.close()
            except OSError:
                pass


def recover_flow_store_if_possible(
    repo_root: Path,
    flow_type: str,
    state: FlowRoutesState,
    exc: Exception,
) -> bool:
    """Recover a corrupt flow store for this repo, dropping the cached controller.

    Thin surface-side adapter: the decision to rotate and re-initialize a user's
    durable database lives in ``flows.controller_provider.recover_flow_store``.
    """
    db_path, _ = flow_paths(repo_root)
    return recover_flow_store(
        db_path,
        exc,
        on_evict=lambda: evict_cached_controller(repo_root, flow_type, state),
    )
