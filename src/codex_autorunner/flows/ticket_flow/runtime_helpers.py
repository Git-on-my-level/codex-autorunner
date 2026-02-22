from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from ...core.config import load_repo_config
from ...core.flows import FlowController
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
