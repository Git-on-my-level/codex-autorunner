"""Flow-related CLI command extraction from the monolithic cli surface."""

import asyncio
import atexit
import json
import os
import signal
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import typer

from ....core.config import ConfigError
from ....core.flows import FlowController, FlowStore
from ....core.flows.models import FlowEventType, FlowRunRecord, FlowRunStatus
from ....core.flows.ux_helpers import build_flow_status_snapshot, ensure_worker
from ....core.flows.worker_process import (
    check_worker_health,
    clear_worker_metadata,
    register_worker_metadata,
    write_worker_crash_info,
    write_worker_exit_info,
)
from ....core.managed_processes import reap_managed_processes
from ....core.runtime import RuntimeContext
from ....core.utils import resolve_executable
from ....tickets import AgentPool
from ....tickets.files import (
    list_ticket_paths,
    read_ticket,
    safe_relpath,
    ticket_is_done,
)
from ....tickets.lint import lint_ticket_directory, parse_ticket_index


def _stale_terminal_runs(records: list[FlowRunRecord]) -> list[FlowRunRecord]:
    return [
        r for r in records if r.status in (FlowRunStatus.FAILED, FlowRunStatus.STOPPED)
    ]


def register_flow_commands(
    flow_app: typer.Typer,
    ticket_flow_app: typer.Typer,
    *,
    require_repo_config: Callable[[Optional[Path], Optional[Path]], RuntimeContext],
    raise_exit: Callable[..., None],
    build_agent_pool: Callable,
    build_ticket_flow_definition: Callable,
    guard_unregistered_hub_repo: Callable[[Path, Optional[Path]], None],
    parse_bool_text: Callable[..., bool],
    parse_duration: Callable[[str], object],
    cleanup_stale_flow_runs: Callable[..., int],
    archive_flow_run_artifacts: Callable[..., dict],
) -> dict[str, Callable[..., None]]:
    """Register flow-oriented subcommands and return command callables for reuse."""

    def _normalize_flow_run_id(run_id: Optional[str]) -> Optional[str]:
        if run_id is None:
            return None
        try:
            return str(uuid.UUID(str(run_id)))
        except ValueError:
            raise_exit("Invalid run_id format; must be a UUID")
        raise AssertionError("Unreachable")  # satisfies mypy return

    def _ticket_flow_paths(engine: RuntimeContext) -> tuple[Path, Path, Path]:
        db_path = engine.repo_root / ".codex-autorunner" / "flows.db"
        artifacts_root = engine.repo_root / ".codex-autorunner" / "flows"
        ticket_dir = engine.repo_root / ".codex-autorunner" / "tickets"
        return db_path, artifacts_root, ticket_dir

    @dataclass(frozen=True)
    class PreflightCheck:
        check_id: str
        status: str  # ok | warning | error
        message: str
        fix: Optional[str] = None
        details: list[str] = field(default_factory=list)

        def to_dict(self) -> dict:
            return {
                "id": self.check_id,
                "status": self.status,
                "message": self.message,
                "fix": self.fix,
                "details": list(self.details),
            }

    @dataclass(frozen=True)
    class PreflightReport:
        checks: list[PreflightCheck]

        def has_errors(self) -> bool:
            return any(check.status == "error" for check in self.checks)

        def to_dict(self) -> dict:
            return {
                "ok": sum(1 for check in self.checks if check.status == "ok"),
                "warnings": sum(
                    1 for check in self.checks if check.status == "warning"
                ),
                "errors": sum(1 for check in self.checks if check.status == "error"),
                "checks": [check.to_dict() for check in self.checks],
            }

    def _print_preflight_report(report: PreflightReport) -> None:
        for check in report.checks:
            status = check.status.upper()
            typer.echo(f"- {status}: {check.message}")
            if check.details:
                for detail in check.details:
                    typer.echo(f"    {detail}")
            if check.fix:
                typer.echo(f"    Fix: {check.fix}")

    def _ticket_lint_details(ticket_dir: Path) -> dict[str, list[str]]:
        details: dict[str, list[str]] = {
            "invalid_filenames": [],
            "duplicate_indices": [],
            "frontmatter": [],
        }
        if not ticket_dir.exists():
            return details

        ticket_root = ticket_dir.parent
        for path in sorted(ticket_dir.iterdir()):
            if not path.is_file():
                continue
            if path.name == "AGENTS.md":
                continue
            if parse_ticket_index(path.name) is None:
                rel_path = safe_relpath(path, ticket_root)
                details["invalid_filenames"].append(
                    f"{rel_path}: Invalid ticket filename; expected TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md)"
                )

        details["duplicate_indices"].extend(lint_ticket_directory(ticket_dir))

        ticket_paths = list_ticket_paths(ticket_dir)
        for path in ticket_paths:
            _, ticket_errors = read_ticket(path)
            for err in ticket_errors:
                details["frontmatter"].append(
                    f"{path.relative_to(path.parent.parent)}: {err}"
                )

        return details

    def _ticket_flow_preflight(
        engine: RuntimeContext, ticket_dir: Path
    ) -> PreflightReport:
        checks: list[PreflightCheck] = []

        state_root = engine.repo_root / ".codex-autorunner"
        if state_root.exists():
            checks.append(
                PreflightCheck(
                    check_id="repo_initialized",
                    status="ok",
                    message="Repo initialized (.codex-autorunner present).",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    check_id="repo_initialized",
                    status="error",
                    message="Repo not initialized (.codex-autorunner missing).",
                    fix="Run `car init` in the repo root.",
                )
            )

        if ticket_dir.exists():
            checks.append(
                PreflightCheck(
                    check_id="ticket_dir",
                    status="ok",
                    message=f"Ticket directory found: {ticket_dir.relative_to(engine.repo_root)}.",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    check_id="ticket_dir",
                    status="error",
                    message="Ticket directory missing.",
                    fix="Run `car flow ticket_flow bootstrap` to create the ticket dir and seed TICKET-001.",
                )
            )

        ticket_paths = list_ticket_paths(ticket_dir)
        if ticket_paths:
            checks.append(
                PreflightCheck(
                    check_id="tickets_present",
                    status="ok",
                    message=f"Found {len(ticket_paths)} ticket(s).",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    check_id="tickets_present",
                    status="error",
                    message="No tickets found.",
                    fix="Create tickets under .codex-autorunner/tickets or run `car flow ticket_flow bootstrap`.",
                )
            )

        lint_details = _ticket_lint_details(ticket_dir)
        if lint_details["invalid_filenames"]:
            checks.append(
                PreflightCheck(
                    check_id="ticket_filenames",
                    status="error",
                    message="Invalid ticket filenames detected.",
                    fix="Rename tickets to TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md).",
                    details=lint_details["invalid_filenames"],
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    check_id="ticket_filenames",
                    status="ok",
                    message="Ticket filenames are valid.",
                )
            )

        if lint_details["duplicate_indices"]:
            checks.append(
                PreflightCheck(
                    check_id="duplicate_indices",
                    status="error",
                    message="Duplicate ticket indices detected.",
                    fix="Rename or remove duplicates so each index is unique.",
                    details=lint_details["duplicate_indices"],
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    check_id="duplicate_indices",
                    status="ok",
                    message="Ticket indices are unique.",
                )
            )

        if lint_details["frontmatter"]:
            checks.append(
                PreflightCheck(
                    check_id="frontmatter",
                    status="error",
                    message="Ticket frontmatter validation failed.",
                    fix="Fix the YAML frontmatter in the listed tickets.",
                    details=lint_details["frontmatter"],
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    check_id="frontmatter",
                    status="ok",
                    message="Ticket frontmatter passes validation.",
                )
            )

        ticket_docs = []
        for path in ticket_paths:
            doc, errors = read_ticket(path)
            if doc is not None and not errors:
                ticket_docs.append(doc)

        if ticket_docs:
            agents = sorted({doc.frontmatter.agent for doc in ticket_docs})
            agent_errors: list[str] = []
            agent_warnings: list[str] = []

            if "codex" in agents:
                app_cmd = engine.config.app_server.command or []
                app_binary = app_cmd[0] if app_cmd else None
                resolved = resolve_executable(app_binary) if app_binary else None
                if not resolved:
                    agent_errors.append(
                        "codex: app_server command not available in PATH."
                    )

            if "opencode" in agents:
                opencode_cmd = engine.config.agent_serve_command("opencode")
                opencode_binary: Optional[str] = None
                if opencode_cmd:
                    opencode_binary = resolve_executable(opencode_cmd[0])
                if not opencode_binary:
                    try:
                        opencode_binary = resolve_executable(
                            engine.config.agent_binary("opencode")
                        )
                    except ConfigError:
                        opencode_binary = None
                if not opencode_binary:
                    agent_errors.append(
                        "opencode: backend unavailable (missing binary/serve command)."
                    )

            for agent in agents:
                if agent in ("codex", "opencode", "user"):
                    continue
                agent_warnings.append(
                    f"{agent}: availability not verified; ensure its backend is configured."
                )

            if agent_errors:
                checks.append(
                    PreflightCheck(
                        check_id="agents",
                        status="error",
                        message="One or more agents are unavailable.",
                        fix="Install missing agents or update agents.<id>.binary/serve_command in config.",
                        details=agent_errors,
                    )
                )
            elif agent_warnings:
                checks.append(
                    PreflightCheck(
                        check_id="agents",
                        status="warning",
                        message="Agents detected but availability could not be verified.",
                        details=agent_warnings,
                    )
                )
            else:
                checks.append(
                    PreflightCheck(
                        check_id="agents",
                        status="ok",
                        message="All referenced agents appear available.",
                    )
                )
        else:
            checks.append(
                PreflightCheck(
                    check_id="agents",
                    status="warning",
                    message="Agent availability skipped (no valid tickets to inspect).",
                )
            )

        return PreflightReport(checks=checks)

    def _open_flow_store(engine: RuntimeContext) -> FlowStore:
        db_path, _, _ = _ticket_flow_paths(engine)
        store = FlowStore(db_path, durable=engine.config.durable_writes)
        store.initialize()
        return store

    def _active_or_paused_run(records: list[FlowRunRecord]) -> Optional[FlowRunRecord]:
        for record in records:
            if record.status in (FlowRunStatus.RUNNING, FlowRunStatus.PAUSED):
                return record
        return None

    def _resumable_run(
        records: list[FlowRunRecord],
    ) -> tuple[Optional[FlowRunRecord], str]:
        """Return a resumable run and the reason."""
        if not records:
            return None, "new_run"
        active = _active_or_paused_run(records)
        if active:
            return active, "active"
        latest = records[0]
        if latest.status == FlowRunStatus.COMPLETED:
            return latest, "completed_pending"
        return None, "new_run"

    def _ticket_flow_status_payload(
        engine: RuntimeContext, record: FlowRunRecord, store: Optional[FlowStore]
    ) -> dict:
        snapshot = build_flow_status_snapshot(engine.repo_root, record, store)
        health = snapshot.get("worker_health")
        effective_ticket = snapshot.get("effective_current_ticket")
        return {
            "run_id": record.id,
            "flow_type": record.flow_type,
            "status": record.status.value,
            "current_step": record.current_step,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "last_event_seq": snapshot.get("last_event_seq"),
            "last_event_at": snapshot.get("last_event_at"),
            "current_ticket": effective_ticket,
            "ticket_progress": snapshot.get("ticket_progress"),
            "worker": (
                {
                    "status": health.status,
                    "pid": health.pid,
                    "message": health.message,
                    "exit_code": getattr(health, "exit_code", None),
                    "stderr_tail": getattr(health, "stderr_tail", None),
                }
                if health
                else None
            ),
        }

    def _print_ticket_flow_status(payload: dict) -> None:
        typer.echo(f"Run id: {payload.get('run_id')}")
        typer.echo(f"Status: {payload.get('status')}")
        progress = payload.get("ticket_progress") or {}
        if isinstance(progress, dict):
            done = progress.get("done")
            total = progress.get("total")
            if isinstance(done, int) and isinstance(total, int):
                typer.echo(f"Tickets: {done}/{total}")
        typer.echo(f"Current step: {payload.get('current_step')}")
        typer.echo(f"Current ticket: {payload.get('current_ticket') or 'n/a'}")
        typer.echo(f"Created at: {payload.get('created_at')}")
        typer.echo(f"Started at: {payload.get('started_at')}")
        typer.echo(f"Finished at: {payload.get('finished_at')}")
        typer.echo(
            f"Last event: {payload.get('last_event_at')} (seq={payload.get('last_event_seq')})"
        )
        worker = payload.get("worker") or {}
        status = payload.get("status") or ""
        if worker and status not in {"completed", "failed", "stopped"}:
            typer.echo(
                f"Worker: {worker.get('status')} pid={worker.get('pid')} {worker.get('message') or ''}".rstrip()
            )
        elif worker and status in {"completed", "failed", "stopped"}:
            worker_status = worker.get("status") or ""
            worker_pid = worker.get("pid")
            worker_msg = worker.get("message") or ""
            if worker_status == "absent" or "missing" in worker_msg.lower():
                typer.echo("Worker: exited")
            elif worker_status == "dead" or "not running" in worker_msg.lower():
                typer.echo(f"Worker: exited (pid={worker_pid})")
            else:
                typer.echo(
                    f"Worker: {worker.get('status')} pid={worker.get('pid')} {worker.get('message') or ''}".rstrip()
                )
            if status == "failed":
                exit_code = worker.get("exit_code")
                stderr_tail = worker.get("stderr_tail")
                if exit_code is not None:
                    typer.echo(f"Worker exit code: {exit_code}")
                if isinstance(stderr_tail, str) and stderr_tail.strip():
                    typer.echo(f"Worker stderr tail: {stderr_tail.strip()}")

    def _start_ticket_flow_worker(
        repo_root: Path, run_id: str, is_terminal: bool = False
    ) -> None:
        result = ensure_worker(repo_root, run_id, is_terminal=is_terminal)
        if result == {"status": "reused"}:
            return

    def _stop_ticket_flow_worker(repo_root: Path, run_id: str) -> None:
        health = check_worker_health(repo_root, run_id)
        if health.status in {"dead", "mismatch", "invalid"}:
            try:
                clear_worker_metadata(health.artifact_path.parent)
            except Exception:
                pass
        if not health.pid:
            return
        try:
            if os.name != "nt" and hasattr(os, "killpg"):
                # Workers are spawned as session/group leaders, so pgid == pid.
                os.killpg(health.pid, signal.SIGTERM)
            else:
                os.kill(health.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Keep stop idempotent when process ownership changed unexpectedly.
            pass
        except Exception:
            try:
                os.kill(health.pid, signal.SIGTERM)
            except Exception:
                pass

    def _ticket_flow_controller(
        engine: RuntimeContext,
    ) -> tuple[FlowController, AgentPool]:
        db_path, artifacts_root, _ = _ticket_flow_paths(engine)
        agent_pool = build_agent_pool(engine.config)
        definition = build_ticket_flow_definition(agent_pool=agent_pool)
        definition.validate()
        controller = FlowController(
            definition=definition,
            db_path=db_path,
            artifacts_root=artifacts_root,
            durable=engine.config.durable_writes,
        )
        controller.initialize()
        return controller, agent_pool

    @flow_app.command("worker")
    def flow_worker(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        run_id: Optional[str] = typer.Option(
            None, "--run-id", help="Flow run ID (required)"
        ),
    ):
        """Start a flow worker process for an existing run."""
        engine = require_repo_config(repo, hub)
        try:
            cleanup = reap_managed_processes(engine.repo_root)
            if cleanup.killed or cleanup.signaled or cleanup.removed:
                typer.echo(
                    f"Managed process cleanup: killed {cleanup.killed}, signaled {cleanup.signaled}, removed {cleanup.removed} records, skipped {cleanup.skipped}"
                )
        except Exception as exc:
            typer.echo(f"Managed process cleanup failed: {exc}", err=True)
        normalized_run_id = _normalize_flow_run_id(run_id)
        if normalized_run_id is None:
            raise_exit("--run-id is required for worker command")

        db_path, artifacts_root, ticket_dir = _ticket_flow_paths(engine)

        typer.echo(f"Starting flow worker for run {normalized_run_id}")

        exit_code_holder = [0]
        _repo_root = engine.repo_root
        _artifacts_root = artifacts_root
        shutdown_event = threading.Event()

        def _write_exit_info(*, shutdown_intent: bool = False) -> None:
            try:
                write_worker_exit_info(
                    _repo_root,
                    normalized_run_id,  # type: ignore[arg-type]
                    returncode=exit_code_holder[0] or None,
                    shutdown_intent=shutdown_intent,
                    artifacts_root=_artifacts_root,
                )
            except Exception:
                pass

        def _signal_handler(signum: int, _frame) -> None:
            exit_code_holder[0] = -signum
            shutdown_event.set()

        atexit.register(_write_exit_info)
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        async def _run_worker():
            typer.echo(f"Flow worker started for {normalized_run_id}")
            typer.echo(f"DB path: {db_path}")
            typer.echo(f"Artifacts root: {artifacts_root}")

            store = FlowStore(db_path, durable=engine.config.durable_writes)
            store.initialize()

            record = store.get_flow_run(normalized_run_id)
            if not record:
                typer.echo(f"Flow run {normalized_run_id} not found", err=True)
                store.close()
                raise typer.Exit(code=1)

            if record.flow_type == "ticket_flow":
                report = _ticket_flow_preflight(engine, ticket_dir)
                if report.has_errors():
                    typer.echo("Ticket flow preflight failed:", err=True)
                    _print_preflight_report(report)
                    store.close()
                    raise typer.Exit(code=1)

            store.close()

            try:
                register_worker_metadata(
                    engine.repo_root,
                    normalized_run_id,
                    artifacts_root=artifacts_root,
                )
            except Exception as exc:
                typer.echo(f"Failed to register worker metadata: {exc}", err=True)

            agent_pool: Optional[AgentPool] = None

            def _build_definition(flow_type: str):
                nonlocal agent_pool
                if flow_type == "pr_flow":
                    raise_exit(
                        "PR flow is no longer supported. Use ticket_flow instead."
                    )
                if flow_type == "ticket_flow":
                    agent_pool = build_agent_pool(engine.config)
                    return build_ticket_flow_definition(agent_pool=agent_pool)
                raise_exit(
                    f"Unknown flow type for run {normalized_run_id}: {flow_type}"
                )
                return None

            definition = _build_definition(record.flow_type)
            definition.validate()

            controller = FlowController(
                definition=definition,
                db_path=db_path,
                artifacts_root=artifacts_root,
                durable=engine.config.durable_writes,
            )
            controller.initialize()
            shutdown_requested = False
            try:
                record = controller.get_status(normalized_run_id)
                if not record:
                    typer.echo(f"Flow run {normalized_run_id} not found", err=True)
                    raise typer.Exit(code=1)

                if record.status.is_terminal() and record.status not in {
                    FlowRunStatus.STOPPED,
                    FlowRunStatus.FAILED,
                }:
                    typer.echo(
                        f"Flow run {normalized_run_id} already completed (status={record.status})"
                    )
                    return

                action = (
                    "Resuming" if record.status != FlowRunStatus.PENDING else "Starting"
                )
                typer.echo(
                    f"{action} flow run {normalized_run_id} from step: {record.current_step}"
                )

                run_task = asyncio.create_task(controller.run_flow(normalized_run_id))
                shutdown_wait_task = asyncio.create_task(
                    asyncio.to_thread(shutdown_event.wait)
                )
                done, _ = await asyncio.wait(
                    {run_task, shutdown_wait_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                shutdown_requested = (
                    shutdown_wait_task in done and shutdown_event.is_set()
                )

                if shutdown_requested and not run_task.done():
                    await controller.stop_flow(normalized_run_id)
                    run_task.cancel()

                if run_task.done():
                    final_record = await run_task
                    typer.echo(
                        f"Flow run {normalized_run_id} finished with status {final_record.status}"
                    )
                else:
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        typer.echo(f"Flow run {normalized_run_id} cancelled by signal")
                if not shutdown_wait_task.done():
                    shutdown_wait_task.cancel()
            except Exception as exc:
                exit_code_holder[0] = 1
                last_event = None
                try:
                    app_event = controller.store.get_last_event_by_type(
                        normalized_run_id, FlowEventType.APP_SERVER_EVENT
                    )
                    if app_event and isinstance(app_event.data, dict):
                        msg = app_event.data.get("message")
                        if isinstance(msg, dict):
                            method = msg.get("method")
                            if isinstance(method, str) and method.strip():
                                last_event = method.strip()
                except Exception:
                    last_event = None
                write_worker_crash_info(
                    engine.repo_root,
                    normalized_run_id,
                    worker_pid=os.getpid(),
                    exit_code=1,
                    last_event=last_event,
                    exception=f"{type(exc).__name__}: {exc}",
                    stack_trace=traceback.format_exc(),
                    artifacts_root=artifacts_root,
                )
                raise
            finally:
                controller.shutdown()
                if agent_pool is not None:
                    try:
                        await agent_pool.close_all()
                    except Exception:
                        typer.echo("Failed to close agent pool cleanly", err=True)
                _write_exit_info()

        asyncio.run(_run_worker())

    @ticket_flow_app.command("bootstrap")
    def ticket_flow_bootstrap(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        force_new: bool = typer.Option(
            False, "--force-new", help="Always create a new run"
        ),
    ):
        """Bootstrap ticket_flow (seed TICKET-001 if needed) and start a run."""
        engine = require_repo_config(repo, hub)
        guard_unregistered_hub_repo(engine.repo_root, hub)
        db_path, artifacts_root, ticket_dir = _ticket_flow_paths(engine)
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"

        store = _open_flow_store(engine)
        stale_terminal: list[FlowRunRecord] = []
        try:
            records = store.list_flow_runs(flow_type="ticket_flow")
            stale_terminal = _stale_terminal_runs(records)
            if not force_new:
                existing_run, reason = _resumable_run(records)
                if existing_run and reason == "active":
                    _start_ticket_flow_worker(
                        engine.repo_root, existing_run.id, is_terminal=False
                    )
                    typer.echo(f"Reused active run: {existing_run.id}")
                    typer.echo(
                        f"Next: car flow ticket_flow status --repo {engine.repo_root} --run-id {existing_run.id}"
                    )
                    return
                elif existing_run and reason == "completed_pending":
                    existing_tickets = list_ticket_paths(ticket_dir)
                    pending_count = len(
                        [t for t in existing_tickets if not ticket_is_done(t)]
                    )
                    if pending_count > 0:
                        typer.echo(
                            f"Warning: Latest run {existing_run.id} is COMPLETED with {pending_count} pending ticket(s)."
                        )
                        typer.echo(
                            "Use --force-new to start a fresh run (dispatch history will be reset)."
                        )
                        raise_exit("Add --force-new to create a new run.")
        finally:
            store.close()

        if stale_terminal:
            typer.echo(
                f"Warning: {len(stale_terminal)} stale run(s) found (FAILED/STOPPED)."
            )
            typer.echo(
                f"Consider 'car flow ticket_flow resume --run-id {stale_terminal[0].id}' "
                "to resume instead of starting fresh."
            )
            typer.echo("Use --force-new to suppress this warning and start a new run.")

        existing_tickets = list_ticket_paths(ticket_dir)
        seeded = False
        if not existing_tickets and not ticket_path.exists():
            template = """---
agent: codex
done: false
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
            ticket_path.write_text(template, encoding="utf-8")
            seeded = True

        db_path, _, _ = _ticket_flow_paths(engine)
        controller, agent_pool = _ticket_flow_controller(engine)
        try:
            run_id = str(uuid.uuid4())
            record = asyncio.run(
                controller.start_flow(
                    input_data={},
                    run_id=run_id,
                    metadata={"seeded_ticket": seeded},
                )
            )
            _start_ticket_flow_worker(engine.repo_root, record.id, is_terminal=False)
        finally:
            controller.shutdown()
            asyncio.run(agent_pool.close_all())

        typer.echo(f"Started ticket_flow run: {run_id}")
        typer.echo(
            f"Next: car flow ticket_flow status --repo {engine.repo_root} --run-id {run_id}"
        )

    @ticket_flow_app.command("preflight")
    def ticket_flow_preflight(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        output_json: bool = typer.Option(
            True, "--json/--no-json", help="Emit JSON output (default: true)"
        ),
    ):
        """Run ticket_flow preflight checks."""
        engine = require_repo_config(repo, hub)
        guard_unregistered_hub_repo(engine.repo_root, hub)
        _, _, ticket_dir = _ticket_flow_paths(engine)

        report = _ticket_flow_preflight(engine, ticket_dir)
        if output_json:
            typer.echo(json.dumps(report.to_dict(), indent=2))
            if report.has_errors():
                raise typer.Exit(code=1)
            return

        _print_preflight_report(report)
        if report.has_errors():
            raise_exit("Ticket flow preflight failed.")

    @ticket_flow_app.command("start")
    def ticket_flow_start(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        force_new: bool = typer.Option(
            False, "--force-new", help="Always create a new run"
        ),
    ):
        """Start or resume the latest ticket_flow run."""
        engine = require_repo_config(repo, hub)
        guard_unregistered_hub_repo(engine.repo_root, hub)
        _, _, ticket_dir = _ticket_flow_paths(engine)
        ticket_dir.mkdir(parents=True, exist_ok=True)

        store = _open_flow_store(engine)
        stale_terminal: list[FlowRunRecord] = []
        try:
            records = store.list_flow_runs(flow_type="ticket_flow")
            stale_terminal = _stale_terminal_runs(records)
            if not force_new:
                existing_run, reason = _resumable_run(records)
                if existing_run and reason == "active":
                    report = _ticket_flow_preflight(engine, ticket_dir)
                    if report.has_errors():
                        typer.echo("Ticket flow preflight failed:", err=True)
                        _print_preflight_report(report)
                        raise_exit(
                            "Fix the above errors before starting the ticket flow."
                        )
                    _start_ticket_flow_worker(
                        engine.repo_root, existing_run.id, is_terminal=False
                    )
                    typer.echo(f"Reused active run: {existing_run.id}")
                    typer.echo(
                        f"Next: car flow ticket_flow status --repo {engine.repo_root} --run-id {existing_run.id}"
                    )
                    return
                elif existing_run and reason == "completed_pending":
                    existing_tickets = list_ticket_paths(ticket_dir)
                    pending_count = len(
                        [t for t in existing_tickets if not ticket_is_done(t)]
                    )
                    if pending_count > 0:
                        typer.echo(
                            f"Warning: Latest run {existing_run.id} is COMPLETED with {pending_count} pending ticket(s)."
                        )
                        typer.echo(
                            "Use --force-new to start a fresh run (dispatch history will be reset)."
                        )
                        raise_exit("Add --force-new to create a new run.")
        finally:
            store.close()

        if stale_terminal:
            typer.echo(
                f"Warning: {len(stale_terminal)} stale run(s) found (FAILED/STOPPED)."
            )
            typer.echo(
                f"Consider 'car flow ticket_flow resume --run-id {stale_terminal[0].id}' "
                "to resume instead of starting fresh."
            )
            typer.echo("Use --force-new to suppress this warning and start a new run.")

        report = _ticket_flow_preflight(engine, ticket_dir)
        if report.has_errors():
            typer.echo("Ticket flow preflight failed:", err=True)
            _print_preflight_report(report)
            raise_exit("Fix the above errors before starting the ticket flow.")

        controller, agent_pool = _ticket_flow_controller(engine)
        try:
            run_id = str(uuid.uuid4())
            input_data = {"workspace_root": str(engine.repo_root)}
            record = asyncio.run(
                controller.start_flow(input_data=input_data, run_id=run_id)
            )
            _start_ticket_flow_worker(engine.repo_root, record.id, is_terminal=False)
        finally:
            controller.shutdown()
            asyncio.run(agent_pool.close_all())

        typer.echo(f"Started ticket_flow run: {run_id}")
        typer.echo(
            f"Next: car flow ticket_flow status --repo {engine.repo_root} --run-id {run_id}"
        )

    @ticket_flow_app.command("status")
    def ticket_flow_status(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Show status for a ticket_flow run."""
        engine = require_repo_config(repo, hub)
        normalized_run_id = _normalize_flow_run_id(run_id)

        store = _open_flow_store(engine)
        try:
            record = None
            if normalized_run_id:
                record = store.get_flow_run(normalized_run_id)
            else:
                records = store.list_flow_runs(flow_type="ticket_flow")
                record = records[0] if records else None
            if not record:
                raise_exit("No ticket_flow runs found.")
            assert record is not None
            normalized_run_id = record.id
        finally:
            store.close()

        _, _, ticket_dir = _ticket_flow_paths(engine)
        report = _ticket_flow_preflight(engine, ticket_dir)
        if report.has_errors():
            typer.echo("Ticket flow preflight failed:", err=True)
            _print_preflight_report(report)
            raise_exit("Fix the above errors before resuming the ticket flow.")

    @ticket_flow_app.command("stop")
    def ticket_flow_stop(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
    ):
        """Stop a ticket_flow run."""
        engine = require_repo_config(repo, hub)
        normalized_run_id = _normalize_flow_run_id(run_id)

        store = _open_flow_store(engine)
        try:
            record = None
            if normalized_run_id:
                record = store.get_flow_run(normalized_run_id)
            else:
                records = store.list_flow_runs(flow_type="ticket_flow")
                record = records[0] if records else None
            if not record:
                raise_exit("No ticket_flow runs found.")
            assert record is not None
            normalized_run_id = record.id
        finally:
            store.close()

        controller, agent_pool = _ticket_flow_controller(engine)
        try:
            _stop_ticket_flow_worker(engine.repo_root, normalized_run_id)
            updated = asyncio.run(controller.stop_flow(normalized_run_id))
        finally:
            controller.shutdown()
            asyncio.run(agent_pool.close_all())

        typer.echo(
            f"Stop requested for run: {updated.id} (status={updated.status.value})"
        )

    @ticket_flow_app.command("archive")
    def ticket_flow_archive(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
        force: bool = typer.Option(
            False, "--force", help="Allow archiving paused/stopping runs"
        ),
        delete_run: str = typer.Option(
            "true",
            "--delete-run",
            help="Delete flow run record after archive (true|false)",
        ),
        dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Archive run artifacts and optionally delete the flow run record."""
        engine = require_repo_config(repo, hub)
        normalized_run_id = _normalize_flow_run_id(run_id)
        if not normalized_run_id:
            raise_exit("--run-id is required")
        parsed_delete_run = parse_bool_text(delete_run, flag="--delete-run")
        run_id_str: str = normalized_run_id  # type: ignore[assignment]

        store = _open_flow_store(engine)
        try:
            record = store.get_flow_run(run_id_str)
            if record is None:
                raise_exit(f"Flow run not found: {normalized_run_id}")
            assert record is not None
            try:
                summary = archive_flow_run_artifacts(
                    repo_root=engine.repo_root,
                    store=store,
                    record=record,
                    force=force,
                    delete_run=parsed_delete_run,
                    dry_run=dry_run,
                )
            except ValueError as exc:
                raise_exit(str(exc), cause=exc)
        finally:
            store.close()

        if output_json:
            typer.echo(json.dumps(summary, indent=2))
            return
        typer.echo(
            f"Archived run {summary.get('run_id')} status={summary.get('status')} "
            f"archived_runs={summary.get('archived_runs')} "
            f"deleted_run={summary.get('deleted_run')} dry_run={dry_run}"
        )

    def ticket_flow_preflight_report(
        engine: RuntimeContext, ticket_dir: Path
    ) -> PreflightReport:
        return _ticket_flow_preflight(engine, ticket_dir)

    return {
        "PreflightCheck": PreflightCheck,  # type: ignore[dict-item]
        "PreflightReport": PreflightReport,  # type: ignore[dict-item]
        "ticket_flow_start": ticket_flow_start,
        "ticket_flow_preflight": ticket_flow_preflight,
        "_ticket_flow_preflight": _ticket_flow_preflight,  # type: ignore[dict-item]
        "ticket_flow_preflight_report": ticket_flow_preflight_report,  # type: ignore[dict-item]
        "ticket_flow_print_preflight_report": _print_preflight_report,
        "ticket_flow_resumable_run": _resumable_run,  # type: ignore[dict-item]
    }
