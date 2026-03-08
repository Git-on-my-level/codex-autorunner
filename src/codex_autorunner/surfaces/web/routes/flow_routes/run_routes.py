from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from . import FlowRoutesState

_logger = logging.getLogger(__name__)


def start_flow_worker(
    repo_root: Path,
    flow_type: str,
    state: FlowRoutesState,
) -> tuple[Optional[subprocess.Popen], Optional[str], Optional[threading.Thread]]:
    import uuid

    from ....core.flows.worker_process import FlowWorkerHealth, check_worker_health

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    worker_cmd = [
        "python3",
        "-m",
        "codex_autorunner.flows.run",
        "--repo-root",
        str(repo_root),
        "--flow-type",
        flow_type,
        "--run-id",
        run_id,
    ]

    proc = subprocess.Popen(
        worker_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(repo_root),
    )

    def _monitor():
        health = check_worker_health(proc, FlowWorkerHealth.TERMINATED)
        if health == FlowWorkerHealth.UNHEALTHY:
            try:
                proc.terminate()
            except Exception:
                pass

    monitor_thread = threading.Thread(target=_monitor, daemon=True)
    monitor_thread.start()

    return proc, run_id, monitor_thread


def stop_flow_worker(
    repo_root: Path,
    flow_type: str,
    state: FlowRoutesState,
) -> dict[str, Any]:
    key = (repo_root.resolve(), flow_type)
    with state.lock:
        worker_handle = state.active_workers.pop(key, None)

    if worker_handle is None:
        return {"status": "ok", "action": "no_worker"}

    proc, run_id, monitor_thread = worker_handle
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass

    return {"status": "ok", "action": "stopped", "run_id": run_id}


def resume_flow_run(
    repo_root: Path,
    run_id: str,
    flow_type: str,
    state: FlowRoutesState,
) -> dict[str, Any]:
    from .definitions import get_flow_controller

    controller = get_flow_controller(repo_root, flow_type, state)
    controller.resume(run_id)
    return {"status": "ok", "run_id": run_id, "action": "resumed"}


def reconcile_flow_run(
    repo_root: Path,
    run_id: str,
    flow_type: str,
    state: FlowRoutesState,
) -> dict[str, Any]:
    from ....core.flows.reconciler import reconcile_flow_run as perform_reconcile

    perform_reconcile(repo_root, run_id)
    return {"status": "ok", "run_id": run_id, "action": "reconciled"}


def archive_flow_run(
    repo_root: Path,
    run_id: str,
    flow_type: str,
    state: FlowRoutesState,
) -> dict[str, Any]:
    from .definitions import get_flow_controller

    controller = get_flow_controller(repo_root, flow_type, state)
    controller.archive(run_id)
    return {"status": "ok", "run_id": run_id, "action": "archived"}


def get_flow_run_status(
    repo_root: Path,
    run_id: str,
    flow_type: str,
    state: FlowRoutesState,
) -> dict[str, Any]:
    from .definitions import get_flow_controller

    controller = get_flow_controller(repo_root, flow_type, state)
    status = controller.get_status(run_id)
    return {"status": "ok", "run_id": run_id, "flow_status": status}


def stream_flow_events(
    repo_root: Path,
    run_id: str,
    flow_type: str,
    state: FlowRoutesState,
):
    from ....integrations.app_server.event_buffer import format_sse
    from ...services import flow_store as flow_store_service

    store = flow_store_service.require_flow_store(repo_root, logger=_logger)
    if store is None:
        return

    events = store.get_events(run_id)
    for event in events:
        yield format_sse("flow_event", event)
    yield format_sse("done", {"status": "ok"})


_RUN_ROUTE_API = (
    stop_flow_worker,
    resume_flow_run,
    archive_flow_run,
    get_flow_run_status,
)
