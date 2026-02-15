import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp

from ...core.logging_utils import safe_log
from ...housekeeping import run_housekeeping_once
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
from .routes.system import build_system_routes
from .static_assets import (
    index_response_headers,
    render_index_html,
)

__all__ = ["create_app", "create_repo_app", "create_hub_app"]


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
        app.state.hub_started = True
        if app.state.config.housekeeping.enabled:
            interval = max(app.state.config.housekeeping.interval_seconds, 1)

            async def _housekeeping_loop():
                while True:
                    try:
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

            tasks.append(asyncio.create_task(_housekeeping_loop()))
        app_server_supervisor = getattr(app.state, "app_server_supervisor", None)
        app_server_prune_interval = getattr(
            app.state, "app_server_prune_interval", None
        )
        if app_server_supervisor is not None and app_server_prune_interval:

            async def _app_server_prune_loop():
                while True:
                    await asyncio.sleep(app_server_prune_interval)
                    try:
                        await app_server_supervisor.prune_idle()
                    except Exception as exc:
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "Hub app-server prune task failed",
                            exc,
                        )

            tasks.append(asyncio.create_task(_app_server_prune_loop()))
        opencode_supervisor = getattr(app.state, "opencode_supervisor", None)
        opencode_prune_interval = getattr(app.state, "opencode_prune_interval", None)
        if opencode_supervisor is not None and opencode_prune_interval:

            async def _opencode_prune_loop():
                while True:
                    await asyncio.sleep(opencode_prune_interval)
                    try:
                        await opencode_supervisor.prune_idle()
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
            app_server_supervisor = getattr(app.state, "app_server_supervisor", None)
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
