import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp

from ...core.config import HubConfig
from ...core.flows.reconciler import reconcile_flow_runs
from ...core.logging_utils import safe_log
from ...core.state import persist_session_registry
from ...core.utils import reset_repo_root_context, set_repo_root_context
from ...housekeeping import run_housekeeping_once
from .app_factory import CacheStaticFiles, resolve_allowed_hosts, resolve_auth_token
from .app_state import AppContext, ServerOverrides, apply_app_context, build_app_context
from .middleware import (
    AuthTokenMiddleware,
    BasePathRouterMiddleware,
    HostOriginMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from .routes import build_repo_router
from .terminal_sessions import prune_terminal_registry


def _app_lifespan(context: AppContext):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []

        async def _cleanup_loop():
            try:
                while True:
                    await asyncio.sleep(600)  # Check every 10 mins
                    try:
                        async with app.state.terminal_lock:
                            prune_terminal_registry(
                                app.state.engine.state_path,
                                app.state.terminal_sessions,
                                app.state.session_registry,
                                app.state.repo_to_session,
                                app.state.terminal_max_idle_seconds,
                            )
                    except Exception as exc:
                        safe_log(
                            app.state.logger,
                            logging.WARNING,
                            "Terminal cleanup task failed",
                            exc,
                        )
            except asyncio.CancelledError:
                return

        async def _housekeeping_loop():
            config = app.state.config.housekeeping
            interval = max(config.interval_seconds, 1)
            try:
                while True:
                    try:
                        await asyncio.to_thread(
                            run_housekeeping_once,
                            config,
                            app.state.engine.repo_root,
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
            except asyncio.CancelledError:
                return

        async def _flow_reconcile_loop():
            active_interval = 2.0
            idle_interval = 5.0
            try:
                while True:
                    result = await asyncio.to_thread(
                        reconcile_flow_runs,
                        app.state.engine.repo_root,
                        logger=app.state.logger,
                    )
                    interval = (
                        active_interval if result.summary.active > 0 else idle_interval
                    )
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

        tasks.append(asyncio.create_task(_cleanup_loop()))
        if app.state.config.housekeeping.enabled:
            tasks.append(asyncio.create_task(_housekeeping_loop()))
        tasks.append(asyncio.create_task(_flow_reconcile_loop()))
        app_server_supervisor = getattr(app.state, "app_server_supervisor", None)
        app_server_prune_interval = getattr(
            app.state, "app_server_prune_interval", None
        )
        if app_server_supervisor is not None and app_server_prune_interval:

            async def _app_server_prune_loop():
                try:
                    while True:
                        await asyncio.sleep(app_server_prune_interval)
                        try:
                            await app_server_supervisor.prune_idle()
                        except Exception as exc:
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "App-server prune task failed",
                                exc,
                            )
                except asyncio.CancelledError:
                    return

            tasks.append(asyncio.create_task(_app_server_prune_loop()))

        opencode_supervisor = getattr(app.state, "opencode_supervisor", None)
        opencode_prune_interval = getattr(app.state, "opencode_prune_interval", None)
        if opencode_supervisor is not None and opencode_prune_interval:

            async def _opencode_prune_loop():
                try:
                    while True:
                        await asyncio.sleep(opencode_prune_interval)
                        try:
                            await opencode_supervisor.prune_idle()
                        except Exception as exc:
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "OpenCode prune task failed",
                                exc,
                            )
                except asyncio.CancelledError:
                    return

            tasks.append(asyncio.create_task(_opencode_prune_loop()))

        if (
            context.tui_idle_seconds is not None
            and context.tui_idle_check_seconds is not None
        ):

            async def _tui_idle_loop():
                try:
                    while True:
                        await asyncio.sleep(context.tui_idle_check_seconds)
                        try:
                            async with app.state.terminal_lock:
                                terminal_sessions = app.state.terminal_sessions
                                session_registry = app.state.session_registry
                                for session_id, session in list(
                                    terminal_sessions.items()
                                ):
                                    if not session.pty.isalive():
                                        continue
                                    if not session.should_notify_idle(
                                        context.tui_idle_seconds
                                    ):
                                        continue
                                    record = session_registry.get(session_id)
                                    repo_path = record.repo_path if record else None
                                    notifier = getattr(
                                        app.state.engine, "notifier", None
                                    )
                                    if notifier:
                                        asyncio.create_task(
                                            notifier.notify_tui_idle_async(
                                                session_id=session_id,
                                                idle_seconds=context.tui_idle_seconds,
                                                repo_path=repo_path,
                                            )
                                        )
                        except Exception as exc:
                            safe_log(
                                app.state.logger,
                                logging.WARNING,
                                "TUI idle notification loop failed",
                                exc,
                            )
                except asyncio.CancelledError:
                    return

            tasks.append(asyncio.create_task(_tui_idle_loop()))

        # Shutdown event for graceful SSE/WebSocket termination during reload
        app.state.shutdown_event = asyncio.Event()
        app.state.active_websockets: set = set()

        try:
            yield
        finally:
            # Signal SSE streams to stop and close WebSocket connections
            app.state.shutdown_event.set()
            for ws in list(app.state.active_websockets):
                try:
                    await ws.close(code=1012)  # 1012 = Service Restart
                except Exception as exc:
                    safe_log(
                        app.state.logger,
                        logging.DEBUG,
                        "Failed to close websocket during shutdown",
                        exc=exc,
                    )
            app.state.active_websockets.clear()

            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            async with app.state.terminal_lock:
                for session in app.state.terminal_sessions.values():
                    session.close()
                app.state.terminal_sessions.clear()
                app.state.session_registry.clear()
                app.state.repo_to_session.clear()
                persist_session_registry(
                    app.state.engine.state_path,
                    app.state.session_registry,
                    app.state.repo_to_session,
                )
            app_server_supervisor = getattr(app.state, "app_server_supervisor", None)
            if app_server_supervisor is not None:
                try:
                    await app_server_supervisor.close_all()
                except Exception as exc:
                    safe_log(
                        app.state.logger,
                        logging.WARNING,
                        "App-server shutdown failed",
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
                        "OpenCode shutdown failed",
                        exc,
                    )
            static_context = getattr(app.state, "static_assets_context", None)
            if static_context is not None:
                static_context.close()

    return lifespan


def _add_shared_repo_middlewares(
    app: FastAPI,
    context: AppContext,
    server_overrides: Optional[ServerOverrides] = None,
    hub_config: Optional[HubConfig] = None,
    *,
    include_base_path_router: bool,
) -> None:
    class _RepoRootContextMiddleware(BaseHTTPMiddleware):
        """Ensure find_repo_root() resolves to the mounted repo even when cwd differs."""

        def __init__(self, app, repo_root: Path):
            super().__init__(app)
            self.repo_root = repo_root

        async def dispatch(self, request, call_next):
            token = set_repo_root_context(self.repo_root)
            try:
                return await call_next(request)
            finally:
                reset_repo_root_context(token)

    app.add_middleware(_RepoRootContextMiddleware, repo_root=context.engine.repo_root)
    apply_app_context(app, context)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    static_files = CacheStaticFiles(directory=context.static_dir)
    app.state.static_files = static_files
    app.state.static_assets_lock = threading.Lock()
    app.state.hub_static_assets = (
        hub_config.static_assets if hub_config is not None else None
    )
    app.mount("/static", static_files, name="static")
    # Route handlers
    app.include_router(build_repo_router(context.static_dir))

    allowed_hosts = resolve_allowed_hosts(
        context.engine.config.server_host, context.engine.config.server_allowed_hosts
    )
    allowed_origins = context.engine.config.server_allowed_origins
    auth_token_env = context.engine.config.server_auth_token_env
    if server_overrides is not None:
        if server_overrides.allowed_hosts is not None:
            allowed_hosts = list(server_overrides.allowed_hosts)
        if server_overrides.allowed_origins is not None:
            allowed_origins = list(server_overrides.allowed_origins)
        if server_overrides.auth_token_env is not None:
            auth_token_env = server_overrides.auth_token_env
    auth_token = resolve_auth_token(auth_token_env, env=context.env)
    app.state.auth_token = auth_token
    if auth_token:
        app.add_middleware(
            AuthTokenMiddleware, auth_token=auth_token, base_path=context.base_path
        )
    if include_base_path_router:
        app.add_middleware(BasePathRouterMiddleware, base_path=context.base_path)
    app.add_middleware(
        HostOriginMiddleware,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)


def create_repo_app(
    repo_root: Path,
    server_overrides: Optional[ServerOverrides] = None,
    hub_config: Optional[HubConfig] = None,
) -> ASGIApp:
    # Hub-only: repo apps are always mounted under `/repos/<id>` and must not
    # apply their own base-path rewriting (the hub handles that globally).
    context = build_app_context(repo_root, base_path="", hub_config=hub_config)
    app = FastAPI(redirect_slashes=False, lifespan=_app_lifespan(context))

    _add_shared_repo_middlewares(
        app,
        context,
        server_overrides,
        hub_config=hub_config,
        include_base_path_router=False,
    )

    return app


def create_app(
    repo_root: Optional[Path] = None,
    base_path: Optional[str] = None,
    server_overrides: Optional[ServerOverrides] = None,
    hub_config: Optional[HubConfig] = None,
) -> ASGIApp:
    """
    Public-facing factory for standalone repo apps (non-hub) retained for backward compatibility.
    """
    # Respect provided base_path when running directly; hub passes base_path="".
    context = build_app_context(repo_root, base_path, hub_config=hub_config)
    app = FastAPI(redirect_slashes=False, lifespan=_app_lifespan(context))

    _add_shared_repo_middlewares(
        app,
        context,
        server_overrides,
        hub_config=hub_config,
        include_base_path_router=True,
    )

    return app
