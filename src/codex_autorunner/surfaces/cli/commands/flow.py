"""Flow-related CLI command extraction from the monolithic cli surface."""

import asyncio
import atexit
import json
import logging
import os
import signal
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, cast

import typer

from ....core.config import find_nearest_hub_config_path
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
    parse_flow_timestamp,
)
from ....core.flows.telemetry_export import export_all_runs
from ....core.flows.worker_health_policy import (
    AppServerStatus,
    WorkerHealthAction,
    build_worker_health_snapshot,
    decide_worker_health,
)
from ....core.flows.worker_process import (
    register_worker_metadata,
    write_worker_crash_info,
    write_worker_exit_info,
)
from ....core.managed_processes import reap_managed_processes
from ....core.orchestration import build_ticket_flow_orchestration_service
from ....core.orchestration.ticket_flow_visibility_repair import (
    repair_ticket_flow_chat_visibility,
)
from ....core.runtime import RuntimeContext
from ....core.state import load_state
from ....core.state_roots import resolve_repo_flows_db_path
from ....core.ticket_flow_operator import (
    PreflightCheck,
    PreflightReport,
    select_resumable_run,
)
from ....core.ticket_flow_operator import (
    ticket_flow_preflight as shared_ticket_flow_preflight,
)
from ....core.utils import resolve_executable  # noqa: F401
from ....flows.ticket_flow.runtime_helpers import flow_run_record_from_target
from ....tickets import DEFAULT_MAX_TOTAL_TURNS, AgentPool
from ..hub_path_option import hub_root_path_option
from ..ops_cleanup import (
    FlowHousekeepPlan,
    FlowHousekeepResult,
    render_flow_housekeep_human,
    render_flow_housekeep_json,
)
from .ticket_flow_controller import (
    TicketFlowCliController,
    normalize_flow_run_id,
    resolve_ticket_flow_paths,
)
from .ticket_flow_status import (
    build_ticket_flow_status_payload,
    render_preflight_report_lines,
    render_ticket_flow_status_lines,
)

logger = logging.getLogger(__name__)

_DEFAULT_FLOW_WORKER_MAX_WALL_SECONDS = 2 * 60 * 60
_FLOW_WORKER_WATCHDOG_GRACE_SECONDS = 30.0
_FLOW_WORKER_WATCHDOG_POLL_SECONDS = 2.0
_OPENCODE_STALL_TIMEOUT_METHOD = "opencode.stream.stalled.timeout"


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


def _resolve_ticket_flow_repair_hub_root(repo_root: Path, hub: Optional[Path]) -> Path:
    if hub is not None:
        return hub.expanduser().resolve()
    hub_config_path = find_nearest_hub_config_path(repo_root)
    if hub_config_path is not None:
        return hub_config_path.parent.parent.resolve()
    return repo_root.resolve()


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
        try:
            return normalize_flow_run_id(run_id)
        except ValueError:
            raise_exit("Invalid run_id format; must be a UUID")
        raise AssertionError("Unreachable")  # satisfies mypy return

    def _ticket_flow_paths(engine: RuntimeContext) -> tuple[Path, Path, Path]:
        paths = resolve_ticket_flow_paths(engine)
        return paths.db_path, paths.artifacts_root, paths.ticket_dir

    def _print_preflight_report(report: PreflightReport) -> None:
        for line in render_preflight_report_lines(report):
            typer.echo(line)

    def _ticket_flow_preflight(
        engine: RuntimeContext, ticket_dir: Path
    ) -> PreflightReport:
        _ = ticket_dir
        return shared_ticket_flow_preflight(engine.repo_root, config=engine.config)

    def _ticket_flow_status_payload(
        engine: RuntimeContext, record: FlowRunRecord, store: Optional[FlowStore]
    ) -> dict:
        return build_ticket_flow_status_payload(engine, record, store)

    def _print_ticket_flow_status(payload: dict) -> None:
        for line in render_ticket_flow_status_lines(payload):
            typer.echo(line)

    def _ticket_flow_controller(
        engine: RuntimeContext,
    ) -> tuple[FlowController, AgentPool]:
        from ....flows.ticket_flow.runtime_helpers import (
            build_ticket_flow_runtime_resources,
        )

        resources = build_ticket_flow_runtime_resources(engine.repo_root)
        return resources.controller, resources.agent_pool

    def _ticket_flow_orchestration_service(engine: RuntimeContext):
        return build_ticket_flow_orchestration_service(workspace_root=engine.repo_root)

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
        watchdog_reason: dict[str, Optional[str]] = {"value": None}
        watchdog_app_server_floor_seq: dict[str, Optional[int]] = {"value": None}
        shutdown_signal: dict[str, Optional[str]] = {"value": None}
        shutdown_exit_origin: dict[str, Optional[str]] = {"value": None}
        shutdown_exit_kind: dict[str, Optional[str]] = {"value": None}

        def _write_exit_info(*, shutdown_intent: bool = False) -> None:
            try:
                write_worker_exit_info(
                    _repo_root,
                    worker_run_id,
                    returncode=exit_code_holder[0] or None,
                    shutdown_intent=shutdown_intent,
                    signal=shutdown_signal["value"],
                    exit_origin=shutdown_exit_origin["value"],
                    exit_kind=shutdown_exit_kind["value"],
                    artifacts_root=_artifacts_root,
                )
            except OSError:
                logger.debug("Failed to write worker exit info", exc_info=True)

        def _signal_handler(signum: int, _frame) -> None:
            exit_code_holder[0] = -signum
            signal_enum = getattr(signal, "Signals", None)
            if signal_enum is not None:
                try:
                    shutdown_signal["value"] = signal_enum(signum).name
                except ValueError:
                    shutdown_signal["value"] = f"signal-{signum}"
            else:
                shutdown_signal["value"] = f"signal-{signum}"
            shutdown_exit_origin["value"] = "worker_signal"
            shutdown_exit_kind["value"] = "external_signal"
            shutdown_event.set()

        def _resolve_worker_max_wall_seconds() -> float:
            raw = os.environ.get("CAR_FLOW_WORKER_MAX_WALL_SECONDS")
            if raw is None or not raw.strip():
                return float(_DEFAULT_FLOW_WORKER_MAX_WALL_SECONDS)
            try:
                value = float(raw)
            except ValueError:
                return float(_DEFAULT_FLOW_WORKER_MAX_WALL_SECONDS)
            return max(1.0, value)

        def _extract_app_server_method_from_event(event: Any) -> Optional[str]:
            if not isinstance(event.data, dict):
                return None
            message = event.data.get("message")
            if isinstance(message, dict):
                method = message.get("method")
                return method if isinstance(method, str) else None
            method = event.data.get("method")
            return method if isinstance(method, str) else None

        def _max_timestamp(*values: Optional[str]) -> Optional[str]:
            parsed: list[datetime] = []
            for value in values:
                dt = parse_flow_timestamp(value)
                if dt is not None:
                    parsed.append(dt.astimezone(timezone.utc))
            if not parsed:
                return None
            return max(parsed).isoformat()

        def _current_ticket_from_record(
            record: Optional[FlowRunRecord],
        ) -> Optional[str]:
            if record is None or not isinstance(record.state, dict):
                return None
            ticket_engine = record.state.get("ticket_engine")
            if not isinstance(ticket_engine, dict):
                return None
            ticket = ticket_engine.get("current_ticket")
            return (
                ticket.strip() if isinstance(ticket, str) and ticket.strip() else None
            )

        def _watchdog_snapshot_after_seq(
            min_seq_exclusive: int, *, worker_age_seconds: float
        ):
            try:
                with FlowStore.connect_readonly(db_path) as watchdog_store:
                    record = watchdog_store.get_flow_run(worker_run_id)
                    _event_seq, last_event_at = watchdog_store.get_last_event_meta(
                        worker_run_id
                    )
                    _telemetry_seq, last_telemetry_at = (
                        watchdog_store.get_last_telemetry_meta(worker_run_id)
                    )
                    app_event = watchdog_store.get_last_telemetry_by_type(
                        worker_run_id, FlowEventType.APP_SERVER_EVENT
                    )
            except (OSError, RuntimeError, ValueError):
                return (
                    build_worker_health_snapshot(
                        process_status="alive",
                        worker_age_seconds=worker_age_seconds,
                        app_server_status="unknown",
                    ),
                    None,
                )
            method = None
            if app_event is not None and app_event.seq > min_seq_exclusive:
                method = _extract_app_server_method_from_event(app_event)
            app_status: AppServerStatus = (
                "stalled_timeout"
                if method == _OPENCODE_STALL_TIMEOUT_METHOD
                else "connected"
            )
            return (
                build_worker_health_snapshot(
                    process_status="alive",
                    worker_age_seconds=worker_age_seconds,
                    last_activity_at=_max_timestamp(last_event_at, last_telemetry_at),
                    last_semantic_progress_at=last_event_at,
                    current_ticket=_current_ticket_from_record(record),
                    current_turn=record.current_step if record is not None else None,
                    app_server_status=app_status,
                    now=datetime.now(timezone.utc),
                ),
                method,
            )

        def _watchdog_loop() -> None:
            max_wall_seconds = _resolve_worker_max_wall_seconds()
            started_monotonic = time.monotonic()
            rotation_reported = False
            while not shutdown_event.wait(_FLOW_WORKER_WATCHDOG_POLL_SECONDS):
                floor = watchdog_app_server_floor_seq["value"]
                if floor is None:
                    continue
                snapshot, method = _watchdog_snapshot_after_seq(
                    floor,
                    worker_age_seconds=time.monotonic() - started_monotonic,
                )
                decision = decide_worker_health(
                    snapshot,
                    max_wall_seconds=max_wall_seconds,
                    idle_stale_seconds=None,
                )
                if decision.force_restart and method == _OPENCODE_STALL_TIMEOUT_METHOD:
                    watchdog_reason["value"] = method
                    exit_code_holder[0] = 1
                    shutdown_exit_origin["value"] = "worker_watchdog"
                    shutdown_exit_kind["value"] = decision.exit_kind
                    write_worker_crash_info(
                        _repo_root,
                        worker_run_id,
                        worker_pid=os.getpid(),
                        exit_code=1,
                        last_event=method,
                        exception="opencode stream stalled timeout",
                        artifacts_root=_artifacts_root,
                    )
                    _write_exit_info(shutdown_intent=True)
                    os._exit(1)
                if (
                    decision.action == WorkerHealthAction.ROTATE_REQUESTED
                    and not rotation_reported
                ):
                    rotation_reported = True
                    logger.info(
                        "flow worker exceeded wall-clock budget; requesting cooperative rotation "
                        "run_id=%s age=%.1fs last_activity_at=%s current_ticket=%s",
                        worker_run_id,
                        snapshot.worker_age_seconds or 0.0,
                        snapshot.last_activity_at,
                        snapshot.current_ticket,
                    )

        atexit.register(_write_exit_info)
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
        watchdog_thread = threading.Thread(
            target=_watchdog_loop,
            name="flow-worker-watchdog",
            daemon=True,
        )
        watchdog_thread.start()

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
                    state_path = getattr(engine, "state_path", None)
                    require_commit_default = (
                        load_state(state_path).ticket_flow_require_commit
                        if state_path is not None
                        else True
                    )
                    return build_ticket_flow_definition(
                        agent_pool=agent_pool,
                        auto_commit_default=engine.config.git_auto_commit,
                        require_commit_default=require_commit_default,
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
            try:
                with FlowStore.connect_readonly(db_path) as floor_store:
                    last_app = floor_store.get_last_telemetry_by_type(
                        worker_run_id, FlowEventType.APP_SERVER_EVENT
                    )
                    watchdog_app_server_floor_seq["value"] = (
                        last_app.seq if last_app is not None else 0
                    )
            except (OSError, RuntimeError, ValueError):
                watchdog_app_server_floor_seq["value"] = 0
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

                async def _wait_for_shutdown() -> None:
                    while not shutdown_event.is_set():
                        await asyncio.sleep(0.2)

                shutdown_wait_task = asyncio.create_task(_wait_for_shutdown())
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
                    try:
                        await shutdown_wait_task
                    except asyncio.CancelledError:
                        pass
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
        controller = TicketFlowCliController(
            engine, _ticket_flow_orchestration_service(engine)
        )
        action = controller.bootstrap(
            force_new=force_new, max_total_turns=max_total_turns
        )

        typer.echo(action.message)
        if action.should_exit:
            raise_exit(action.exit_message)

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
        controller = TicketFlowCliController(
            engine, _ticket_flow_orchestration_service(engine)
        )
        action = controller.start(
            force_new=force_new,
            max_total_turns=max_total_turns,
            preflight=lambda: _ticket_flow_preflight(
                engine, controller.paths.ticket_dir
            ),
        )

        typer.echo(action.message, err=action.preflight_report is not None)
        if action.preflight_report is not None:
            _print_preflight_report(action.preflight_report)
        if action.should_exit:
            raise_exit(action.exit_message)

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
        controller = TicketFlowCliController(
            engine, _ticket_flow_orchestration_service(engine)
        )
        target = controller.resolve_target_run(normalized_run_id)
        if not target:
            raise_exit("No ticket_flow runs found.")
        assert target is not None
        normalized_run_id = target.run_id

        report = _ticket_flow_preflight(engine, controller.paths.ticket_dir)
        if report.has_errors():
            typer.echo("Ticket flow preflight failed:", err=True)
            _print_preflight_report(report)
            raise_exit("Fix the above errors before resuming the ticket flow.")

        assert normalized_run_id is not None
        store = controller.open_flow_store()
        try:
            record = store.get_flow_run(normalized_run_id)
            if not record:
                record = flow_run_record_from_target(target)
            payload = _ticket_flow_status_payload(engine, record, store)
        finally:
            store.close()

        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        _print_ticket_flow_status(payload)

    @ticket_flow_app.command("repair-visibility")
    def ticket_flow_repair_visibility(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
        repo_id: Optional[str] = typer.Option(
            None,
            "--repo-id",
            help="Hub repo id to stamp on repaired managed-thread rows.",
        ),
        dry_run: bool = typer.Option(
            True,
            "--dry-run/--apply",
            help="Preview repairs by default; pass --apply to mutate hub records.",
        ),
        output_json: bool = typer.Option(True, "--json/--no-json", help="Emit JSON"),
    ):
        """Repair missing Web Hub chat visibility for legacy ticket_flow runs."""
        engine = require_repo_config(repo, hub)
        normalized_run_id = _normalize_flow_run_id(run_id)
        report = repair_ticket_flow_chat_visibility(
            repo_root=engine.repo_root,
            hub_root=_resolve_ticket_flow_repair_hub_root(engine.repo_root, hub),
            repo_id=repo_id,
            run_id=normalized_run_id,
            dry_run=dry_run,
            durable=engine.config.durable_writes,
        )
        payload = report.to_dict()
        if output_json:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
            return
        typer.echo(
            f"scanned={payload['scanned_runs']} repaired={payload['repaired']} "
            f"already_linked={payload['already_linked']} dry_run={payload['dry_run']}"
        )
        for diagnostic in payload["diagnostics"]:
            typer.echo(
                "diagnostic: "
                f"run={diagnostic['run_id']} status={diagnostic['status']} "
                f"reason={diagnostic['reason']}"
            )

    @ticket_flow_app.command("stop")
    def ticket_flow_stop(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
    ):
        """Stop a ticket_flow run."""
        engine = require_repo_config(repo, hub)
        normalized_run_id = _normalize_flow_run_id(run_id)
        controller = TicketFlowCliController(
            engine, _ticket_flow_orchestration_service(engine)
        )
        updated = controller.stop(normalized_run_id)
        if not updated:
            raise_exit("No ticket_flow runs found.")
        assert updated is not None

        typer.echo(
            f"Stop requested for run: {updated.run_id} (status={updated.status})"
        )

    @ticket_flow_app.command("retire")
    def ticket_flow_retire(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
        force: bool = typer.Option(
            False, "--force", help="Allow retiring paused/stopping runs"
        ),
        force_attestation: Optional[str] = typer.Option(
            None,
            "--force-attestation",
            help="Attestation text required with --force for dangerous actions.",
        ),
        delete_run: str = typer.Option(
            "true",
            "--delete-run",
            help="Delete flow run record after retiring (true|false)",
        ),
        no_vacuum: bool = typer.Option(
            False,
            "--no-vacuum",
            help="Skip VACUUM after deleting flow rows (WAL checkpoint still runs).",
        ),
        dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Retire a run by preserving artifacts and clearing active flow state.

        Safety:
        Use `--dry-run` before retiring paused/stopping runs with `--force` or
        deleting run records with `--delete-run true`.
        """
        engine = require_repo_config(repo, hub)
        normalized_run_id = _normalize_flow_run_id(run_id)
        if not normalized_run_id:
            raise_exit("--run-id is required")
        parsed_delete_run = parse_bool_text(delete_run, flag="--delete-run")
        run_id_str: str = normalized_run_id  # type: ignore[assignment]
        try:
            controller = TicketFlowCliController(
                engine, _ticket_flow_orchestration_service(engine)
            )
            summary = controller.retire(
                run_id=run_id_str,
                force=force,
                force_attestation=force_attestation,
                delete_run=parsed_delete_run,
                vacuum=not no_vacuum,
                dry_run=dry_run,
                archive_flow_run_artifacts=archive_flow_run_artifacts,
            )
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)

        if output_json:
            typer.echo(json.dumps(summary, indent=2))
            return
        typer.echo(
            f"Retired run {summary.get('run_id')} status={summary.get('status')} "
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
            None,
            "--run-id",
            help="Export a specific run (default: all non-active runs except paused)",
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Export wire telemetry from flow_events.

        Writes per-run JSONL.GZ archives under .codex-autorunner/flows/{run_id}/
        and prunes redundant rows from the database for eligible runs.
        Paused runs are omitted when exporting all runs; pass --run-id for a paused run.

        Use --dry-run to preview.
        """
        engine = require_repo_config(repo, hub)
        db_path = resolve_repo_flows_db_path(engine.repo_root)
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
        db_path = resolve_repo_flows_db_path(engine.repo_root)
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
                housekeep_plan = FlowHousekeepPlan(
                    mode="stats",
                    retention_days=retention_config.retention_days,
                    run_id=run_id,
                    output_json=output_json,
                )
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
                housekeep_result = FlowHousekeepResult(
                    plan=housekeep_plan,
                    payload=payload,
                )
                if output_json:
                    typer.echo(render_flow_housekeep_json(housekeep_result))
                else:
                    for line in render_flow_housekeep_human(housekeep_result):
                        typer.echo(line)
                return

            target_run_ids = [run_id] if run_id else None

            if dry_run:
                hk_plan = build_plan(
                    store, db_path, retention_config, run_ids=target_run_ids
                )
                housekeep_plan = FlowHousekeepPlan(
                    mode="dry_run",
                    retention_days=retention_config.retention_days,
                    run_id=run_id,
                    output_json=output_json,
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
                housekeep_result = FlowHousekeepResult(
                    plan=housekeep_plan,
                    payload=plan_payload,
                )
                if output_json:
                    typer.echo(render_flow_housekeep_json(housekeep_result))
                else:
                    for line in render_flow_housekeep_human(housekeep_result):
                        typer.echo(line)
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
            result_payload.setdefault("events_exported", hk_result.events_exported)
            result_payload.setdefault("events_pruned", hk_result.events_pruned)
            result_payload.setdefault("exported_bytes", hk_result.exported_bytes)
            result_payload.setdefault("vacuum_performed", hk_result.vacuum_performed)
            result_payload.setdefault(
                "db_size_before_bytes", hk_result.db_size_before_bytes
            )
            result_payload.setdefault(
                "db_size_after_bytes", hk_result.db_size_after_bytes
            )
            housekeep_result = FlowHousekeepResult(
                plan=FlowHousekeepPlan(
                    mode="execute",
                    retention_days=retention_config.retention_days,
                    run_id=run_id,
                    output_json=output_json,
                ),
                payload=result_payload,
                errors=tuple(hk_result.errors),
            )
            if output_json:
                typer.echo(render_flow_housekeep_json(housekeep_result))
            else:
                for line in render_flow_housekeep_human(housekeep_result):
                    typer.echo(line)
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
        ticket_flow_resumable_run=select_resumable_run,
    )
