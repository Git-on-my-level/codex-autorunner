"""Flow-related CLI command extraction from the monolithic cli surface."""

import asyncio
import atexit
import json
import logging
import os
import signal
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, cast

import typer

from ....core.config import ConfigError
from ....core.flows import FlowController, FlowStore
from ....core.flows.flow_housekeeping import (
    FlowRetentionConfig,
    build_plan,
    execute_housekeep,
    gather_stats,
    parse_flow_retention_config,
)
from ....core.flows.models import (
    FlowEventType,
    FlowRunRecord,
    FlowRunStatus,
    flow_run_duration_seconds,
    format_flow_duration,
)
from ....core.flows.start_policy import evaluate_ticket_start_policy
from ....core.flows.telemetry_export import export_all_runs
from ....core.flows.ux_helpers import build_flow_status_snapshot, ensure_worker
from ....core.flows.worker_process import (
    check_worker_health,
    clear_worker_metadata,
    register_worker_metadata,
    write_worker_crash_info,
    write_worker_exit_info,
)
from ....core.force_attestation import (
    FORCE_ATTESTATION_REQUIRED_PHRASE,
    validate_force_attestation,
)
from ....core.managed_processes import reap_managed_processes
from ....core.orchestration import build_ticket_flow_orchestration_service
from ....core.orchestration.models import FlowRunTarget
from ....core.runtime import RuntimeContext
from ....core.utils import resolve_executable
from ....tickets import DEFAULT_MAX_TOTAL_TURNS, AgentPool
from ....tickets.files import list_ticket_paths, read_ticket, ticket_is_done
from ....tickets.frontmatter import generate_ticket_id
from ..hub_path_option import hub_root_path_option

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlowCommandExports:
    PreflightCheck: type[Any]
    PreflightReport: type[Any]
    ticket_flow_start: Callable[..., Any]
    ticket_flow_preflight: Callable[..., Any]
    _ticket_flow_preflight: Callable[..., Any]
    ticket_flow_preflight_report: Callable[..., Any]
    ticket_flow_print_preflight_report: Callable[[Any], None]
    ticket_flow_resumable_run: Callable[..., Any]


def _build_force_attestation(
    force_attestation: Optional[str], *, target_scope: str
) -> Optional[dict[str, str]]:
    if force_attestation is None:
        return None
    return {
        "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
        "user_request": force_attestation,
        "target_scope": target_scope,
    }


def _stale_terminal_runs(records: list[FlowRunRecord]) -> list[FlowRunRecord]:
    return [
        r for r in records if r.status in (FlowRunStatus.FAILED, FlowRunStatus.STOPPED)
    ]


def register_flow_commands(
    flow_app: typer.Typer,
    ticket_flow_app: typer.Typer,
    telemetry_app: typer.Typer,
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
) -> FlowCommandExports:
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
            parts = [f"{status}: {check.message}"]
            if check.details:
                parts.append(" ".join(check.details))
            if check.fix:
                parts.append(f"fix: {check.fix}")
            typer.echo(" ".join(parts))

    def _ticket_lint_details(ticket_dir: Path) -> dict[str, list[str]]:
        policy = evaluate_ticket_start_policy(ticket_dir)
        return {
            "invalid_filenames": list(policy.invalid_filenames),
            "duplicate_indices": list(policy.duplicate_indices),
            "frontmatter": list(policy.frontmatter),
        }

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
                    fix="Run `car ticket-flow bootstrap` to create the ticket dir and seed TICKET-001.",
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
                    fix="Create tickets under .codex-autorunner/tickets or run `car ticket-flow bootstrap`.",
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
        state = record.state if isinstance(record.state, dict) else {}
        raw_ticket_engine = state.get("ticket_engine")
        ticket_engine: dict[str, Any]
        if isinstance(raw_ticket_engine, dict):
            ticket_engine = raw_ticket_engine
        else:
            ticket_engine = {}
        reason_summary = state.get("reason_summary")
        normalized_reason_summary = (
            reason_summary.strip()
            if isinstance(reason_summary, str) and reason_summary.strip()
            else None
        )
        reason = ticket_engine.get("reason")
        normalized_reason = (
            reason.strip() if isinstance(reason, str) and reason.strip() else None
        )
        reason_code = ticket_engine.get("reason_code")
        normalized_reason_code = (
            reason_code.strip()
            if isinstance(reason_code, str) and reason_code.strip()
            else None
        )
        reason_details = ticket_engine.get("reason_details")
        normalized_reason_details = (
            reason_details.strip()
            if isinstance(reason_details, str) and reason_details.strip()
            else None
        )
        error_message = (
            record.error_message.strip()
            if isinstance(record.error_message, str) and record.error_message.strip()
            else None
        )
        return {
            "run_id": record.id,
            "flow_type": record.flow_type,
            "status": record.status.value,
            "current_step": record.current_step,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "duration_seconds": flow_run_duration_seconds(record),
            "last_event_seq": snapshot.get("last_event_seq"),
            "last_event_at": snapshot.get("last_event_at"),
            "current_ticket": effective_ticket,
            "ticket_progress": snapshot.get("ticket_progress"),
            "reason_summary": normalized_reason_summary,
            "reason": normalized_reason,
            "reason_code": normalized_reason_code,
            "reason_details": normalized_reason_details,
            "error_message": error_message,
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
        progress = payload.get("ticket_progress") or {}
        tickets = ""
        if isinstance(progress, dict):
            done = progress.get("done")
            total = progress.get("total")
            if isinstance(done, int) and isinstance(total, int):
                tickets = f" tickets={done}/{total}"
        typer.echo(
            f"run_id={payload.get('run_id')} status={payload.get('status')}{tickets}"
        )
        step = payload.get("current_step")
        ticket = payload.get("current_ticket") or "n/a"
        typer.echo(f"step={step} ticket={ticket}")
        typer.echo(
            f"created={payload.get('created_at')} started={payload.get('started_at')} "
            f"finished={payload.get('finished_at')}"
        )
        duration_str = format_flow_duration(payload.get("duration_seconds"))
        if duration_str:
            typer.echo(f"duration={duration_str}")
        typer.echo(
            f"last_event={payload.get('last_event_at')} seq={payload.get('last_event_seq')}"
        )
        status = payload.get("status") or ""
        reason_summary = payload.get("reason_summary")
        if isinstance(reason_summary, str) and reason_summary.strip():
            typer.echo(f"summary: {reason_summary.strip()}")
        reason_code = payload.get("reason_code")
        if isinstance(reason_code, str) and reason_code.strip():
            typer.echo(f"reason_code: {reason_code.strip()}")
        reason = payload.get("reason")
        if (
            isinstance(reason, str)
            and reason.strip()
            and reason.strip() != str(reason_summary or "").strip()
        ):
            typer.echo(f"reason: {reason.strip()}")
        reason_details = payload.get("reason_details")
        if isinstance(reason_details, str) and reason_details.strip():
            typer.echo(f"reason_details: {reason_details.strip()}")
        error_message = payload.get("error_message")
        if isinstance(error_message, str) and error_message.strip():
            typer.echo(f"error: {error_message.strip()}")
        worker = payload.get("worker") or {}
        if worker:
            if status not in {"completed", "failed", "stopped"}:
                typer.echo(
                    f"worker: {worker.get('status')} pid={worker.get('pid')}".rstrip()
                )
            else:
                worker_status = worker.get("status") or ""
                worker_msg = worker.get("message") or ""
                if worker_status == "absent" or "missing" in worker_msg.lower():
                    typer.echo("worker: exited")
                else:
                    typer.echo(
                        f"worker: {worker.get('status')} pid={worker.get('pid')}".rstrip()
                    )
                if status == "failed":
                    exit_code = worker.get("exit_code")
                    stderr_tail = worker.get("stderr_tail")
                    if exit_code is not None:
                        typer.echo(f"worker_exit={exit_code}")
                    if isinstance(stderr_tail, str) and stderr_tail.strip():
                        typer.echo(f"worker_stderr: {stderr_tail.strip()}")

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
            except OSError:
                logger.debug("Failed to clear worker metadata", exc_info=True)
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
        except OSError:
            try:
                os.kill(health.pid, signal.SIGTERM)
            except OSError:
                logger.debug(
                    "Fallback SIGTERM to worker pid %d failed",
                    health.pid,
                    exc_info=True,
                )

    def _ticket_flow_controller(
        engine: RuntimeContext,
    ) -> tuple[FlowController, AgentPool]:
        db_path, artifacts_root, _ = _ticket_flow_paths(engine)
        agent_pool = build_agent_pool(engine.config)
        definition = build_ticket_flow_definition(
            agent_pool=agent_pool,
            auto_commit_default=engine.config.git_auto_commit,
            include_previous_ticket_context_default=(
                engine.config.ticket_flow.include_previous_ticket_context
            ),
            max_total_turns_default=(
                engine.config.ticket_flow.max_total_turns
                if engine.config.ticket_flow.max_total_turns is not None
                else DEFAULT_MAX_TOTAL_TURNS
            ),
        )
        definition.validate()
        controller = FlowController(
            definition=definition,
            db_path=db_path,
            artifacts_root=artifacts_root,
            durable=engine.config.durable_writes,
        )
        controller.initialize()
        return controller, agent_pool

    def _ticket_flow_orchestration_service(engine: RuntimeContext):
        return build_ticket_flow_orchestration_service(workspace_root=engine.repo_root)

    def _flow_run_record_from_target(target: FlowRunTarget) -> FlowRunRecord:
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

    @flow_app.command("worker")
    def flow_worker(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
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
                    f"cleanup: killed={cleanup.killed} signaled={cleanup.signaled} "
                    f"removed={cleanup.removed} skipped={cleanup.skipped}"
                )
        except (OSError, RuntimeError) as exc:
            typer.echo(f"Managed process cleanup failed: {exc}", err=True)
        normalized_run_id = _normalize_flow_run_id(run_id)
        if normalized_run_id is None:
            raise_exit("--run-id is required for worker command")
        worker_run_id: str = cast(str, normalized_run_id)

        db_path, artifacts_root, ticket_dir = _ticket_flow_paths(engine)

        exit_code_holder = [0]
        _repo_root = engine.repo_root
        _artifacts_root = artifacts_root
        shutdown_event = threading.Event()

        def _write_exit_info(*, shutdown_intent: bool = False) -> None:
            try:
                write_worker_exit_info(
                    _repo_root,
                    worker_run_id,
                    returncode=exit_code_holder[0] or None,
                    shutdown_intent=shutdown_intent,
                    artifacts_root=_artifacts_root,
                )
            except OSError:
                logger.debug("Failed to write worker exit info", exc_info=True)

        def _signal_handler(signum: int, _frame) -> None:
            exit_code_holder[0] = -signum
            shutdown_event.set()

        atexit.register(_write_exit_info)
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        async def _run_worker():
            typer.echo(
                f"worker: run={worker_run_id} db={db_path} artifacts={artifacts_root}"
            )

            store = FlowStore(db_path, durable=engine.config.durable_writes)
            store.initialize()

            record = store.get_flow_run(worker_run_id)
            if not record:
                typer.echo(f"Flow run {worker_run_id} not found", err=True)
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
                    worker_run_id,
                    artifacts_root=artifacts_root,
                )
            except (OSError, RuntimeError) as exc:
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
                    return build_ticket_flow_definition(
                        agent_pool=agent_pool,
                        auto_commit_default=engine.config.git_auto_commit,
                        include_previous_ticket_context_default=(
                            engine.config.ticket_flow.include_previous_ticket_context
                        ),
                        max_total_turns_default=(
                            engine.config.ticket_flow.max_total_turns
                            or DEFAULT_MAX_TOTAL_TURNS
                        ),
                    )
                raise_exit(f"Unknown flow type for run {worker_run_id}: {flow_type}")
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
                record = controller.get_status(worker_run_id)
                if not record:
                    typer.echo(f"Flow run {worker_run_id} not found", err=True)
                    raise typer.Exit(code=1)

                if record.status.is_terminal() and record.status not in {
                    FlowRunStatus.STOPPED,
                    FlowRunStatus.FAILED,
                }:
                    typer.echo(
                        f"Flow run {worker_run_id} already completed (status={record.status})"
                    )
                    return

                action = (
                    "Resuming" if record.status != FlowRunStatus.PENDING else "Starting"
                )
                typer.echo(
                    f"{action} flow run {worker_run_id} from step: {record.current_step}"
                )

                run_task = asyncio.create_task(controller.run_flow(worker_run_id))
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
                    await controller.stop_flow(worker_run_id)
                    run_task.cancel()

                if run_task.done():
                    final_record = await run_task
                    typer.echo(
                        f"Flow run {worker_run_id} finished with status {final_record.status}"
                    )
                else:
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        typer.echo(f"Flow run {worker_run_id} cancelled by signal")
                if not shutdown_wait_task.done():
                    shutdown_wait_task.cancel()
            except (
                Exception
            ) as exc:  # intentional: top-level worker crash handler; re-raises after logging
                exit_code_holder[0] = 1
                last_event = None
                try:
                    app_event = controller.store.get_last_telemetry_by_type(
                        worker_run_id, FlowEventType.APP_SERVER_EVENT
                    )
                    if app_event and isinstance(app_event.data, dict):
                        msg = app_event.data.get("message")
                        if isinstance(msg, dict):
                            method = msg.get("method")
                            if isinstance(method, str) and method.strip():
                                last_event = method.strip()
                except (
                    Exception
                ):  # intentional: best-effort diagnostic during crash handling
                    last_event = None
                write_worker_crash_info(
                    engine.repo_root,
                    worker_run_id,
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
                    except (
                        Exception
                    ):  # intentional: best-effort cleanup; must not mask the original error
                        typer.echo("Failed to close agent pool cleanly", err=True)
                _write_exit_info()

        asyncio.run(_run_worker())

    @ticket_flow_app.command("bootstrap")
    def ticket_flow_bootstrap(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        force_new: bool = typer.Option(
            False, "--force-new", help="Always create a new run"
        ),
        max_total_turns: Optional[int] = typer.Option(
            None,
            "--max-total-turns",
            min=1,
            help="Maximum total agent turns before pausing the run.",
        ),
    ):
        """Bootstrap ticket_flow (seed TICKET-001 if needed) and start a run.

        Breadcrumbs:
        - Inspect all ticket_flow commands: `car ticket-flow --help`
        - Check run health: `car ticket-flow status --run-id <run_id>`
        """
        engine = require_repo_config(repo, hub)
        guard_unregistered_hub_repo(engine.repo_root, hub)
        _, _, ticket_dir = _ticket_flow_paths(engine)
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"

        service = _ticket_flow_orchestration_service(engine)
        records = [
            _flow_run_record_from_target(target) for target in service.list_flow_runs()
        ]
        stale_terminal = _stale_terminal_runs(records)
        if not force_new:
            existing_run, reason = _resumable_run(records)
            if existing_run and reason == "active":
                _start_ticket_flow_worker(
                    engine.repo_root, existing_run.id, is_terminal=False
                )
                typer.echo(
                    f"reused run={existing_run.id} | car ticket-flow status --run-id {existing_run.id}"
                )
                return
            elif existing_run and reason == "completed_pending":
                existing_tickets = list_ticket_paths(ticket_dir)
                pending_count = len(
                    [t for t in existing_tickets if not ticket_is_done(t)]
                )
                if pending_count > 0:
                    typer.echo(
                        f"run {existing_run.id} completed with {pending_count} pending tickets. "
                        f"use --force-new to reset dispatch history."
                    )
                    raise_exit("Add --force-new to create a new run.")

        if stale_terminal:
            stale_id = stale_terminal[0].id
            typer.echo(
                f"warning: {len(stale_terminal)} stale runs (FAILED/STOPPED). "
                f"inspect: car ticket-flow status --run-id {stale_id}. "
                f"archive: car ticket-flow archive --run-id {stale_id} --force. "
                f"use --force-new to suppress."
            )

        existing_tickets = list_ticket_paths(ticket_dir)
        seeded = False
        if not existing_tickets and not ticket_path.exists():
            bootstrap_ticket_id = generate_ticket_id()
            template = f"""---
agent: codex
done: false
ticket_id: "{bootstrap_ticket_id}"
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

        run_id = str(uuid.uuid4())
        input_data: dict[str, object] = {}
        if max_total_turns is not None:
            input_data["max_total_turns"] = max_total_turns
        asyncio.run(
            service.start_flow_run(
                "ticket_flow",
                input_data=input_data,
                run_id=run_id,
                metadata={"seeded_ticket": seeded},
            )
        )

        typer.echo(f"run={run_id} | car ticket-flow status --run-id {run_id}")

    @ticket_flow_app.command("preflight")
    def ticket_flow_preflight(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
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
        hub: Optional[Path] = hub_root_path_option(),
        force_new: bool = typer.Option(
            False, "--force-new", help="Always create a new run"
        ),
        max_total_turns: Optional[int] = typer.Option(
            None,
            "--max-total-turns",
            min=1,
            help="Maximum total agent turns before pausing the run.",
        ),
    ):
        """Start or resume the latest ticket_flow run.

        Breadcrumbs:
        - Run preflight checks first: `car ticket-flow preflight`
        - Inspect run details: `car ticket-flow status --run-id <run_id>`
        """
        engine = require_repo_config(repo, hub)
        guard_unregistered_hub_repo(engine.repo_root, hub)
        _, _, ticket_dir = _ticket_flow_paths(engine)
        ticket_dir.mkdir(parents=True, exist_ok=True)

        service = _ticket_flow_orchestration_service(engine)
        records = [
            _flow_run_record_from_target(target) for target in service.list_flow_runs()
        ]
        stale_terminal = _stale_terminal_runs(records)
        if not force_new:
            existing_run, reason = _resumable_run(records)
            if existing_run and reason == "active":
                report = _ticket_flow_preflight(engine, ticket_dir)
                if report.has_errors():
                    typer.echo("Ticket flow preflight failed:", err=True)
                    _print_preflight_report(report)
                    raise_exit("Fix the above errors before starting the ticket flow.")
                _start_ticket_flow_worker(
                    engine.repo_root, existing_run.id, is_terminal=False
                )
                typer.echo(
                    f"reused run={existing_run.id} | car ticket-flow status --run-id {existing_run.id}"
                )
                return
            elif existing_run and reason == "completed_pending":
                existing_tickets = list_ticket_paths(ticket_dir)
                pending_count = len(
                    [t for t in existing_tickets if not ticket_is_done(t)]
                )
                if pending_count > 0:
                    typer.echo(
                        f"run {existing_run.id} completed with {pending_count} pending tickets. "
                        f"use --force-new to reset dispatch history."
                    )
                    raise_exit("Add --force-new to create a new run.")

        if stale_terminal:
            stale_id = stale_terminal[0].id
            typer.echo(
                f"warning: {len(stale_terminal)} stale runs (FAILED/STOPPED). "
                f"inspect: car ticket-flow status --run-id {stale_id}. "
                f"archive: car ticket-flow archive --run-id {stale_id} --force. "
                f"use --force-new to suppress."
            )

        report = _ticket_flow_preflight(engine, ticket_dir)
        if report.has_errors():
            typer.echo("Ticket flow preflight failed:", err=True)
            _print_preflight_report(report)
            raise_exit("Fix the above errors before starting the ticket flow.")

        run_id = str(uuid.uuid4())
        input_data: dict[str, object] = {"workspace_root": str(engine.repo_root)}
        if max_total_turns is not None:
            input_data["max_total_turns"] = max_total_turns
        asyncio.run(
            service.start_flow_run(
                "ticket_flow",
                input_data=input_data,
                run_id=run_id,
            )
        )

        typer.echo(f"run={run_id} | car ticket-flow status --run-id {run_id}")

    @ticket_flow_app.command("status")
    def ticket_flow_status(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Show status for a ticket_flow run."""
        engine = require_repo_config(repo, hub)
        normalized_run_id = _normalize_flow_run_id(run_id)
        service = _ticket_flow_orchestration_service(engine)

        target: Optional[FlowRunTarget] = None
        if normalized_run_id:
            target = service.get_flow_run(normalized_run_id)
        else:
            runs = service.list_flow_runs()
            target = runs[0] if runs else None
        if not target:
            raise_exit("No ticket_flow runs found.")
        assert target is not None
        normalized_run_id = target.run_id

        _, _, ticket_dir = _ticket_flow_paths(engine)
        report = _ticket_flow_preflight(engine, ticket_dir)
        if report.has_errors():
            typer.echo("Ticket flow preflight failed:", err=True)
            _print_preflight_report(report)
            raise_exit("Fix the above errors before resuming the ticket flow.")

        assert normalized_run_id is not None
        store = _open_flow_store(engine)
        try:
            record = store.get_flow_run(normalized_run_id)
            if not record:
                record = _flow_run_record_from_target(target)
            payload = _ticket_flow_status_payload(engine, record, store)
        finally:
            store.close()

        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        _print_ticket_flow_status(payload)

    @ticket_flow_app.command("stop")
    def ticket_flow_stop(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
    ):
        """Stop a ticket_flow run."""
        engine = require_repo_config(repo, hub)
        normalized_run_id = _normalize_flow_run_id(run_id)
        service = _ticket_flow_orchestration_service(engine)

        if normalized_run_id:
            target = service.get_flow_run(normalized_run_id)
        else:
            runs = service.list_flow_runs()
            target = runs[0] if runs else None
        if not target:
            raise_exit("No ticket_flow runs found.")
        normalized_run_id = target.run_id

        _stop_ticket_flow_worker(engine.repo_root, normalized_run_id)
        updated = asyncio.run(service.stop_flow_run(normalized_run_id))

        typer.echo(
            f"Stop requested for run: {updated.run_id} (status={updated.status})"
        )

    @ticket_flow_app.command("archive")
    def ticket_flow_archive(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
        force: bool = typer.Option(
            False, "--force", help="Allow archiving paused/stopping runs"
        ),
        force_attestation: Optional[str] = typer.Option(
            None,
            "--force-attestation",
            help="Attestation text required with --force for dangerous actions.",
        ),
        delete_run: str = typer.Option(
            "true",
            "--delete-run",
            help="Delete flow run record after archive (true|false)",
        ),
        dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Archive run artifacts and optionally delete the flow run record.

        Safety:
        Use `--dry-run` before archiving paused/stopping runs with `--force` or
        deleting run records with `--delete-run true`.
        """
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
                archive_kwargs = {
                    "repo_root": engine.repo_root,
                    "store": store,
                    "record": record,
                    "force": force,
                    "delete_run": parsed_delete_run,
                    "dry_run": dry_run,
                }
                force_attestation_payload: Optional[dict[str, str]] = None
                if force:
                    force_attestation_payload = _build_force_attestation(
                        force_attestation,
                        target_scope=f"flow.ticket_flow.archive:{run_id_str}",
                    )
                    validate_force_attestation(force_attestation_payload)
                    archive_kwargs["force_attestation"] = force_attestation_payload
                summary = archive_flow_run_artifacts(**archive_kwargs)
            except ValueError as exc:
                raise_exit(str(exc), cause=exc)
        finally:
            store.close()

        if output_json:
            typer.echo(json.dumps(summary, indent=2))
            return
        typer.echo(
            f"Archived run {summary.get('run_id')} status={summary.get('status')} "
            f"archived_tickets={summary.get('archived_tickets')} "
            f"archived_runs={summary.get('archived_runs')} "
            f"archived_contextspace={summary.get('archived_contextspace')} "
            f"deleted_run={summary.get('deleted_run')} dry_run={dry_run}"
        )

    def ticket_flow_preflight_report(
        engine: RuntimeContext, ticket_dir: Path
    ) -> PreflightReport:
        return _ticket_flow_preflight(engine, ticket_dir)

    @telemetry_app.command("export")
    def telemetry_export(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Preview what would be exported/pruned"
        ),
        run_id: Optional[str] = typer.Option(
            None, "--run-id", help="Export a specific run (default: all terminal runs)"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Export wire telemetry from flow_events for terminal runs.

        Writes per-run JSONL.GZ archives under .codex-autorunner/flows/{run_id}/
        and prunes redundant rows from the database. Use --dry-run to preview.
        """
        engine = require_repo_config(repo, hub)
        db_path = engine.repo_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            raise_exit("Flow database not found at .codex-autorunner/flows.db")

        store = FlowStore(db_path, durable=engine.config.durable_writes)
        try:
            store.initialize()
            if dry_run:
                run_ids = [run_id] if run_id else None
                result = export_all_runs(
                    engine.repo_root,
                    store,
                    dry_run=True,
                    run_ids=run_ids,
                )
                payload: dict[str, Any] = {
                    "dry_run": True,
                    **result.dry_run_summary(),
                }
                if output_json:
                    typer.echo(json.dumps(payload, indent=2))
                else:
                    typer.echo(
                        f"dry-run: runs={payload['runs_total']} skipped={payload['runs_skipped']} "
                        f"export={payload['events_to_export']} prune={payload['events_to_prune']} "
                        f"retain={payload['events_to_retain']} size={payload['estimated_bytes']:,}"
                    )
                return

            run_ids = [run_id] if run_id else None
            result = export_all_runs(
                engine.repo_root, store, dry_run=False, run_ids=run_ids
            )
            export_payload: dict[str, Any] = {
                "dry_run": False,
                "runs_exported": sum(1 for r in result.records if not r.skipped),
                "runs_skipped": sum(1 for r in result.records if r.skipped),
                "total_exported_events": result.total_exported_events,
                "total_pruned_events": result.total_pruned_events,
                "total_exported_bytes": result.total_exported_bytes,
                "archive_files": result.archive_files,
                "errors": result.errors,
                "run_details": [
                    {
                        "run_id": r.run_id,
                        "run_status": r.run_status,
                        "skipped": r.skipped,
                        "skip_reason": r.skip_reason,
                        "exported_events": r.exported_events,
                        "exported_bytes": r.exported_bytes,
                        "pruned_app_server_events": r.prunable_app_server_events,
                        "pruned_stream_deltas": r.prunable_stream_deltas,
                        "retained_events": r.retained_events,
                        "archive_path": r.archive_path,
                    }
                    for r in result.records
                ],
            }
            if output_json:
                typer.echo(json.dumps(export_payload, indent=2))
            else:
                typer.echo(
                    f"exported {export_payload['runs_exported']} runs "
                    f"{export_payload['total_exported_events']} events "
                    f"{export_payload['total_exported_bytes']:,} bytes "
                    f"pruned {export_payload['total_pruned_events']}"
                )
                for r in result.records:
                    if r.skipped:
                        typer.echo(
                            f"  skipped {r.run_id} ({r.run_status}): {r.skip_reason}"
                        )
                    else:
                        typer.echo(
                            f"  {r.run_id}: exported={r.exported_events} "
                            f"pruned={r.prunable_app_server_events + r.prunable_stream_deltas} "
                            f"retained={r.retained_events}"
                        )
                if result.errors:
                    for err in result.errors:
                        typer.echo(f"  error: {err}", err=True)
        finally:
            store.close()

    @flow_app.command("housekeep")
    def flow_housekeep(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        stats_only: bool = typer.Option(
            False, "--stats", help="Report DB stats only (no export/prune)"
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Preview export/prune without mutating"
        ),
        retention: Optional[str] = typer.Option(
            None,
            "--retention",
            help="Override retention window (e.g. 7d, 14d). Default: 7d.",
        ),
        run_id: Optional[str] = typer.Option(
            None, "--run-id", help="Target a specific run (default: all expired runs)"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Export and prune expired flow telemetry to manage DB size.

        Modes:
          --stats     Report DB/run statistics without mutating anything.
          --dry-run   Preview which runs would be exported and pruned.
          (default)   Execute export and prune for expired terminal runs.
          VACUUM runs automatically when pruning deletes rows.

        Expired means a terminal run whose finished_at is older than the
        retention window (default 7d, overridable via --retention or config).
        Active runs are never touched.
        """
        engine = require_repo_config(repo, hub)
        db_path = engine.repo_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            raise_exit("Flow database not found at .codex-autorunner/flows.db")

        configured_retention = getattr(engine.config, "flow_retention", None)
        if isinstance(configured_retention, FlowRetentionConfig):
            retention_config = configured_retention
        else:
            retention_config = parse_flow_retention_config(configured_retention)
        if retention is not None:
            from datetime import timedelta

            td = parse_duration(retention)
            assert isinstance(td, timedelta)
            total_seconds = int(td.total_seconds())
            if total_seconds <= 0 or total_seconds % 86400 != 0:
                raise_exit(
                    "--retention must be a positive whole-day duration such as 7d or 14d."
                )
            retention_config = FlowRetentionConfig(
                retention_days=total_seconds // 86400,
                sweep_interval_seconds=retention_config.sweep_interval_seconds,
            )

        store = FlowStore(db_path, durable=engine.config.durable_writes)
        try:
            store.initialize()

            if stats_only:
                hk_stats = gather_stats(store, db_path, retention_config)
                payload: dict[str, Any] = {
                    "db_path": hk_stats.db_path,
                    "db_size_bytes": hk_stats.db_size_bytes,
                    "runs_total": hk_stats.runs_total,
                    "runs_active": hk_stats.runs_active,
                    "runs_terminal": hk_stats.runs_terminal,
                    "runs_expired": hk_stats.runs_expired,
                    "events_total": hk_stats.events_total,
                    "telemetry_total": hk_stats.telemetry_total,
                    "wire_events_total": hk_stats.wire_events_total,
                    "retention_days": retention_config.retention_days,
                    "run_details": [
                        {
                            "run_id": r.run_id,
                            "run_status": r.run_status,
                            "flow_type": r.flow_type,
                            "created_at": r.created_at,
                            "finished_at": r.finished_at,
                            "is_active": r.is_active,
                            "is_terminal": r.is_terminal,
                            "is_expired": r.is_expired,
                            "events_total": r.events_total,
                            "telemetry_total": r.telemetry_total,
                            "wire_events": r.wire_events,
                        }
                        for r in hk_stats.run_details
                    ],
                }
                if output_json:
                    typer.echo(json.dumps(payload, indent=2))
                else:
                    typer.echo(
                        f"db={hk_stats.db_path} size={hk_stats.db_size_bytes:,} "
                        f"runs={hk_stats.runs_total}(active={hk_stats.runs_active},"
                        f"terminal={hk_stats.runs_terminal},expired={hk_stats.runs_expired}) "
                        f"events={hk_stats.events_total}(telemetry={hk_stats.telemetry_total},wire={hk_stats.wire_events_total}) "
                        f"retention={retention_config.retention_days}d"
                    )
                    for r in hk_stats.run_details:
                        flags = []
                        if r.is_active:
                            flags.append("active")
                        if r.is_terminal:
                            flags.append("terminal")
                        if r.is_expired:
                            flags.append("expired")
                        flag_str = ",".join(flags) if flags else "other"
                        typer.echo(
                            f"  {r.run_id}: {r.run_status} [{flag_str}] "
                            f"events={r.events_total} telemetry={r.telemetry_total} wire={r.wire_events}"
                        )
                return

            target_run_ids = [run_id] if run_id else None

            if dry_run:
                hk_plan = build_plan(
                    store, db_path, retention_config, run_ids=target_run_ids
                )
                plan_payload: dict[str, Any] = {
                    "dry_run": True,
                    "retention_days": retention_config.retention_days,
                    "runs_to_process": len(hk_plan.runs_to_process),
                    "runs_skipped_active": hk_plan.runs_skipped_active,
                    "runs_skipped_not_expired": hk_plan.runs_skipped_not_expired,
                    "events_to_export": hk_plan.events_to_export,
                    "events_to_prune": hk_plan.events_to_prune,
                    "estimated_export_bytes": hk_plan.estimated_export_bytes,
                    "db_size_bytes": hk_plan.stats.db_size_bytes,
                    "run_details": [
                        {
                            "run_id": r.run_id,
                            "run_status": r.run_status,
                            "finished_at": r.finished_at,
                            "events_total": r.events_total,
                            "wire_events": r.wire_events,
                        }
                        for r in hk_plan.runs_to_process
                    ],
                }
                if output_json:
                    typer.echo(json.dumps(plan_payload, indent=2))
                else:
                    typer.echo(
                        f"housekeep(dry-run) retention={retention_config.retention_days}d "
                        f"process={plan_payload['runs_to_process']} "
                        f"skip_active={plan_payload['runs_skipped_active']} "
                        f"skip_not_expired={plan_payload['runs_skipped_not_expired']} "
                        f"export={plan_payload['events_to_export']} prune={plan_payload['events_to_prune']} "
                        f"size={plan_payload['estimated_export_bytes']:,} db={plan_payload['db_size_bytes']:,}"
                    )
                    for r in hk_plan.runs_to_process:
                        typer.echo(
                            f"  {r.run_id}: {r.run_status} finished={r.finished_at} "
                            f"events={r.events_total} wire={r.wire_events}"
                        )
                return

            hk_result = execute_housekeep(
                engine.repo_root,
                store,
                db_path,
                retention_config,
                run_ids=target_run_ids,
                dry_run=False,
            )
            result_payload = hk_result.to_dict()
            if output_json:
                typer.echo(json.dumps(result_payload, indent=2))
            else:
                typer.echo(
                    f"housekeep: {hk_result.runs_processed} runs "
                    f"exported={hk_result.events_exported}({hk_result.exported_bytes:,} bytes) "
                    f"pruned={hk_result.events_pruned}"
                )
                if hk_result.vacuum_performed:
                    typer.echo("  vacuum: performed")
                typer.echo(
                    f"  db: {hk_result.db_size_before_bytes:,} -> {hk_result.db_size_after_bytes:,}"
                )
                for err in hk_result.errors:
                    typer.echo(f"  error: {err}", err=True)
        finally:
            store.close()

    return FlowCommandExports(
        PreflightCheck=PreflightCheck,
        PreflightReport=PreflightReport,
        ticket_flow_start=ticket_flow_start,
        ticket_flow_preflight=ticket_flow_preflight,
        _ticket_flow_preflight=_ticket_flow_preflight,
        ticket_flow_preflight_report=ticket_flow_preflight_report,
        ticket_flow_print_preflight_report=_print_preflight_report,
        ticket_flow_resumable_run=_resumable_run,
    )
