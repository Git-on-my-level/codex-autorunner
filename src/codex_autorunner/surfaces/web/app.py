import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Protocol, cast

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp

from ...core.filebox_retention import (
    prune_filebox_root,
    resolve_filebox_retention_policy,
)
from ...core.logging_utils import safe_log
from ...core.managed_processes import reap_managed_processes
from ...core.orchestration.runtime_bindings import (
    clear_runtime_thread_bindings_for_hub_root,
)
from ...housekeeping import reap_managed_docker_containers, run_housekeeping_once
from .app_builders import create_app, create_repo_app
from .app_factory import CacheStaticFiles, resolve_allowed_hosts, resolve_auth_token
from .app_state import ServerOverrides, apply_hub_context, build_hub_context
from .hub_routes import register_simple_hub_routes
from .middleware import (
    AuthTokenMiddleware,
    BasePathRouterMiddleware,
    HostOriginMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from .routes.filebox import build_hub_filebox_routes
from .routes.hub_messages import build_hub_messages_routes
from .routes.hub_repos import HubMountManager, build_hub_repo_routes
from .routes.pma import build_pma_routes
from .routes.pma_routes.managed_thread_runtime import (
    recover_orphaned_managed_thread_executions,
    restart_managed_thread_queue_workers,
)
from .routes.system import build_system_routes
from .static_assets import (
    index_response_headers,
    render_index_html,
)

__all__ = ["create_app", "create_hub_app", "create_repo_app"]


class _IdlePrunable(Protocol):
    async def prune_idle(self) -> None: ...


def create_hub_app(
    hub_root: Optional[Path] = None, base_path: Optional[str] = None
) -> ASGIApp:
    context = build_hub_context(hub_root, base_path)
    app = FastAPI(redirect_slashes=False)
    apply_hub_context(app, context)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    static_files = CacheStaticFiles(directory=context.static_dir)
    app.state.static_files = static_files
    app.state.static_assets_lock = threading.Lock()
    app.state.hub_static_assets = None
    app.mount("/static", static_files, name="static")
    raw_config = getattr(context.config, "raw", {})
    pma_config = raw_config.get("pma", {}) if isinstance(raw_config, dict) else {}
    if isinstance(pma_config, dict) and pma_config.get("enabled"):
        pma_router = build_pma_routes()
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
    app.include_router(build_hub_filebox_routes())

    app.state.hub_started = False
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

    initial_snapshots = context.supervisor.scan()
    mount_manager.mount_initial(initial_snapshots)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        registered_pma_lane_starter = False
        pma_lane_starter_register = None
        app.state.hub_started = True
        clear_runtime_thread_bindings_for_hub_root(context.config.root)
        try:
            await recover_orphaned_managed_thread_executions(app)
            await restart_managed_thread_queue_workers(app)
        except Exception as exc:
            safe_log(
                app.state.logger,
                logging.WARNING,
                "Managed-thread queue worker restore failed at hub startup",
                exc,
            )
        try:
            cleanup = reap_managed_processes(context.config.root)
            if cleanup.killed or cleanup.signaled or cleanup.removed:
                app.state.logger.info(
                    "Managed process cleanup: killed=%s signaled=%s removed=%s skipped=%s",
                    cleanup.killed,
                    cleanup.signaled,
                    cleanup.removed,
                    cleanup.skipped,
                )
        except Exception as exc:
            safe_log(
                app.state.logger,
                logging.WARNING,
                "Managed process reaper failed at hub startup",
                exc,
            )
        if app.state.config.housekeeping.enabled:
            interval = max(app.state.config.housekeeping.interval_seconds, 1)
            initial_delay = min(interval, 60)

            async def _managed_docker_reaper_loop():
                await asyncio.sleep(initial_delay)
                while True:
                    try:
                        await asyncio.to_thread(
                            reap_managed_docker_containers,
                            logger=app.state.logger,
                        )
                    except Exception as exc:
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "Managed docker container reaper failed",
                            exc,
                        )
                    await asyncio.sleep(interval)

            async def _housekeeping_loop():
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
                        except Exception as exc:
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
                    except Exception as exc:
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "Housekeeping task failed",
                            exc,
                        )
                    await asyncio.sleep(interval)

            tasks.append(asyncio.create_task(_managed_docker_reaper_loop()))
            tasks.append(asyncio.create_task(_housekeeping_loop()))
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
            prune_supervisor = app_server_supervisor
            app_server_prune_interval = float(app_server_prune_interval_raw)

            async def _app_server_prune_loop():
                while True:
                    await asyncio.sleep(app_server_prune_interval)
                    try:
                        await prune_supervisor.prune_idle()
                    except Exception as exc:
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "Hub app-server prune task failed",
                            exc,
                        )

            tasks.append(asyncio.create_task(_app_server_prune_loop()))
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
            prune_supervisor = opencode_supervisor
            opencode_prune_interval = float(opencode_prune_interval_raw)

            async def _opencode_prune_loop():
                while True:
                    await asyncio.sleep(opencode_prune_interval)
                    try:
                        await prune_supervisor.prune_idle()
                    except Exception as exc:
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "Hub opencode prune task failed",
                            exc,
                        )

            tasks.append(asyncio.create_task(_opencode_prune_loop()))
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
                    except Exception as exc:
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
                        except Exception as exc:
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
                except Exception as exc:
                    safe_log(
                        app.state.logger,
                        logging.WARNING,
                        "PMA lane worker registration failed",
                        exc,
                    )
            if starter is not None:
                try:
                    await starter(app, "pma:default")
                except Exception as exc:
                    safe_log(
                        app.state.logger,
                        logging.WARNING,
                        "PMA lane worker startup failed",
                        exc,
                    )
        await mount_manager.start_repo_lifespans()
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await mount_manager.stop_repo_mounts()
            if registered_pma_lane_starter and callable(pma_lane_starter_register):
                try:
                    pma_lane_starter_register(None)
                except Exception as exc:
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
                except Exception as exc:
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
                    except Exception as exc:
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
                    except Exception as exc:
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "Hub opencode shutdown failed",
                            exc,
                        )
            static_context = getattr(app.state, "static_assets_context", None)
            if static_context is not None:
                static_context.close()
            stop_all = getattr(app.state, "pma_lane_worker_stop_all", None)
            if stop_all is not None:
                try:
                    await stop_all(app)
                except Exception as exc:
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
                    except Exception as exc:
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "PMA lane worker shutdown failed",
                            exc,
                        )

    app.router.lifespan_context = lifespan

    register_simple_hub_routes(app, context)

    app.include_router(build_hub_messages_routes(context))
    app.include_router(build_hub_repo_routes(context, mount_manager))

    @app.get("/", include_in_schema=False)
    def hub_index():
        index_path = context.static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=500, detail="Static UI assets missing; reinstall package"
            )
        html = render_index_html(context.static_dir, app.state.asset_version)
        return HTMLResponse(html, headers=index_response_headers())

    app.include_router(build_system_routes())

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
