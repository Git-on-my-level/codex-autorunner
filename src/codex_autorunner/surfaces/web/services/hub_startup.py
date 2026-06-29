from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, cast

from fastapi import FastAPI

from ....core.config import parse_flow_retention_config
from ....core.diagnostics import (
    DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS,
    DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS,
    ProcessMonitorStore,
    capture_process_monitor_sample,
)
from ....core.filebox_retention import (
    prune_filebox_root,
    resolve_filebox_retention_policy,
)
from ....core.hub_diagnostics import (
    install_hub_exception_hooks,
    record_hub_clean_shutdown,
    record_hub_startup,
)
from ....core.logging_utils import safe_log
from ....core.managed_processes import reap_managed_processes
from ....core.orchestration.execution_history_maintenance import (
    resolve_execution_history_maintenance_policy,
    run_execution_history_housekeeping_once,
)
from ....core.pma_domain.constants import DEFAULT_PMA_LANE_ID
from ....core.pma_queue import PmaQueue, QueueItemState
from ....core.preview_services import PreviewServiceKind
from ....housekeeping import (
    DEFAULT_FLOW_WORKER_REAP_INTERVAL_SECONDS,
    reap_managed_docker_containers,
    reap_stale_flow_workers,
    run_housekeeping_once,
)
from ..app_state import HubAppContext
from .pma.managed_thread_runtime import (
    ensure_managed_thread_queue_worker,
    recover_orphaned_managed_thread_executions,
    restart_managed_thread_queue_workers,
)

_DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS = float(
    parse_flow_retention_config(None).sweep_interval_seconds
)
_DEFAULT_PREVIEW_SERVICE_RECONCILE_INTERVAL_SECONDS = 5.0


class _IdlePrunable(Protocol):
    async def prune_idle(self) -> None: ...


class _HubMountManager(Protocol):
    async def refresh_mounts(
        self, snapshots: Iterable[Any], *, full_refresh: bool = False
    ) -> Any: ...

    async def stop_repo_mounts(self) -> None: ...


async def start_replayable_pma_lane_workers(
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


def resolve_hub_flow_sweep_interval_seconds(
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


async def run_prune_loop(
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


def record_process_monitor_sample(root: Path) -> None:
    store = ProcessMonitorStore(root)
    store.record_sample(
        capture_process_monitor_sample(root),
        cadence_seconds=DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS,
        window_seconds=DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS,
    )


class HubStartupService:
    def __init__(
        self,
        *,
        context: HubAppContext,
        mount_manager: _HubMountManager,
        endpoint_host: Optional[str],
        endpoint_port: Optional[int],
        base_path: Optional[str],
    ) -> None:
        self._context = context
        self._mount_manager = mount_manager
        self._endpoint_host = endpoint_host
        self._endpoint_port = endpoint_port
        self._base_path = base_path

    @asynccontextmanager
    async def lifespan(self, app: FastAPI):
        tasks: list[asyncio.Task] = []
        registered_pma_lane_starter = False
        pma_lane_starter_register = None
        managed_thread_queue_starter_register = None
        exception_hooks = None
        startup_completed = False
        app.state.hub_started = True
        record_hub_startup(
            app.state.config.root,
            app.state.logger,
            durable=bool(getattr(app.state.config, "durable_writes", False)),
            host=self._endpoint_host,
            port=self._endpoint_port,
            base_path=self._base_path or "",
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

            tasks.append(asyncio.create_task(self._refresh_mounts_from_manifest(app)))
            tasks.append(asyncio.create_task(self.run_deferred_startup(app)))
            self._register_housekeeping_tasks(app, tasks)
            self._register_prune_tasks(app, tasks)
            self._register_preview_service_reconciler(app, tasks)
            registered_pma_lane_starter, pma_lane_starter_register = (
                self._register_pma_lane_starter(app)
            )
            managed_thread_queue_starter_register = (
                self._register_managed_thread_queue_starter(app)
            )
            tasks.append(asyncio.create_task(self._process_monitor_loop(app)))
            startup_completed = True
            try:
                yield
            finally:
                await self._shutdown(
                    app,
                    tasks,
                    registered_pma_lane_starter=registered_pma_lane_starter,
                    pma_lane_starter_register=pma_lane_starter_register,
                    managed_thread_queue_starter_register=(
                        managed_thread_queue_starter_register
                    ),
                    startup_completed=startup_completed,
                )
        finally:
            if exception_hooks is not None:
                exception_hooks.restore()

    async def _refresh_mounts_from_manifest(self, app: FastAPI) -> None:
        try:
            snapshots = await asyncio.to_thread(
                self._context.supervisor.list_repos, use_cache=False
            )
            await self._mount_manager.refresh_mounts(snapshots)
        except Exception as exc:
            safe_log(
                app.state.logger,
                logging.WARNING,
                "Hub repo mount refresh failed",
                exc,
            )

    async def run_deferred_startup(self, app: FastAPI) -> None:
        """DB/process-heavy hub startup; runs after /health can succeed."""
        t0 = time.monotonic()
        log = app.state.logger
        log.info("hub.deferred_startup.begin")
        await self._restore_managed_threads(app, log)
        self._reap_managed_processes(app, log)
        await self._start_pma_lane_workers(app, log)
        await self._start_autostart_preview_services(app, log)
        log.info(
            "hub.deferred_startup.phase skipped=start_repo_lifespans "
            "detail=repo_apps_stay_lazy_until_first_request"
        )
        app.state.hub_deferred_startup_complete = True
        log.info(
            "hub.deferred_startup.complete total_elapsed_ms=%.2f",
            (time.monotonic() - t0) * 1000,
        )

    async def _restore_managed_threads(self, app: FastAPI, log: logging.Logger) -> None:
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

    def _reap_managed_processes(self, app: FastAPI, log: logging.Logger) -> None:
        t_phase = time.monotonic()
        try:
            cleanup = reap_managed_processes(self._context.config.root)
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

    def _preview_services_enabled(self, config: object) -> bool:
        raw_config = getattr(config, "raw", {})
        if not isinstance(raw_config, dict):
            return True
        preview_config = raw_config.get("preview_services")
        if not isinstance(preview_config, dict):
            return True
        enabled = preview_config.get("enabled")
        return True if enabled is None else bool(enabled)

    def _preview_service_reconcile_interval_seconds(self, config: object) -> float:
        raw_config = getattr(config, "raw", {})
        if not isinstance(raw_config, dict):
            return _DEFAULT_PREVIEW_SERVICE_RECONCILE_INTERVAL_SECONDS
        preview_config = raw_config.get("preview_services")
        if not isinstance(preview_config, dict):
            return _DEFAULT_PREVIEW_SERVICE_RECONCILE_INTERVAL_SECONDS
        raw_interval = preview_config.get("reconcile_interval_seconds")
        if not isinstance(raw_interval, (int, float)) or raw_interval <= 0:
            return _DEFAULT_PREVIEW_SERVICE_RECONCILE_INTERVAL_SECONDS
        return float(raw_interval)

    def _register_preview_service_reconciler(
        self,
        app: FastAPI,
        tasks: list[asyncio.Task],
    ) -> None:
        if not self._preview_services_enabled(app.state.config):
            return
        manager = getattr(self._context, "preview_service_manager", None)
        if manager is None:
            return
        interval = self._preview_service_reconcile_interval_seconds(app.state.config)
        tasks.append(
            asyncio.create_task(
                self._preview_service_reconciler_loop(
                    app,
                    initial_delay=min(interval, 5.0),
                    interval=interval,
                )
            )
        )

    async def _preview_service_reconciler_loop(
        self,
        app: FastAPI,
        *,
        initial_delay: float,
        interval: float,
    ) -> None:
        await asyncio.sleep(initial_delay)
        manager = self._context.preview_service_manager
        while True:
            try:
                await asyncio.to_thread(manager.reconcile)
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                AttributeError,
            ) as exc:  # intentional: background loop must not crash
                safe_log(
                    app.state.logger,
                    logging.WARNING,
                    "Preview service reconciler failed",
                    exc,
                )
            await asyncio.sleep(interval)

    async def _start_autostart_preview_services(
        self, app: FastAPI, log: logging.Logger
    ) -> None:
        if not self._preview_services_enabled(app.state.config):
            return
        manager = getattr(self._context, "preview_service_manager", None)
        if manager is None:
            return
        t_phase = time.monotonic()
        started: list[str] = []
        try:
            records = await asyncio.to_thread(manager.registry.list)
            for record in records:
                if record.kind != PreviewServiceKind.MANAGED_COMMAND.value:
                    continue
                if not record.restart_policy.auto_start_on_hub_start:
                    continue
                try:
                    await asyncio.to_thread(manager.start, record.service_id)
                    started.append(record.service_id)
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                ) as exc:  # intentional: best-effort startup
                    safe_log(
                        log,
                        logging.WARNING,
                        f"Preview service autostart failed for {record.service_id}",
                        exc,
                    )
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:  # intentional: best-effort startup
            safe_log(
                log,
                logging.WARNING,
                "Preview service autostart pass failed at hub startup",
                exc,
            )
        else:
            log.info(
                "hub.deferred_startup.phase done=preview_service_autostart services=%s elapsed_ms=%.2f",
                ",".join(started) if started else "-",
                (time.monotonic() - t_phase) * 1000,
            )

    async def _start_pma_lane_workers(self, app: FastAPI, log: logging.Logger) -> None:
        pma_cfg = getattr(app.state.config, "pma", None)
        if pma_cfg is None or not pma_cfg.enabled:
            return
        starter = getattr(app.state, "pma_lane_worker_start", None)
        if starter is None:
            return
        t_phase = time.monotonic()
        try:
            started_lanes = await start_replayable_pma_lane_workers(app, starter)
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

    def _register_housekeeping_tasks(
        self, app: FastAPI, tasks: list[asyncio.Task]
    ) -> None:
        if not app.state.config.housekeeping.enabled:
            return
        interval = max(app.state.config.housekeeping.interval_seconds, 1)
        housekeeping_initial_delay = min(interval, 60)
        docker_reaper_initial_delay = 60
        tasks.append(
            asyncio.create_task(
                self._managed_docker_reaper_loop(
                    app,
                    initial_delay=docker_reaper_initial_delay,
                    interval=interval,
                )
            )
        )
        tasks.append(
            asyncio.create_task(
                self._housekeeping_loop(
                    app,
                    initial_delay=housekeeping_initial_delay,
                    interval=interval,
                )
            )
        )
        flow_sweep_interval = resolve_hub_flow_sweep_interval_seconds(
            app.state.config.repo_defaults,
            app.state.logger,
        )
        tasks.append(
            asyncio.create_task(
                self._flow_telemetry_sweep_loop(
                    app,
                    initial_delay=min(flow_sweep_interval, 60),
                    interval=flow_sweep_interval,
                )
            )
        )
        tasks.append(asyncio.create_task(self._flow_worker_reaper_loop(app)))

    async def _managed_docker_reaper_loop(
        self, app: FastAPI, *, initial_delay: float, interval: float
    ) -> None:
        await asyncio.sleep(initial_delay)
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

    async def _housekeeping_loop(
        self, app: FastAPI, *, initial_delay: float, interval: float
    ) -> None:
        await asyncio.sleep(initial_delay)
        while True:
            try:
                try:
                    filebox_summary = await asyncio.to_thread(
                        prune_filebox_root,
                        app.state.config.root,
                        policy=resolve_filebox_retention_policy(app.state.config.pma),
                    )
                    if filebox_summary.inbox_pruned or filebox_summary.outbox_pruned:
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

    async def _flow_telemetry_sweep_loop(
        self, app: FastAPI, *, initial_delay: float, interval: float
    ) -> None:
        await asyncio.sleep(initial_delay)
        while True:
            try:
                from ....core.flows.flow_telemetry_hooks import housekeep_sweep_repos
                from ....manifest import load_manifest

                hub_root = app.state.config.root
                manifest_path = app.state.config.manifest_path
                if manifest_path.exists():
                    manifest = load_manifest(manifest_path, hub_root)
                    repo_roots = [
                        (hub_root / entry.path).resolve() for entry in manifest.repos
                    ]
                    await asyncio.to_thread(housekeep_sweep_repos, repo_roots)
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
            await asyncio.sleep(interval)

    async def _flow_worker_reaper_loop(self, app: FastAPI) -> None:
        await asyncio.sleep(min(DEFAULT_FLOW_WORKER_REAP_INTERVAL_SECONDS, 60))
        while True:
            try:
                from ....manifest import load_manifest

                hub_root = app.state.config.root
                manifest_path = app.state.config.manifest_path
                if manifest_path.exists():
                    manifest = load_manifest(manifest_path, hub_root)
                    repo_roots = [
                        (hub_root / entry.path).resolve() for entry in manifest.repos
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

    def _register_prune_tasks(self, app: FastAPI, tasks: list[asyncio.Task]) -> None:
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
            tasks.append(
                asyncio.create_task(
                    run_prune_loop(
                        interval_seconds=float(app_server_prune_interval_raw),
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
            tasks.append(
                asyncio.create_task(
                    run_prune_loop(
                        interval_seconds=float(opencode_prune_interval_raw),
                        supervisor=opencode_supervisor,
                        logger=app.state.logger,
                        failure_message="Hub opencode prune task failed",
                    )
                )
            )

    def _register_pma_lane_starter(self, app: FastAPI) -> tuple[bool, object]:
        pma_cfg = getattr(app.state.config, "pma", None)
        if pma_cfg is None or not pma_cfg.enabled:
            return False, None
        starter = getattr(app.state, "pma_lane_worker_start", None)
        supervisor = getattr(app.state, "hub_supervisor", None)
        register_lane_starter = (
            getattr(supervisor, "set_pma_lane_worker_starter", None)
            if supervisor is not None
            else None
        )
        if starter is None or not callable(register_lane_starter):
            return False, None
        loop = asyncio.get_running_loop()

        def _start_lane_worker(lane_id: str) -> None:
            try:
                fut = asyncio.run_coroutine_threadsafe(starter(app, lane_id), loop)
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
            return True, register_lane_starter
        except (RuntimeError, TypeError, AttributeError) as exc:
            safe_log(
                app.state.logger,
                logging.WARNING,
                "PMA lane worker registration failed",
                exc,
            )
            return False, None

    def _register_managed_thread_queue_starter(self, app: FastAPI) -> object:
        supervisor = getattr(app.state, "hub_supervisor", None)
        register_starter = (
            getattr(supervisor, "set_managed_thread_queue_worker_starter", None)
            if supervisor is not None
            else None
        )
        if not callable(register_starter):
            return None
        loop = asyncio.get_running_loop()

        def _start_queue_worker(thread_id: str) -> None:
            def _on_loop() -> None:
                try:
                    ensure_managed_thread_queue_worker(app, thread_id)
                except (
                    RuntimeError,
                    TypeError,
                    AttributeError,
                ) as exc:  # intentional: external callback must not crash
                    safe_log(
                        app.state.logger,
                        logging.WARNING,
                        "Managed-thread queue worker startup failed",
                        exc,
                    )

            try:
                loop.call_soon_threadsafe(_on_loop)
            except (
                RuntimeError,
                TypeError,
                AttributeError,
            ) as exc:  # intentional: external callback must not crash
                safe_log(
                    app.state.logger,
                    logging.WARNING,
                    "Managed-thread queue worker startup dispatch failed",
                    exc,
                )

        try:
            register_starter(_start_queue_worker)
            return register_starter
        except (RuntimeError, TypeError, AttributeError) as exc:
            safe_log(
                app.state.logger,
                logging.WARNING,
                "Managed-thread queue worker registration failed",
                exc,
            )
            return None

    async def _process_monitor_loop(self, app: FastAPI) -> None:
        await asyncio.sleep(DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS)
        while True:
            try:
                await asyncio.to_thread(
                    record_process_monitor_sample,
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

    async def _shutdown(
        self,
        app: FastAPI,
        tasks: list[asyncio.Task],
        *,
        registered_pma_lane_starter: bool,
        pma_lane_starter_register: object,
        managed_thread_queue_starter_register: object,
        startup_completed: bool,
    ) -> None:
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
        await self._mount_manager.stop_repo_mounts()
        if callable(managed_thread_queue_starter_register):
            try:
                managed_thread_queue_starter_register(None)
            except (
                OSError,
                RuntimeError,
            ) as exc:  # intentional: cleanup must not crash
                safe_log(
                    app.state.logger,
                    logging.WARNING,
                    "Managed-thread queue worker starter cleanup failed",
                    exc,
                )
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
        await self._close_runtime_services(app)
        self._close_web_static_context(app)
        await self._stop_pma_lane_workers(app)
        if startup_completed:
            record_hub_clean_shutdown(
                app.state.config.root,
                app.state.logger,
                durable=bool(getattr(app.state.config, "durable_writes", False)),
            )

    async def _close_runtime_services(self, app: FastAPI) -> None:
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
            return
        app_server_supervisor = getattr(app.state, "app_server_supervisor", None)
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
        opencode_supervisor = getattr(app.state, "opencode_supervisor", None)
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

    def _close_web_static_context(self, app: FastAPI) -> None:
        web_static_context = getattr(app.state, "web_static_assets_context", None)
        if web_static_context is not None:
            web_static_context.close()

    async def _stop_pma_lane_workers(self, app: FastAPI) -> None:
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
            return
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
