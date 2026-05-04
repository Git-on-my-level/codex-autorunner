import asyncio
import html as html_lib
import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Protocol, cast
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp

from ...core.config import parse_flow_retention_config
from ...core.diagnostics import (
    DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS,
    DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS,
    ProcessMonitorStore,
    capture_process_monitor_sample,
)
from ...core.filebox_retention import (
    prune_filebox_root,
    resolve_filebox_retention_policy,
)
from ...core.hub_diagnostics import (
    install_hub_exception_hooks,
    record_hub_clean_shutdown,
    record_hub_startup,
)
from ...core.logging_utils import safe_log
from ...core.managed_processes import reap_managed_processes
from ...core.orchestration.execution_history_maintenance import (
    resolve_execution_history_maintenance_policy,
    run_execution_history_housekeeping_once,
)
from ...core.pma_domain.constants import DEFAULT_PMA_LANE_ID
from ...core.pma_queue import PmaQueue, QueueItemState
from ...housekeeping import (
    DEFAULT_FLOW_WORKER_REAP_INTERVAL_SECONDS,
    reap_managed_docker_containers,
    reap_stale_flow_workers,
    run_housekeeping_once,
)
from .app_builders import create_app, create_repo_app
from .app_factory import CacheStaticFiles, resolve_allowed_hosts, resolve_auth_token
from .app_state import (
    ServerOverrides,
    apply_hub_context,
    build_app_context,
    build_hub_context,
)
from .hub_routes import register_simple_hub_routes
from .middleware import (
    AuthTokenMiddleware,
    BasePathRouterMiddleware,
    HostOriginMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from .routes.contextspace import build_contextspace_routes
from .routes.feedback_reports import build_feedback_report_routes
from .routes.filebox import build_hub_filebox_routes
from .routes.flows import build_flow_routes
from .routes.hub_control_plane import build_hub_control_plane_routes
from .routes.hub_messages import build_hub_messages_routes
from .routes.hub_repos import HubMountManager, build_hub_repo_routes
from .routes.pma import build_pma_routes
from .routes.pma_routes import PmaRuntimeState
from .routes.pma_routes.managed_thread_runtime import (
    recover_orphaned_managed_thread_executions,
    restart_managed_thread_queue_workers,
)
from .routes.scm_webhooks import build_scm_webhook_routes
from .routes.settings import build_settings_routes
from .routes.system import build_system_routes
from .services.pma import create_pma_application_container
from .static_assets import (
    index_response_headers,
    pma_index_response_headers,
    render_index_html,
    render_pma_index_html,
    resolve_pma_static_dir,
)

__all__ = ["create_app", "create_hub_app", "create_repo_app"]

_DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS = float(
    parse_flow_retention_config(None).sweep_interval_seconds
)
_LEGACY_REPO_FRONTEND_TABS = {
    "workspace",
    "tickets",
    "messages",
    "inbox",
    "contextspace",
    "archive",
    "analytics",
    "terminal",
    "settings",
}


class _IdlePrunable(Protocol):
    async def prune_idle(self) -> None: ...


async def _start_replayable_pma_lane_workers(
    app: FastAPI,
    starter: object,
) -> list[str]:
    if not callable(starter):
        return []

    queue = PmaQueue(Path(app.state.config.root))
    lanes = [DEFAULT_PMA_LANE_ID]
    seen = {DEFAULT_PMA_LANE_ID}
    for lane_id in await queue.get_all_lanes():
        if lane_id in seen:
            continue
        items = await queue.list_items(lane_id)
        if any(
            item.state in {QueueItemState.PENDING, QueueItemState.RUNNING}
            for item in items
        ):
            lanes.append(lane_id)
            seen.add(lane_id)

    started: list[str] = []
    for lane_id in lanes:
        await starter(app, lane_id)
        started.append(lane_id)
    return started


def _resolve_hub_flow_sweep_interval_seconds(
    repo_defaults: object, logger: logging.Logger
) -> float:
    if not isinstance(repo_defaults, dict):
        return _DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS
    flow_retention = repo_defaults.get("flow_retention")
    if flow_retention is None:
        return _DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS
    if not isinstance(flow_retention, dict):
        logger.warning(
            "Ignoring invalid hub repo_defaults.flow_retention=%r; using default flow sweep interval %s",
            flow_retention,
            int(_DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS),
        )
        return _DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS
    sweep_interval_seconds = flow_retention.get("sweep_interval_seconds")
    if sweep_interval_seconds is None:
        return _DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS
    if (
        not isinstance(sweep_interval_seconds, int)
        or isinstance(sweep_interval_seconds, bool)
        or sweep_interval_seconds <= 0
    ):
        logger.warning(
            "Ignoring invalid hub repo_defaults.flow_retention.sweep_interval_seconds=%r; using default %s",
            sweep_interval_seconds,
            int(_DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS),
        )
        return _DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS
    return float(sweep_interval_seconds)


async def _run_prune_loop(
    *,
    interval_seconds: float,
    supervisor: _IdlePrunable,
    logger: logging.Logger,
    failure_message: str,
) -> None:
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await supervisor.prune_idle()
            except (
                RuntimeError,
                OSError,
                ConnectionError,
                ValueError,
                TypeError,
            ) as exc:  # intentional: background loop must not crash
                safe_log(logger, logging.WARNING, failure_message, exc)
    except asyncio.CancelledError:
        return


def _record_process_monitor_sample(root: Path) -> None:
    store = ProcessMonitorStore(root)
    store.record_sample(
        capture_process_monitor_sample(root),
        cadence_seconds=DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS,
        window_seconds=DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS,
    )


def create_hub_app(
    hub_root: Optional[Path] = None,
    base_path: Optional[str] = None,
    endpoint_host: Optional[str] = None,
    endpoint_port: Optional[int] = None,
) -> ASGIApp:
    context = build_hub_context(hub_root, base_path)
    app = FastAPI(redirect_slashes=False)
    apply_hub_context(app, context)
    repo_context = build_app_context(
        context.config.root, context.base_path, context.config
    )
    app.state.engine = repo_context.engine
    app.state.manager = repo_context.manager
    app.state.app_server_threads = repo_context.app_server_threads
    app.add_middleware(GZipMiddleware, minimum_size=500)
    static_files = CacheStaticFiles(directory=context.static_dir)
    app.state.static_files = static_files
    app.state.static_assets_lock = threading.Lock()
    app.state.hub_static_assets = None
    app.state.orchestration_housekeeping = None
    app.mount("/static", static_files, name="static")
    pma_static_dir, pma_static_context = resolve_pma_static_dir()
    app.state.pma_static_dir = pma_static_dir
    app.state.pma_static_assets_context = pma_static_context
    pma_app_assets_dir = pma_static_dir / "_app"
    if pma_app_assets_dir.exists():
        app.mount(
            "/_app",
            CacheStaticFiles(directory=pma_app_assets_dir),
            name="pma-app-assets",
        )
    raw_config = getattr(context.config, "raw", {})
    pma_config = raw_config.get("pma", {}) if isinstance(raw_config, dict) else {}
    if isinstance(pma_config, dict) and pma_config.get("enabled"):
        pma_container = create_pma_application_container(
            runtime_state=PmaRuntimeState(),
            host_state=app.state,
        )
        app.state.pma_container = pma_container
        pma_router = build_pma_routes(container=pma_container)
        app.include_router(pma_router)
        app.state.pma_lane_worker_start = getattr(
            pma_router, "_pma_start_lane_worker", None
        )
        app.state.pma_lane_worker_stop = getattr(
            pma_router, "_pma_stop_lane_worker", None
        )
        app.state.pma_lane_worker_stop_all = getattr(
            pma_router, "_pma_stop_all_lane_workers", None
        )
    app.include_router(build_feedback_report_routes())
    app.include_router(build_scm_webhook_routes())
    app.include_router(build_hub_filebox_routes())
    app.include_router(build_hub_control_plane_routes())

    app.state.hub_started = False
    app.state.hub_deferred_startup_complete = False
    repo_server_overrides: Optional[ServerOverrides] = None
    if context.config.repo_server_inherit:
        repo_server_overrides = ServerOverrides(
            allowed_hosts=resolve_allowed_hosts(
                context.config.server_host, context.config.server_allowed_hosts
            ),
            allowed_origins=list(context.config.server_allowed_origins),
            auth_token_env=context.config.server_auth_token_env,
        )

    mount_manager = HubMountManager(
        app,
        context,
        lambda repo_path: create_repo_app(
            repo_path,
            server_overrides=repo_server_overrides,
            hub_config=context.config,
        ),
    )

    # Mount lightweight placeholders immediately so repo URLs remain routable
    # without paying the cost of constructing every repo app up front.
    _, records = context.supervisor._manifest_records(manifest_only=True)
    initial_snapshots = [
        SimpleNamespace(
            id=record.repo.id,
            path=record.absolute_path,
            initialized=record.initialized,
            exists_on_disk=record.exists_on_disk,
        )
        for record in records
    ]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        registered_pma_lane_starter = False
        pma_lane_starter_register = None
        exception_hooks = None
        startup_completed = False
        app.state.hub_started = True
        record_hub_startup(
            app.state.config.root,
            app.state.logger,
            durable=bool(getattr(app.state.config, "durable_writes", False)),
            host=endpoint_host,
            port=endpoint_port,
            base_path=base_path or "",
        )
        exception_hooks = install_hub_exception_hooks(
            logger=app.state.logger,
            loop=asyncio.get_running_loop(),
        )
        try:
            hub_supervisor = getattr(app.state, "hub_supervisor", None)
            startup_hub_supervisor = getattr(hub_supervisor, "startup", None)
            if callable(startup_hub_supervisor):
                startup_hub_supervisor()

            async def _refresh_mounts_from_manifest() -> None:
                try:
                    snapshots = await asyncio.to_thread(
                        context.supervisor.list_repos, use_cache=False
                    )
                    await mount_manager.refresh_mounts(snapshots)
                except Exception as exc:
                    safe_log(
                        app.state.logger,
                        logging.WARNING,
                        "Hub repo mount refresh failed",
                        exc,
                    )

            tasks.append(asyncio.create_task(_refresh_mounts_from_manifest()))

            async def _deferred_hub_startup() -> None:
                """DB/process-heavy hub startup; runs after /health can succeed."""
                t0 = time.monotonic()
                log = app.state.logger
                log.info("hub.deferred_startup.begin")
                t_phase = time.monotonic()
                try:
                    await recover_orphaned_managed_thread_executions(app)
                    await restart_managed_thread_queue_workers(app)
                except (
                    RuntimeError,
                    TypeError,
                    AttributeError,
                    OSError,
                ) as exc:  # intentional: best-effort startup recovery
                    safe_log(
                        log,
                        logging.WARNING,
                        "Managed-thread queue worker restore failed at hub startup",
                        exc,
                    )
                log.info(
                    "hub.deferred_startup.phase done=managed_thread_restore elapsed_ms=%.2f",
                    (time.monotonic() - t_phase) * 1000,
                )
                t_phase = time.monotonic()
                try:
                    cleanup = reap_managed_processes(context.config.root)
                    if cleanup.killed or cleanup.signaled or cleanup.removed:
                        log.info(
                            "Managed process cleanup: killed=%s signaled=%s removed=%s skipped=%s",
                            cleanup.killed,
                            cleanup.signaled,
                            cleanup.removed,
                            cleanup.skipped,
                        )
                    log.info(
                        "hub.deferred_startup.phase done=reap_managed_processes elapsed_ms=%.2f",
                        (time.monotonic() - t_phase) * 1000,
                    )
                except (
                    OSError,
                    RuntimeError,
                    AttributeError,
                ) as exc:  # intentional: best-effort startup cleanup
                    safe_log(
                        log,
                        logging.WARNING,
                        "Managed process reaper failed at hub startup",
                        exc,
                    )
                pma_cfg = getattr(app.state.config, "pma", None)
                if pma_cfg is not None and pma_cfg.enabled:
                    starter = getattr(app.state, "pma_lane_worker_start", None)
                    if starter is not None:
                        t_phase = time.monotonic()
                        try:
                            started_lanes = await _start_replayable_pma_lane_workers(
                                app,
                                starter,
                            )
                        except (
                            RuntimeError,
                            TypeError,
                            AttributeError,
                            OSError,
                            sqlite3.Error,
                        ) as exc:  # intentional: best-effort startup
                            safe_log(
                                log,
                                logging.WARNING,
                                "PMA lane worker startup failed",
                                exc,
                            )
                        else:
                            log.info(
                                "hub.deferred_startup.phase done=pma_lane_worker lanes=%s elapsed_ms=%.2f",
                                ",".join(started_lanes) if started_lanes else "-",
                                (time.monotonic() - t_phase) * 1000,
                            )
                log.info(
                    "hub.deferred_startup.phase skipped=start_repo_lifespans "
                    "detail=repo_apps_stay_lazy_until_first_request"
                )
                app.state.hub_deferred_startup_complete = True
                log.info(
                    "hub.deferred_startup.complete total_elapsed_ms=%.2f",
                    (time.monotonic() - t0) * 1000,
                )

            tasks.append(asyncio.create_task(_deferred_hub_startup()))
            if app.state.config.housekeeping.enabled:
                interval = max(app.state.config.housekeeping.interval_seconds, 1)
                housekeeping_initial_delay = min(interval, 60)
                docker_reaper_initial_delay = 60

                async def _managed_docker_reaper_loop():
                    await asyncio.sleep(docker_reaper_initial_delay)
                    while True:
                        try:
                            await asyncio.to_thread(
                                reap_managed_docker_containers,
                                logger=app.state.logger,
                            )
                        except (
                            RuntimeError,
                            OSError,
                            ConnectionError,
                            ValueError,
                            TypeError,
                        ) as exc:  # intentional: background loop must not crash
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "Managed docker container reaper failed",
                                exc,
                            )
                        await asyncio.sleep(interval)

                async def _housekeeping_loop():
                    await asyncio.sleep(housekeeping_initial_delay)
                    while True:
                        try:
                            try:
                                filebox_summary = await asyncio.to_thread(
                                    prune_filebox_root,
                                    app.state.config.root,
                                    policy=resolve_filebox_retention_policy(
                                        app.state.config.pma
                                    ),
                                )
                                if (
                                    filebox_summary.inbox_pruned
                                    or filebox_summary.outbox_pruned
                                ):
                                    app.state.logger.info(
                                        "FileBox cleanup: inbox_pruned=%s outbox_pruned=%s bytes_before=%s bytes_after=%s",
                                        filebox_summary.inbox_pruned,
                                        filebox_summary.outbox_pruned,
                                        filebox_summary.bytes_before,
                                        filebox_summary.bytes_after,
                                    )
                            except (
                                OSError,
                                RuntimeError,
                                ConnectionError,
                                ValueError,
                                TypeError,
                            ) as exc:  # intentional: background loop must not crash
                                safe_log(
                                    app.state.logger,
                                    logging.WARNING,
                                    "FileBox cleanup task failed",
                                    exc,
                                )
                            await asyncio.to_thread(
                                run_housekeeping_once,
                                app.state.config.housekeeping,
                                app.state.config.root,
                                logger=app.state.logger,
                            )
                            try:
                                summary = await asyncio.to_thread(
                                    run_execution_history_housekeeping_once,
                                    app.state.config.root,
                                    policy=resolve_execution_history_maintenance_policy(
                                        app.state.config.pma
                                    ),
                                )
                                app.state.orchestration_housekeeping = summary.to_dict()
                            except (
                                OSError,
                                RuntimeError,
                                ConnectionError,
                                ValueError,
                                TypeError,
                                sqlite3.Error,
                            ) as exc:
                                safe_log(
                                    app.state.logger,
                                    logging.WARNING,
                                    "Orchestration execution-history housekeeping failed",
                                    exc,
                                )
                        except (
                            RuntimeError,
                            OSError,
                            ConnectionError,
                            ValueError,
                            TypeError,
                            sqlite3.Error,
                        ) as exc:  # intentional: background loop must not crash
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "Housekeeping task failed",
                                exc,
                            )
                        await asyncio.sleep(interval)

                tasks.append(asyncio.create_task(_managed_docker_reaper_loop()))
                tasks.append(asyncio.create_task(_housekeeping_loop()))
                flow_sweep_interval = _resolve_hub_flow_sweep_interval_seconds(
                    app.state.config.repo_defaults,
                    app.state.logger,
                )
                flow_sweep_initial_delay = min(flow_sweep_interval, 60)

                async def _flow_telemetry_sweep_loop():
                    await asyncio.sleep(flow_sweep_initial_delay)
                    while True:
                        try:
                            from ...core.flows.flow_telemetry_hooks import (
                                housekeep_sweep_repos,
                            )
                            from ...manifest import load_manifest

                            hub_root = app.state.config.root
                            manifest_path = app.state.config.manifest_path
                            if manifest_path.exists():
                                manifest = load_manifest(manifest_path, hub_root)
                                repo_roots = [
                                    (hub_root / entry.path).resolve()
                                    for entry in manifest.repos
                                ]
                                await asyncio.to_thread(
                                    housekeep_sweep_repos, repo_roots
                                )
                        except (
                            RuntimeError,
                            OSError,
                            ConnectionError,
                            ValueError,
                            TypeError,
                        ) as exc:
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "Flow telemetry sweep failed",
                                exc,
                            )
                        await asyncio.sleep(flow_sweep_interval)

                tasks.append(asyncio.create_task(_flow_telemetry_sweep_loop()))

                async def _flow_worker_reaper_loop():
                    await asyncio.sleep(
                        min(DEFAULT_FLOW_WORKER_REAP_INTERVAL_SECONDS, 60)
                    )
                    while True:
                        try:
                            from ...manifest import load_manifest

                            hub_root = app.state.config.root
                            manifest_path = app.state.config.manifest_path
                            if manifest_path.exists():
                                manifest = load_manifest(manifest_path, hub_root)
                                repo_roots = [
                                    (hub_root / entry.path).resolve()
                                    for entry in manifest.repos
                                ]
                                for repo_root in repo_roots:
                                    await asyncio.to_thread(
                                        reap_stale_flow_workers,
                                        repo_root,
                                        logger=app.state.logger,
                                    )
                        except (
                            RuntimeError,
                            OSError,
                            ConnectionError,
                            ValueError,
                            TypeError,
                        ) as exc:
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "Flow worker reaper failed",
                                exc,
                            )
                        await asyncio.sleep(DEFAULT_FLOW_WORKER_REAP_INTERVAL_SECONDS)

                tasks.append(asyncio.create_task(_flow_worker_reaper_loop()))
            app_server_supervisor = cast(
                Optional[_IdlePrunable],
                getattr(app.state, "app_server_supervisor", None),
            )
            app_server_prune_interval_raw = getattr(
                app.state, "app_server_prune_interval", None
            )
            if app_server_supervisor is not None and isinstance(
                app_server_prune_interval_raw, (int, float)
            ):
                app_server_prune_interval = float(app_server_prune_interval_raw)
                tasks.append(
                    asyncio.create_task(
                        _run_prune_loop(
                            interval_seconds=app_server_prune_interval,
                            supervisor=app_server_supervisor,
                            logger=app.state.logger,
                            failure_message="Hub app-server prune task failed",
                        )
                    )
                )
            opencode_supervisor = cast(
                Optional[_IdlePrunable],
                getattr(app.state, "opencode_supervisor", None),
            )
            opencode_prune_interval_raw = getattr(
                app.state, "opencode_prune_interval", None
            )
            if opencode_supervisor is not None and isinstance(
                opencode_prune_interval_raw, (int, float)
            ):
                opencode_prune_interval = float(opencode_prune_interval_raw)
                tasks.append(
                    asyncio.create_task(
                        _run_prune_loop(
                            interval_seconds=opencode_prune_interval,
                            supervisor=opencode_supervisor,
                            logger=app.state.logger,
                            failure_message="Hub opencode prune task failed",
                        )
                    )
                )
            pma_cfg = getattr(app.state.config, "pma", None)
            if pma_cfg is not None and pma_cfg.enabled:
                starter = getattr(app.state, "pma_lane_worker_start", None)
                supervisor = getattr(app.state, "hub_supervisor", None)
                register_lane_starter = (
                    getattr(supervisor, "set_pma_lane_worker_starter", None)
                    if supervisor is not None
                    else None
                )
                if starter is not None and callable(register_lane_starter):
                    loop = asyncio.get_running_loop()

                    def _start_lane_worker(lane_id: str) -> None:
                        try:
                            fut = asyncio.run_coroutine_threadsafe(
                                starter(app, lane_id), loop
                            )
                        except (
                            RuntimeError,
                            TypeError,
                            AttributeError,
                        ) as exc:  # intentional: external callback must not crash
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "PMA lane worker startup dispatch failed",
                                exc,
                            )
                            return

                        def _on_done(done_fut) -> None:
                            try:
                                done_fut.result()
                            except (
                                RuntimeError,
                                OSError,
                                ValueError,
                                TypeError,
                                AttributeError,
                                ConnectionError,
                            ) as exc:  # intentional: future callback must not crash
                                safe_log(
                                    app.state.logger,
                                    logging.WARNING,
                                    "PMA lane worker startup failed",
                                    exc,
                                )

                        fut.add_done_callback(_on_done)

                    try:
                        register_lane_starter(_start_lane_worker)
                        registered_pma_lane_starter = True
                        pma_lane_starter_register = register_lane_starter
                    except (RuntimeError, TypeError, AttributeError) as exc:
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "PMA lane worker registration failed",
                            exc,
                        )

            async def _process_monitor_loop() -> None:
                while True:
                    try:
                        await asyncio.to_thread(
                            _record_process_monitor_sample,
                            app.state.config.root,
                        )
                    except (
                        RuntimeError,
                        OSError,
                        ConnectionError,
                        ValueError,
                        TypeError,
                    ) as exc:  # intentional: background loop must not crash
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "Hub process monitor sampling failed",
                            exc,
                        )
                    await asyncio.sleep(DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS)

            tasks.append(asyncio.create_task(_process_monitor_loop()))
            # Default lane worker starts in _deferred_hub_startup so /health is not blocked.
            # Repo apps stay lazy and still activate via _LazyRepoApp._ensure_ready once
            # hub_started is True.
            startup_completed = True
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                hub_supervisor = getattr(app.state, "hub_supervisor", None)
                shutdown_hub_supervisor = getattr(hub_supervisor, "shutdown", None)
                if callable(shutdown_hub_supervisor):
                    try:
                        shutdown_hub_supervisor()
                    except (
                        OSError,
                        RuntimeError,
                    ) as exc:  # intentional: cleanup must not crash
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "Hub supervisor shutdown failed",
                            exc,
                        )
                await mount_manager.stop_repo_mounts()
                if registered_pma_lane_starter and callable(pma_lane_starter_register):
                    try:
                        pma_lane_starter_register(None)
                    except (
                        OSError,
                        RuntimeError,
                    ) as exc:  # intentional: cleanup must not crash
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "PMA lane worker deregistration failed",
                            exc,
                        )
                runtime_services = getattr(app.state, "runtime_services", None)
                if runtime_services is not None:
                    try:
                        await runtime_services.close()
                    except (
                        OSError,
                        RuntimeError,
                    ) as exc:  # intentional: cleanup must not crash
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "Hub runtime services shutdown failed",
                            exc,
                        )
                else:
                    app_server_supervisor = getattr(
                        app.state, "app_server_supervisor", None
                    )
                    if app_server_supervisor is not None:
                        try:
                            await app_server_supervisor.close_all()
                        except (
                            OSError,
                            RuntimeError,
                        ) as exc:  # intentional: cleanup must not crash
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "Hub app-server shutdown failed",
                                exc,
                            )
                    opencode_supervisor = getattr(
                        app.state, "opencode_supervisor", None
                    )
                    if opencode_supervisor is not None:
                        try:
                            await opencode_supervisor.close_all()
                        except (
                            OSError,
                            RuntimeError,
                        ) as exc:  # intentional: cleanup must not crash
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "Hub opencode shutdown failed",
                                exc,
                            )
                static_context = getattr(app.state, "static_assets_context", None)
                if static_context is not None:
                    static_context.close()
                pma_static_context = getattr(
                    app.state, "pma_static_assets_context", None
                )
                if pma_static_context is not None:
                    pma_static_context.close()
                stop_all = getattr(app.state, "pma_lane_worker_stop_all", None)
                if stop_all is not None:
                    try:
                        await stop_all(app)
                    except (
                        OSError,
                        RuntimeError,
                    ) as exc:  # intentional: cleanup must not crash
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "PMA lane worker shutdown failed",
                            exc,
                        )
                else:
                    stopper = getattr(app.state, "pma_lane_worker_stop", None)
                    if stopper is not None:
                        try:
                            await stopper(app, "pma:default")
                        except (
                            OSError,
                            RuntimeError,
                        ) as exc:  # intentional: cleanup must not crash
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "PMA lane worker shutdown failed",
                                exc,
                            )
                if startup_completed:
                    record_hub_clean_shutdown(
                        app.state.config.root,
                        app.state.logger,
                        durable=bool(
                            getattr(app.state.config, "durable_writes", False)
                        ),
                    )
        finally:
            if exception_hooks is not None:
                exception_hooks.restore()

    app.router.lifespan_context = lifespan

    register_simple_hub_routes(app, context)

    app.include_router(build_contextspace_routes())
    app.include_router(build_flow_routes())
    app.include_router(build_settings_routes())
    app.include_router(build_hub_messages_routes(context))
    app.include_router(build_hub_repo_routes(context, mount_manager))

    def _legacy_index_response():
        index_path = context.static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=500, detail="Static UI assets missing; reinstall package"
            )
        html = render_index_html(context.static_dir, app.state.asset_version)
        return HTMLResponse(html, headers=index_response_headers())

    def _pma_index_response():
        index_path = pma_static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=500,
                detail="PMA Hub UI assets missing; run `pnpm pma:build`",
            )
        html = render_pma_index_html(pma_static_dir, base_path=context.base_path)
        return HTMLResponse(
            html, headers=pma_index_response_headers(pma_static_dir, context.base_path)
        )

    @app.get("/", include_in_schema=False)
    def hub_index():
        target = f"{context.base_path}/pma" if context.base_path else "/pma"
        return RedirectResponse(target, status_code=307)

    @app.get("/legacy", include_in_schema=False)
    def legacy_hub_index():
        return _legacy_index_response()

    @app.get("/pma", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    @app.get("/repos", include_in_schema=False)
    @app.get("/repos/{repo_id}", include_in_schema=False)
    @app.get("/worktrees", include_in_schema=False)
    @app.get("/worktrees/{worktree_id}", include_in_schema=False)
    @app.get("/tickets", include_in_schema=False)
    @app.get("/tickets/{ticket_id}", include_in_schema=False)
    @app.get("/contextspace/{workspace_id}", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    def pma_hub_index():
        return _pma_index_response()

    @app.get("/repos/{repo_id}/", include_in_schema=False)
    def pma_repo_index_slash(repo_id: str):
        return _pma_index_response()

    def legacy_repo_frontend_gate(repo_id: str, request: Request, *, legacy_tab: str):
        query = request.url.query
        encoded_repo_id = quote(repo_id, safe="")
        legacy_target = (
            f"{context.base_path}/legacy/repos/{encoded_repo_id}/{legacy_tab}"
            if context.base_path
            else f"/legacy/repos/{encoded_repo_id}/{legacy_tab}"
        )
        if query:
            legacy_target = f"{legacy_target}?{query}"
        pma_target = (
            f"{context.base_path}/repos/{encoded_repo_id}"
            if context.base_path
            else f"/repos/{encoded_repo_id}"
        )
        legacy_target_attr = html_lib.escape(legacy_target, quote=True)
        pma_target_attr = html_lib.escape(pma_target, quote=True)
        html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Legacy CAR UI</title>
  </head>
  <body>
    <main>
      <h1>Legacy/debug route</h1>
      <p>This old CAR UI route is retained for migration and debugging only.</p>
      <p><a href="{pma_target_attr}">Open the PMA Hub route</a></p>
      <p><a href="{legacy_target_attr}">Open this legacy screen</a></p>
    </main>
  </body>
</html>
"""
        return HTMLResponse(html, headers=index_response_headers())

    def _legacy_repo_frontend_endpoint(tab: str):
        def endpoint(repo_id: str, request: Request):
            return legacy_repo_frontend_gate(repo_id, request, legacy_tab=tab)

        return endpoint

    for legacy_tab in sorted(_LEGACY_REPO_FRONTEND_TABS):
        app.add_api_route(
            f"/repos/{{repo_id}}/{legacy_tab}",
            _legacy_repo_frontend_endpoint(legacy_tab),
            methods=["GET"],
            include_in_schema=False,
        )

    app.include_router(build_system_routes())
    mount_manager.mount_initial(initial_snapshots)

    allowed_hosts = resolve_allowed_hosts(
        context.config.server_host, context.config.server_allowed_hosts
    )
    allowed_origins = context.config.server_allowed_origins
    auth_token = resolve_auth_token(context.config.server_auth_token_env)
    app.state.auth_token = auth_token
    asgi_app: ASGIApp = app
    if auth_token:
        asgi_app = AuthTokenMiddleware(asgi_app, auth_token, context.base_path)
    if context.base_path:
        asgi_app = BasePathRouterMiddleware(asgi_app, context.base_path)
    asgi_app = HostOriginMiddleware(asgi_app, allowed_hosts, allowed_origins)
    asgi_app = RequestIdMiddleware(asgi_app)
    asgi_app = SecurityHeadersMiddleware(asgi_app)

    return asgi_app
