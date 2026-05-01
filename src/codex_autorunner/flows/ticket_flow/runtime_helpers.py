from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...core.config import find_nearest_hub_config_path, load_repo_config
from ...core.flows import FlowController, archive_flow_run_artifacts
from ...core.flows.models import FlowRunRecord, FlowRunStatus
from ...core.flows.reconciler import reconcile_flow_run
from ...core.flows.store import FlowStore
from ...core.flows.worker_process import (
    check_worker_health,
    clear_worker_metadata,
    spawn_flow_worker,
)
from ...core.flows.workspace_root import (
    normalize_ticket_flow_input_data as _normalize_ticket_flow_input_data,
)
from ...core.orchestration.models import FlowRunTarget
from ...core.runtime import RuntimeContext
from ...core.state_roots import resolve_repo_flows_db_path, resolve_repo_state_root
from ...core.ticket_flow_operator import (
    PreflightCheckResult as TicketFlowInboxPreflight,
)
from ...core.ticket_flow_operator import ensure_flow_worker
from ...core.ticket_flow_operator import (
    ticket_flow_inbox_preflight as _ticket_flow_inbox_preflight,
)
from ...integrations.agents import build_backend_orchestrator
from ...integrations.agents.build_agent_pool import build_agent_pool
from ...tickets import DEFAULT_MAX_TOTAL_TURNS
from ...tickets.files import list_ticket_paths
from ...tickets.frontmatter import generate_ticket_id
from .definition import build_ticket_flow_definition

logger = logging.getLogger(__name__)


@dataclass
class TicketFlowRuntimeResources:
    controller: FlowController
    agent_pool: Any


def build_ticket_flow_runtime_resources(repo_root: Path) -> TicketFlowRuntimeResources:
    repo_root = repo_root.resolve()
    state_root = resolve_repo_state_root(repo_root)
    db_path = resolve_repo_flows_db_path(repo_root)
    artifacts_root = state_root / "flows"

    config = load_repo_config(repo_root)
    backend_orchestrator = build_backend_orchestrator(repo_root, config)
    engine = RuntimeContext(
        repo_root,
        config=config,
        backend_orchestrator=backend_orchestrator,
    )
    agent_pool = build_agent_pool(engine.config)
    definition = build_ticket_flow_definition(
        agent_pool=agent_pool,
        auto_commit_default=config.git_auto_commit,
        include_previous_ticket_context_default=(
            config.ticket_flow.include_previous_ticket_context
        ),
        max_total_turns_default=(
            config.ticket_flow.max_total_turns
            if config.ticket_flow.max_total_turns is not None
            else DEFAULT_MAX_TOTAL_TURNS
        ),
    )
    definition.validate()
    controller: FlowController = FlowController(
        definition=definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
        durable=config.durable_writes,
    )
    controller.initialize()
    return TicketFlowRuntimeResources(controller=controller, agent_pool=agent_pool)


def build_ticket_flow_controller(repo_root: Path) -> FlowController:
    resources = build_ticket_flow_runtime_resources(repo_root)
    controller: FlowController = resources.controller
    return controller


@asynccontextmanager
async def ticket_flow_runtime_session(
    repo_root: Path,
) -> AsyncIterator[TicketFlowRuntimeResources]:
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
    normalized_input = normalize_ticket_flow_input_data(repo_root, input_data)
    async with ticket_flow_runtime_session(repo_root) as resources:
        record = await resources.controller.start_flow(
            input_data=normalized_input,
            run_id=run_id,
            metadata=metadata,
        )
    ensure_worker(repo_root, record.id, is_terminal=False)
    return record


def normalize_ticket_flow_input_data(
    repo_root: Path, input_data: Optional[dict[str, Any]]
) -> dict[str, Any]:
    return _normalize_ticket_flow_input_data(repo_root, input_data)


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
    stop_ticket_flow_worker(repo_root, run_id)
    async with ticket_flow_runtime_session(repo_root) as resources:
        return await resources.controller.stop_flow(run_id)


def _open_ticket_flow_store(repo_root: Path) -> FlowStore:
    repo_root = repo_root.resolve()
    db_path = resolve_repo_flows_db_path(repo_root)
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


def ticket_flow_inbox_preflight(repo_root: Path) -> TicketFlowInboxPreflight:
    return _ticket_flow_inbox_preflight(repo_root)


def spawn_ticket_flow_worker(
    repo_root: Path, run_id: str, logger: logging.Logger
) -> None:
    try:
        proc, out, err = spawn_flow_worker(repo_root, run_id)
        out.close()
        err.close()
        logger.info("Started ticket_flow worker for %s (pid=%s)", run_id, proc.pid)
    except (OSError, RuntimeError) as exc:
        logger.warning(
            "ticket_flow.worker.spawn_failed",
            exc_info=exc,
            extra={"run_id": run_id},
        )


def ensure_ticket_flow_worker(
    repo_root: Path, run_id: str, *, is_terminal: bool = False
) -> None:
    result = ensure_flow_worker(repo_root, run_id, is_terminal=is_terminal)
    for key in ("stdout", "stderr"):
        handle = result.get(key)
        close = getattr(handle, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                logger.debug("Failed to close %s handle", key, exc_info=True)


def ensure_worker(repo_root: Path, run_id: str, *, is_terminal: bool = False) -> None:
    """Backward-compatible alias for orchestration wrappers/tests."""
    ensure_ticket_flow_worker(repo_root, run_id, is_terminal=is_terminal)


def stop_ticket_flow_worker(repo_root: Path, run_id: str) -> None:
    health = check_worker_health(repo_root, run_id)
    if health.status in {"dead", "mismatch", "invalid"}:
        try:
            clear_worker_metadata(health.artifact_path.parent)
        except OSError:
            logger.debug("Failed to clear worker metadata", exc_info=True)
    if not health.pid:
        return
    try:
        if os.name != "nt" and hasattr(os, "killpg"):
            # Workers are spawned as their own process group, so pgid == pid.
            os.killpg(health.pid, signal.SIGTERM)
        else:
            os.kill(health.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass
    except OSError:
        try:
            os.kill(health.pid, signal.SIGTERM)
        except OSError:
            logger.debug("Fallback kill failed for pid %s", health.pid, exc_info=True)


def reconcile_ticket_flow_run(
    repo_root: Path, run_id: str
) -> tuple[FlowRunRecord, bool, bool]:
    store = _open_ticket_flow_store(repo_root)
    try:
        record = store.get_flow_run(run_id)
        if record is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return reconcile_flow_run(repo_root, record, store)
    finally:
        store.close()


async def wait_for_ticket_flow_terminal(
    repo_root: Path,
    run_id: str,
    *,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.25,
) -> Optional[FlowRunRecord]:
    deadline = time.monotonic() + max(timeout_seconds, poll_interval_seconds)
    latest: Optional[FlowRunRecord] = None

    while time.monotonic() < deadline:
        record = get_ticket_flow_run_status(repo_root, run_id)
        if record is None:
            return None
        latest = record
        if record.status.is_terminal():
            return record
        try:
            record, _updated, locked = reconcile_ticket_flow_run(repo_root, run_id)
        except KeyError:
            return None
        latest = record
        if record.status.is_terminal():
            return record
        await asyncio.sleep(poll_interval_seconds)

    return latest


def archive_ticket_flow_run(
    repo_root: Path,
    run_id: str,
    *,
    force: bool = False,
    delete_run: bool = True,
) -> dict[str, Any]:
    record = get_ticket_flow_run_status(repo_root, run_id)
    if record is None:
        raise KeyError(f"Unknown flow run '{run_id}'")
    if not record.status.is_terminal():
        if force and record.status in (FlowRunStatus.STOPPING, FlowRunStatus.PAUSED):
            stop_ticket_flow_worker(repo_root, run_id)
        else:
            raise ValueError(
                "Can only archive completed/stopped/failed flows (use force=true for stuck flows)"
            )
    return archive_flow_run_artifacts(
        repo_root,
        run_id=run_id,
        force=force,
        delete_run=delete_run,
    )


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


def render_bootstrap_ticket_template(ticket_id: str) -> str:
    return f"""---
agent: codex
done: false
ticket_id: "{ticket_id}"
title: Bootstrap ticket plan
goal: Capture scope and seed follow-up tickets
---

You are the first ticket in a new ticket_flow run.

- Read `.codex-autorunner/ISSUE.md`. If it is missing:
  - If GitHub is available, ask the user for the issue/PR URL or number and create `.codex-autorunner/ISSUE.md` from it.
  - If GitHub is not available, write `DISPATCH.md` with `mode: pause` asking the user to describe the work (or share a doc). After the reply, create `.codex-autorunner/ISSUE.md` with their input.
- If helpful, create or update contextspace docs under `.codex-autorunner/contextspace/`:
  - `active_context.md` for current context and links
  - `decisions.md` for decisions/rationale
  - `spec.md` for requirements and constraints
- Break the work into additional `TICKET-00X.md` files with clear owners/goals; keep this ticket open until they exist.
- Place any supporting artifacts in `.codex-autorunner/runs/<run_id>/dispatch/` if needed.
- Write `DISPATCH.md` to dispatch a message to the user:
  - Use `mode: pause` (handoff) to wait for user response. This pauses execution.
  - Use `mode: notify` (informational) to message the user but keep running.
"""


def seed_bootstrap_ticket_if_needed(ticket_dir: Path) -> bool:
    existing = list_ticket_paths(ticket_dir)
    ticket_path = ticket_dir / "TICKET-001.md"
    if existing or ticket_path.exists():
        return False
    ticket_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_ticket_id = generate_ticket_id()
    template = render_bootstrap_ticket_template(bootstrap_ticket_id)
    ticket_path.write_text(template, encoding="utf-8")
    return True
