from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional

from ...core.config import find_nearest_hub_config_path, load_repo_config
from ...core.flows import FlowController
from ...core.flows.models import FlowRunRecord
from ...core.flows.store import FlowStore
from ...core.flows.ux_helpers import ensure_worker
from ...core.flows.worker_process import spawn_flow_worker
from ...core.runtime import RuntimeContext
from ...integrations.agents import build_backend_orchestrator
from ...integrations.agents.build_agent_pool import build_agent_pool
from .definition import build_ticket_flow_definition


def build_ticket_flow_runtime_resources(repo_root: Path) -> SimpleNamespace:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"

    config = load_repo_config(repo_root)
    backend_orchestrator = build_backend_orchestrator(repo_root, config)
    engine = RuntimeContext(
        repo_root,
        config=config,
        backend_orchestrator=backend_orchestrator,
    )
    agent_pool = build_agent_pool(engine.config)
    definition = build_ticket_flow_definition(agent_pool=agent_pool)
    definition.validate()
    controller: FlowController = FlowController(
        definition=definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
        durable=config.durable_writes,
    )
    controller.initialize()
    return SimpleNamespace(controller=controller, agent_pool=agent_pool)


def build_ticket_flow_controller(repo_root: Path) -> FlowController:
    resources = build_ticket_flow_runtime_resources(repo_root)
    controller: FlowController = resources.controller
    return controller


@asynccontextmanager
async def ticket_flow_runtime_session(
    repo_root: Path,
) -> AsyncIterator[SimpleNamespace]:
    resources = build_ticket_flow_runtime_resources(repo_root)
    try:
        yield resources
    finally:
        resources.controller.shutdown()
        close_all = getattr(resources.agent_pool, "close_all", None)
        if callable(close_all):
            await close_all()


async def start_ticket_flow_run(
    repo_root: Path,
    *,
    input_data: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    run_id: Optional[str] = None,
) -> FlowRunRecord:
    async with ticket_flow_runtime_session(repo_root) as resources:
        record = await resources.controller.start_flow(
            input_data=input_data or {},
            run_id=run_id,
            metadata=metadata,
        )
    ensure_worker(repo_root, record.id, is_terminal=False)
    return record


async def resume_ticket_flow_run(
    repo_root: Path,
    run_id: str,
    *,
    force: bool = False,
) -> FlowRunRecord:
    async with ticket_flow_runtime_session(repo_root) as resources:
        record = await resources.controller.resume_flow(run_id, force=force)
    ensure_worker(repo_root, record.id, is_terminal=False)
    return record


async def stop_ticket_flow_run(repo_root: Path, run_id: str) -> FlowRunRecord:
    async with ticket_flow_runtime_session(repo_root) as resources:
        return await resources.controller.stop_flow(run_id)


def _open_ticket_flow_store(repo_root: Path) -> FlowStore:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    durable = False
    if find_nearest_hub_config_path(repo_root) is not None:
        config = load_repo_config(repo_root)
        durable = config.durable_writes
    store = FlowStore(db_path, durable=durable)
    store.initialize()
    return store


def get_ticket_flow_run_status(repo_root: Path, run_id: str) -> Optional[FlowRunRecord]:
    store = _open_ticket_flow_store(repo_root)
    try:
        return store.get_flow_run(run_id)
    finally:
        store.close()


def list_ticket_flow_runs(repo_root: Path) -> list[FlowRunRecord]:
    store = _open_ticket_flow_store(repo_root)
    try:
        return store.list_flow_runs(flow_type="ticket_flow")
    finally:
        store.close()


def list_active_ticket_flow_runs(repo_root: Path) -> list[FlowRunRecord]:
    return [
        record
        for record in list_ticket_flow_runs(repo_root)
        if record.status.is_active() or record.status.is_paused()
    ]


def spawn_ticket_flow_worker(
    repo_root: Path, run_id: str, logger: logging.Logger
) -> None:
    try:
        proc, out, err = spawn_flow_worker(repo_root, run_id)
        out.close()
        err.close()
        logger.info("Started ticket_flow worker for %s (pid=%s)", run_id, proc.pid)
    except Exception as exc:
        logger.warning(
            "ticket_flow.worker.spawn_failed",
            exc_info=exc,
            extra={"run_id": run_id},
        )
