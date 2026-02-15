import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Mount
from starlette.types import ASGIApp

from ...core.config import ConfigError, HubConfig, load_repo_config
from ...core.flows.failure_diagnostics import (
    format_failure_summary,
    get_failure_payload,
)
from ...core.flows.models import FlowRunStatus
from ...core.flows.reconciler import reconcile_flow_runs
from ...core.flows.store import FlowStore
from ...core.logging_utils import safe_log
from ...core.pma_context import (
    build_ticket_flow_run_state,
    get_latest_ticket_flow_run_state,
)
from ...core.request_context import get_request_id
from ...core.runtime import LockError
from ...core.state import persist_session_registry
from ...core.ticket_flow_summary import build_ticket_flow_summary
from ...core.utils import reset_repo_root_context, set_repo_root_context
from ...housekeeping import run_housekeeping_once
from ...tickets.files import safe_relpath
from ...tickets.models import Dispatch
from ...tickets.outbox import parse_dispatch, resolve_outbox_paths
from .app_factory import (
    CacheStaticFiles,
    resolve_allowed_hosts,
    resolve_auth_token,
)
from .app_state import (
    AppContext,
    ServerOverrides,
    _find_message_resolution,
    _latest_reply_history_seq,
    _load_hub_inbox_dismissals,
    _message_resolution_state,
    _message_resolvable_actions,
    _record_message_resolution,
    apply_app_context,
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
from .routes import build_repo_router
from .routes.filebox import build_hub_filebox_routes
from .routes.pma import build_pma_routes
from .routes.system import build_system_routes
from .schemas import (
    HubArchiveWorktreeRequest,
    HubArchiveWorktreeResponse,
    HubCleanupWorktreeRequest,
    HubCreateRepoRequest,
    HubCreateWorktreeRequest,
    HubJobResponse,
    HubRemoveRepoRequest,
    RunControlRequest,
)
from .static_assets import (
    index_response_headers,
    render_index_html,
)
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


def create_repo_app(
    repo_root: Path,
    server_overrides: Optional[ServerOverrides] = None,
    hub_config: Optional[HubConfig] = None,
) -> ASGIApp:
    # Hub-only: repo apps are always mounted under `/repos/<id>` and must not
    # apply their own base-path rewriting (the hub handles that globally).
    context = build_app_context(repo_root, base_path="", hub_config=hub_config)
    app = FastAPI(redirect_slashes=False, lifespan=_app_lifespan(context))

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
    app.add_middleware(
        HostOriginMiddleware,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

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
    if context.base_path:
        app.add_middleware(BasePathRouterMiddleware, base_path=context.base_path)
    app.add_middleware(
        HostOriginMiddleware,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    return app


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
    mounted_repos: set[str] = set()
    mount_errors: dict[str, str] = {}
    repo_apps: dict[str, ASGIApp] = {}
    repo_lifespans: dict[str, object] = {}
    mount_order: list[str] = []
    mount_lock: Optional[asyncio.Lock] = None

    async def _get_mount_lock() -> asyncio.Lock:
        nonlocal mount_lock
        if mount_lock is None:
            mount_lock = asyncio.Lock()
        return mount_lock

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

    def _unwrap_fastapi(sub_app: ASGIApp) -> Optional[FastAPI]:
        current: ASGIApp = sub_app
        while not isinstance(current, FastAPI):
            nested = getattr(current, "app", None)
            if nested is None:
                return None
            current = nested
        return current

    async def _start_repo_lifespan_locked(prefix: str, sub_app: ASGIApp) -> None:
        if prefix in repo_lifespans:
            return
        fastapi_app = _unwrap_fastapi(sub_app)
        if fastapi_app is None:
            return
        try:
            ctx = fastapi_app.router.lifespan_context(fastapi_app)
            await ctx.__aenter__()
            repo_lifespans[prefix] = ctx
            safe_log(
                app.state.logger,
                logging.INFO,
                f"Repo app lifespan entered for {prefix}",
            )
        except Exception as exc:
            mount_errors[prefix] = str(exc)
            try:
                app.state.logger.warning("Repo lifespan failed for %s: %s", prefix, exc)
            except Exception as exc2:
                safe_log(
                    app.state.logger,
                    logging.DEBUG,
                    f"Failed to log repo lifespan failure for {prefix}",
                    exc=exc2,
                )
            await _unmount_repo_locked(prefix)

    async def _stop_repo_lifespan_locked(prefix: str) -> None:
        ctx = repo_lifespans.pop(prefix, None)
        if ctx is None:
            return
        try:
            await ctx.__aexit__(None, None, None)
            safe_log(
                app.state.logger,
                logging.INFO,
                f"Repo app lifespan exited for {prefix}",
            )
        except Exception as exc:
            try:
                app.state.logger.warning(
                    "Repo lifespan shutdown failed for %s: %s", prefix, exc
                )
            except Exception as exc2:
                safe_log(
                    app.state.logger,
                    logging.DEBUG,
                    f"Failed to log repo lifespan shutdown failure for {prefix}",
                    exc=exc2,
                )

    def _detach_mount_locked(prefix: str) -> None:
        mount_path = f"/repos/{prefix}"
        app.router.routes = [
            route
            for route in app.router.routes
            if not (isinstance(route, Mount) and route.path == mount_path)
        ]
        mounted_repos.discard(prefix)
        repo_apps.pop(prefix, None)
        if prefix in mount_order:
            mount_order.remove(prefix)

    async def _unmount_repo_locked(prefix: str) -> None:
        await _stop_repo_lifespan_locked(prefix)
        _detach_mount_locked(prefix)

    def _mount_repo_sync(prefix: str, repo_path: Path) -> bool:
        if prefix in mounted_repos:
            return True
        if prefix in mount_errors:
            return False
        try:
            # Hub already handles the base path; avoid reapplying it in child apps.
            sub_app = create_repo_app(
                repo_path,
                server_overrides=repo_server_overrides,
                hub_config=context.config,
            )
        except ConfigError as exc:
            mount_errors[prefix] = str(exc)
            try:
                app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception as exc2:
                safe_log(
                    app.state.logger,
                    logging.DEBUG,
                    f"Failed to log mount error for {prefix}",
                    exc=exc2,
                )
            return False
        except Exception as exc:
            mount_errors[prefix] = str(exc)
            try:
                app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception as exc2:
                safe_log(
                    app.state.logger,
                    logging.DEBUG,
                    f"Failed to log mount error for {prefix}",
                    exc=exc2,
                )
            return False
        fastapi_app = _unwrap_fastapi(sub_app)
        if fastapi_app is not None:
            fastapi_app.state.repo_id = prefix
        app.mount(f"/repos/{prefix}", sub_app)
        mounted_repos.add(prefix)
        repo_apps[prefix] = sub_app
        if prefix not in mount_order:
            mount_order.append(prefix)
        mount_errors.pop(prefix, None)
        return True

    async def _refresh_mounts(snapshots, *, full_refresh: bool = True):
        desired = {
            snap.id for snap in snapshots if snap.initialized and snap.exists_on_disk
        }
        mount_lock = await _get_mount_lock()
        async with mount_lock:
            if full_refresh:
                for prefix in list(mounted_repos):
                    if prefix not in desired:
                        await _unmount_repo_locked(prefix)
                for prefix in list(mount_errors):
                    if prefix not in desired:
                        mount_errors.pop(prefix, None)
            for snap in snapshots:
                if snap.id not in desired:
                    continue
                if snap.id in mounted_repos or snap.id in mount_errors:
                    continue
                # Hub already handles the base path; avoid reapplying it in child apps.
                try:
                    sub_app = create_repo_app(
                        snap.path,
                        server_overrides=repo_server_overrides,
                        hub_config=context.config,
                    )
                except ConfigError as exc:
                    mount_errors[snap.id] = str(exc)
                    try:
                        app.state.logger.warning(
                            "Cannot mount repo %s: %s", snap.id, exc
                        )
                    except Exception as exc2:
                        safe_log(
                            app.state.logger,
                            logging.DEBUG,
                            f"Failed to log mount error for snapshot {snap.id}",
                            exc=exc2,
                        )
                    continue
                except Exception as exc:
                    mount_errors[snap.id] = str(exc)
                    try:
                        app.state.logger.warning(
                            "Cannot mount repo %s: %s", snap.id, exc
                        )
                    except Exception as exc2:
                        safe_log(
                            app.state.logger,
                            logging.DEBUG,
                            f"Failed to log mount error for snapshot {snap.id}",
                            exc=exc2,
                        )
                    continue
                fastapi_app = _unwrap_fastapi(sub_app)
                if fastapi_app is not None:
                    fastapi_app.state.repo_id = snap.id
                app.mount(f"/repos/{snap.id}", sub_app)
                mounted_repos.add(snap.id)
                repo_apps[snap.id] = sub_app
                if snap.id not in mount_order:
                    mount_order.append(snap.id)
                mount_errors.pop(snap.id, None)
                if app.state.hub_started:
                    await _start_repo_lifespan_locked(snap.id, sub_app)

    def _add_mount_info(repo_dict: dict) -> dict:
        """Add mount_status to repo dict for UI to know if navigation is possible."""
        repo_id = repo_dict.get("id")
        if repo_id in mount_errors:
            repo_dict["mounted"] = False
            repo_dict["mount_error"] = mount_errors[repo_id]
        elif repo_id in mounted_repos:
            repo_dict["mounted"] = True
        else:
            repo_dict["mounted"] = False
        return repo_dict

    def _get_ticket_flow_summary(repo_path: Path) -> Optional[dict]:
        """Get ticket flow summary for a repo (status, done/total, step).

        Returns None if no ticket flow exists or repo is not initialized.
        """
        return build_ticket_flow_summary(repo_path, include_failure=True)

    initial_snapshots = context.supervisor.scan()
    for snap in initial_snapshots:
        if snap.initialized and snap.exists_on_disk:
            _mount_repo_sync(snap.id, snap.path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
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

            asyncio.create_task(_housekeeping_loop())
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

            asyncio.create_task(_app_server_prune_loop())
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

            asyncio.create_task(_opencode_prune_loop())
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
        mount_lock = await _get_mount_lock()
        async with mount_lock:
            for prefix in list(mount_order):
                sub_app = repo_apps.get(prefix)
                if sub_app is not None:
                    await _start_repo_lifespan_locked(prefix, sub_app)
        try:
            yield
        finally:
            mount_lock = await _get_mount_lock()
            async with mount_lock:
                for prefix in list(reversed(mount_order)):
                    await _stop_repo_lifespan_locked(prefix)
                for prefix in list(mounted_repos):
                    _detach_mount_locked(prefix)
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

    hub_dismissal_locks: dict[str, asyncio.Lock] = {}
    hub_dismissal_locks_guard = asyncio.Lock()

    async def _repo_dismissal_lock(repo_root: Path) -> asyncio.Lock:
        key = str(repo_root.resolve())
        async with hub_dismissal_locks_guard:
            lock = hub_dismissal_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                hub_dismissal_locks[key] = lock
            return lock

    @app.get("/hub/messages")
    async def hub_messages(limit: int = 100):
        """Return paused ticket_flow dispatches across all repos.

        The hub inbox is intentionally simple: it surfaces the latest archived
        dispatch for each paused ticket_flow run.
        """

        def _latest_dispatch(
            repo_root: Path, run_id: str, input_data: dict
        ) -> Optional[dict]:
            try:
                workspace_root = Path(input_data.get("workspace_root") or repo_root)
                runs_dir = Path(input_data.get("runs_dir") or ".codex-autorunner/runs")
                outbox_paths = resolve_outbox_paths(
                    workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
                )
                history_dir = outbox_paths.dispatch_history_dir
                if not history_dir.exists() or not history_dir.is_dir():
                    return None

                def _dispatch_dict(dispatch: Dispatch) -> dict:
                    return {
                        "mode": dispatch.mode,
                        "title": dispatch.title,
                        "body": dispatch.body,
                        "extra": dispatch.extra,
                        "is_handoff": dispatch.is_handoff,
                    }

                def _list_files(dispatch_dir: Path) -> list[str]:
                    files: list[str] = []
                    for child in sorted(dispatch_dir.iterdir(), key=lambda p: p.name):
                        if child.name.startswith("."):
                            continue
                        if child.name == "DISPATCH.md":
                            continue
                        if child.is_file():
                            files.append(child.name)
                    return files

                seq_dirs: list[Path] = []
                for child in history_dir.iterdir():
                    if not child.is_dir():
                        continue
                    name = child.name
                    if len(name) == 4 and name.isdigit():
                        seq_dirs.append(child)
                if not seq_dirs:
                    return None

                seq_dirs = sorted(seq_dirs, key=lambda p: p.name, reverse=True)
                latest_seq = int(seq_dirs[0].name) if seq_dirs else None
                handoff_candidate: Optional[dict] = None
                non_summary_candidate: Optional[dict] = None
                turn_summary_candidate: Optional[dict] = None
                error_candidate: Optional[dict] = None

                for seq_dir in seq_dirs:
                    seq = int(seq_dir.name)
                    dispatch_path = seq_dir / "DISPATCH.md"
                    dispatch, errors = parse_dispatch(dispatch_path)
                    if errors or dispatch is None:
                        # Fail closed: when newest dispatch is unreadable, surface it
                        # instead of falling back to older dispatch entries.
                        if latest_seq is not None and seq == latest_seq:
                            return {
                                "seq": seq,
                                "dir": safe_relpath(seq_dir, repo_root),
                                "dispatch": None,
                                "errors": errors,
                                "files": [],
                            }
                        if error_candidate is None:
                            error_candidate = {
                                "seq": seq,
                                "dir": seq_dir,
                                "errors": errors,
                            }
                        continue
                    candidate = {"seq": seq, "dir": seq_dir, "dispatch": dispatch}
                    if dispatch.is_handoff and handoff_candidate is None:
                        handoff_candidate = candidate
                    if (
                        dispatch.mode != "turn_summary"
                        and non_summary_candidate is None
                    ):
                        non_summary_candidate = candidate
                    if (
                        dispatch.mode == "turn_summary"
                        and turn_summary_candidate is None
                    ):
                        turn_summary_candidate = candidate
                    if (
                        handoff_candidate
                        and non_summary_candidate
                        and turn_summary_candidate
                    ):
                        break

                selected = (
                    handoff_candidate or non_summary_candidate or turn_summary_candidate
                )
                if not selected:
                    if error_candidate:
                        return {
                            "seq": error_candidate["seq"],
                            "dir": safe_relpath(error_candidate["dir"], repo_root),
                            "dispatch": None,
                            "errors": error_candidate["errors"],
                            "files": [],
                        }
                    return None

                selected_dir = selected["dir"]
                dispatch = selected["dispatch"]
                result = {
                    "seq": selected["seq"],
                    "dir": safe_relpath(selected_dir, repo_root),
                    "dispatch": _dispatch_dict(dispatch),
                    "errors": [],
                    "files": _list_files(selected_dir),
                }
                if turn_summary_candidate is not None:
                    result["turn_summary_seq"] = turn_summary_candidate["seq"]
                    result["turn_summary"] = _dispatch_dict(
                        turn_summary_candidate["dispatch"]
                    )
                return result
            except Exception:
                return None

        def _gather() -> list[dict]:
            messages: list[dict] = []
            try:
                snapshots = context.supervisor.list_repos()
            except Exception:
                return []
            for snap in snapshots:
                if not (snap.initialized and snap.exists_on_disk):
                    continue
                dismissals = _load_hub_inbox_dismissals(snap.path)
                repo_root = snap.path
                db_path = repo_root / ".codex-autorunner" / "flows.db"
                if not db_path.exists():
                    continue
                try:
                    config = load_repo_config(repo_root)
                    with FlowStore(db_path, durable=config.durable_writes) as store:
                        active_statuses = [
                            FlowRunStatus.PAUSED,
                            FlowRunStatus.RUNNING,
                            FlowRunStatus.FAILED,
                            FlowRunStatus.STOPPED,
                        ]
                        all_runs = store.list_flow_runs(flow_type="ticket_flow")
                except Exception:
                    continue
                for record in all_runs:
                    if record.status not in active_statuses:
                        continue
                    record_input = dict(record.input_data or {})
                    latest = _latest_dispatch(repo_root, str(record.id), record_input)
                    seq = int(latest.get("seq") or 0) if isinstance(latest, dict) else 0
                    latest_reply_seq = _latest_reply_history_seq(
                        repo_root, str(record.id), record_input
                    )
                    has_pending_dispatch = bool(
                        latest
                        and latest.get("dispatch")
                        and seq > 0
                        and latest_reply_seq < seq
                    )

                    dispatch_state_reason = None
                    if (
                        record.status == FlowRunStatus.PAUSED
                        and not has_pending_dispatch
                    ):
                        if latest and latest.get("errors"):
                            dispatch_state_reason = (
                                "Paused run has unreadable dispatch metadata"
                            )
                        elif seq > 0 and latest_reply_seq >= seq:
                            dispatch_state_reason = (
                                "Latest dispatch already replied; run is still paused"
                            )
                        else:
                            dispatch_state_reason = (
                                "Run is paused without an actionable dispatch"
                            )
                    elif record.status == FlowRunStatus.FAILED:
                        dispatch_state_reason = record.error_message or "Run failed"
                    elif record.status == FlowRunStatus.STOPPED:
                        dispatch_state_reason = "Run was stopped"

                    run_state = build_ticket_flow_run_state(
                        repo_root=repo_root,
                        repo_id=snap.id,
                        record=record,
                        store=store,
                        has_pending_dispatch=has_pending_dispatch,
                        dispatch_state_reason=dispatch_state_reason,
                    )

                    is_terminal_failed = record.status in (
                        FlowRunStatus.FAILED,
                        FlowRunStatus.STOPPED,
                    )
                    if (
                        not run_state.get("attention_required")
                        and not is_terminal_failed
                    ):
                        if has_pending_dispatch:
                            pass
                        else:
                            continue

                    failure_payload = get_failure_payload(record)
                    failure_summary = (
                        format_failure_summary(failure_payload)
                        if failure_payload
                        else None
                    )
                    base_item = {
                        "repo_id": snap.id,
                        "repo_display_name": snap.display_name,
                        "repo_path": str(snap.path),
                        "run_id": record.id,
                        "run_created_at": record.created_at,
                        "status": record.status.value,
                        "failure": failure_payload,
                        "failure_summary": failure_summary,
                        "open_url": f"/repos/{snap.id}/?tab=inbox&run_id={record.id}",
                        "run_state": run_state,
                    }
                    item_payload: dict[str, Any]
                    if has_pending_dispatch:
                        item_payload = {
                            **base_item,
                            "item_type": "run_dispatch",
                            "next_action": "reply_and_resume",
                            "seq": latest["seq"],
                            "dispatch": latest["dispatch"],
                            "message": latest["dispatch"],
                            "files": latest.get("files") or [],
                        }
                    else:
                        fallback_dispatch = latest.get("dispatch") if latest else None
                        item_type = "run_state_attention"
                        next_action = "inspect_and_resume"
                        if record.status == FlowRunStatus.FAILED:
                            item_type = "run_failed"
                            next_action = "diagnose_or_restart"
                        elif record.status == FlowRunStatus.STOPPED:
                            item_type = "run_stopped"
                            next_action = "diagnose_or_restart"
                        item_payload = {
                            **base_item,
                            "item_type": item_type,
                            "next_action": next_action,
                            "seq": seq if seq > 0 else None,
                            "dispatch": fallback_dispatch,
                            "message": fallback_dispatch
                            or {
                                "title": "Run requires attention",
                                "body": dispatch_state_reason or "",
                            },
                            "files": latest.get("files") if latest else [],
                            "reason": dispatch_state_reason,
                            "available_actions": run_state.get(
                                "recommended_actions", []
                            ),
                        }

                    item_type = str(item_payload.get("item_type") or "run_dispatch")
                    item_seq_raw = item_payload.get("seq")
                    item_seq = (
                        int(item_seq_raw)
                        if isinstance(item_seq_raw, int)
                        or (
                            isinstance(item_seq_raw, str)
                            and item_seq_raw.isdigit()
                            and int(item_seq_raw) > 0
                        )
                        else None
                    )
                    if _find_message_resolution(
                        dismissals,
                        run_id=str(record.id),
                        item_type=item_type,
                        seq=item_seq,
                    ):
                        continue

                    item_payload["resolution_state"] = _message_resolution_state(
                        item_type
                    )
                    item_payload["resolvable_actions"] = _message_resolvable_actions(
                        item_type
                    )
                    messages.append(item_payload)
            messages.sort(key=lambda m: m.get("run_created_at") or "", reverse=True)
            if limit and limit > 0:
                return messages[: int(limit)]
            return messages

        items = await asyncio.to_thread(_gather)
        return {"items": items}

    @app.post("/hub/messages/dismiss")
    async def dismiss_hub_message(payload: dict[str, Any]):
        repo_id = str(payload.get("repo_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        seq_raw = payload.get("seq")
        reason_raw = payload.get("reason")
        reason = str(reason_raw).strip() if isinstance(reason_raw, str) else ""
        if not repo_id:
            raise HTTPException(status_code=400, detail="Missing repo_id")
        if not run_id:
            raise HTTPException(status_code=400, detail="Missing run_id")
        try:
            seq = int(seq_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid seq") from None
        if seq <= 0:
            raise HTTPException(status_code=400, detail="Invalid seq")

        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        snapshot = next((s for s in snapshots if s.id == repo_id), None)
        if snapshot is None or not snapshot.exists_on_disk:
            raise HTTPException(status_code=404, detail="Repo not found")

        repo_lock = await _repo_dismissal_lock(snapshot.path)
        async with repo_lock:
            dismissed = _record_message_resolution(
                repo_root=snapshot.path,
                repo_id=repo_id,
                run_id=run_id,
                item_type="run_dispatch",
                seq=seq,
                action="dismiss",
                reason=reason or None,
                actor="hub_messages_dismiss",
            )
            dismissed_at = str(dismissed.get("resolved_at") or "")
            dismissed["dismissed_at"] = dismissed_at
        return {
            "status": "ok",
            "dismissed": dismissed,
        }

    @app.post("/hub/messages/resolve")
    async def resolve_hub_message(payload: dict[str, Any]):
        repo_id = str(payload.get("repo_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        item_type = str(payload.get("item_type") or "").strip()
        action = str(payload.get("action") or "dismiss").strip() or "dismiss"
        reason_raw = payload.get("reason")
        actor_raw = payload.get("actor")
        reason = str(reason_raw).strip() if isinstance(reason_raw, str) else ""
        actor = str(actor_raw).strip() if isinstance(actor_raw, str) else ""
        seq_raw = payload.get("seq")
        seq: Optional[int] = None
        if seq_raw is not None and seq_raw != "":
            try:
                parsed = int(seq_raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Invalid seq") from None
            if parsed <= 0:
                raise HTTPException(status_code=400, detail="Invalid seq")
            seq = parsed

        if not repo_id:
            raise HTTPException(status_code=400, detail="Missing repo_id")
        if not run_id:
            raise HTTPException(status_code=400, detail="Missing run_id")
        if action not in {"dismiss"}:
            raise HTTPException(status_code=400, detail="Unsupported action")

        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        snapshot = next((s for s in snapshots if s.id == repo_id), None)
        if snapshot is None or not snapshot.exists_on_disk:
            raise HTTPException(status_code=404, detail="Repo not found")

        if not item_type:
            hub_payload = await hub_messages(limit=2000)
            items_raw = (
                hub_payload.get("items", []) if isinstance(hub_payload, dict) else []
            )
            matched = None
            for item in items_raw if isinstance(items_raw, list) else []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("repo_id") or "") != repo_id:
                    continue
                if str(item.get("run_id") or "") != run_id:
                    continue
                candidate_seq = item.get("seq")
                if seq is not None and candidate_seq != seq:
                    continue
                matched = item
                break
            if matched is None:
                raise HTTPException(status_code=404, detail="Hub message not found")
            item_type = str(matched.get("item_type") or "").strip() or "run_dispatch"
            if seq is None:
                matched_seq = matched.get("seq")
                if isinstance(matched_seq, int) and matched_seq > 0:
                    seq = matched_seq
        elif item_type == "run_dispatch" and seq is None:
            raise HTTPException(status_code=400, detail="Missing seq for run_dispatch")

        repo_lock = await _repo_dismissal_lock(snapshot.path)
        async with repo_lock:
            resolved = _record_message_resolution(
                repo_root=snapshot.path,
                repo_id=repo_id,
                run_id=run_id,
                item_type=item_type,
                seq=seq,
                action=action,
                reason=reason or None,
                actor=actor or "hub_messages_resolve",
            )
        return {"status": "ok", "resolved": resolved}

    @app.get("/hub/repos")
    async def list_repos():
        safe_log(app.state.logger, logging.INFO, "Hub list_repos")
        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        await _refresh_mounts(snapshots)

        def _enrich_repo(snap):
            repo_dict = _add_mount_info(snap.to_dict(context.config.root))
            if snap.initialized and snap.exists_on_disk:
                repo_dict["ticket_flow"] = _get_ticket_flow_summary(snap.path)
                repo_dict["run_state"] = get_latest_ticket_flow_run_state(
                    snap.path, snap.id
                )
            else:
                repo_dict["ticket_flow"] = None
                repo_dict["run_state"] = None
            return repo_dict

        return {
            "last_scan_at": context.supervisor.state.last_scan_at,
            "repos": [_enrich_repo(snap) for snap in snapshots],
        }

    @app.post("/hub/repos/scan")
    async def scan_repos():
        safe_log(app.state.logger, logging.INFO, "Hub scan_repos")
        snapshots = await asyncio.to_thread(context.supervisor.scan)
        await _refresh_mounts(snapshots)

        def _enrich_repo(snap):
            repo_dict = _add_mount_info(snap.to_dict(context.config.root))
            if snap.initialized and snap.exists_on_disk:
                repo_dict["ticket_flow"] = _get_ticket_flow_summary(snap.path)
                repo_dict["run_state"] = get_latest_ticket_flow_run_state(
                    snap.path, snap.id
                )
            else:
                repo_dict["ticket_flow"] = None
                repo_dict["run_state"] = None
            return repo_dict

        return {
            "last_scan_at": context.supervisor.state.last_scan_at,
            "repos": [_enrich_repo(snap) for snap in snapshots],
        }

    @app.post("/hub/jobs/scan", response_model=HubJobResponse)
    async def scan_repos_job():
        async def _run_scan():
            snapshots = await asyncio.to_thread(context.supervisor.scan)
            await _refresh_mounts(snapshots)
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.scan_repos", _run_scan, request_id=get_request_id()
        )
        return job.to_dict()

    @app.post("/hub/repos")
    async def create_repo(payload: HubCreateRepoRequest):
        git_url = payload.git_url
        repo_id = payload.repo_id
        if not repo_id and not git_url:
            raise HTTPException(status_code=400, detail="Missing repo id")
        repo_path_val = payload.path
        repo_path = Path(repo_path_val) if repo_path_val else None
        git_init = payload.git_init
        force = payload.force
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub create repo id=%s path=%s git_init=%s force=%s git_url=%s"
            % (repo_id, repo_path_val, git_init, force, bool(git_url)),
        )
        try:
            if git_url:
                snapshot = await asyncio.to_thread(
                    context.supervisor.clone_repo,
                    git_url=str(git_url),
                    repo_id=str(repo_id) if repo_id else None,
                    repo_path=repo_path,
                    force=force,
                )
            else:
                snapshot = await asyncio.to_thread(
                    context.supervisor.create_repo,
                    str(repo_id),
                    repo_path=repo_path,
                    git_init=git_init,
                    force=force,
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _refresh_mounts([snapshot], full_refresh=False)
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/jobs/repos", response_model=HubJobResponse)
    async def create_repo_job(payload: HubCreateRepoRequest):
        async def _run_create_repo():
            git_url = payload.git_url
            repo_id = payload.repo_id
            if not repo_id and not git_url:
                raise ValueError("Missing repo id")
            repo_path_val = payload.path
            repo_path = Path(repo_path_val) if repo_path_val else None
            git_init = payload.git_init
            force = payload.force
            if git_url:
                snapshot = await asyncio.to_thread(
                    context.supervisor.clone_repo,
                    git_url=str(git_url),
                    repo_id=str(repo_id) if repo_id else None,
                    repo_path=repo_path,
                    force=force,
                )
            else:
                snapshot = await asyncio.to_thread(
                    context.supervisor.create_repo,
                    str(repo_id),
                    repo_path=repo_path,
                    git_init=git_init,
                    force=force,
                )
            await _refresh_mounts([snapshot], full_refresh=False)
            return _add_mount_info(snapshot.to_dict(context.config.root))

        job = await context.job_manager.submit(
            "hub.create_repo", _run_create_repo, request_id=get_request_id()
        )
        return job.to_dict()

    @app.post("/hub/repos/{repo_id}/worktree-setup")
    async def set_worktree_setup(repo_id: str, payload: Dict[str, Any]):
        commands_raw = payload.get("commands") if isinstance(payload, dict) else []
        if not isinstance(commands_raw, list):
            raise HTTPException(status_code=400, detail="commands must be a list")
        commands = [str(item) for item in commands_raw if str(item).strip()]
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub set worktree setup repo=%s commands=%d" % (repo_id, len(commands)),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.set_worktree_setup_commands,
                repo_id,
                commands,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _refresh_mounts([snapshot], full_refresh=False)
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.get("/hub/repos/{repo_id}/remove-check")
    async def remove_repo_check(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub remove-check {repo_id}")
        try:
            return await asyncio.to_thread(
                context.supervisor.check_repo_removal, repo_id
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/hub/repos/{repo_id}/remove")
    async def remove_repo(repo_id: str, payload: Optional[HubRemoveRepoRequest] = None):
        payload = payload or HubRemoveRepoRequest()
        force = payload.force
        delete_dir = payload.delete_dir
        delete_worktrees = payload.delete_worktrees
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub remove repo id=%s force=%s delete_dir=%s delete_worktrees=%s"
            % (repo_id, force, delete_dir, delete_worktrees),
        )
        try:
            await asyncio.to_thread(
                context.supervisor.remove_repo,
                repo_id,
                force=force,
                delete_dir=delete_dir,
                delete_worktrees=delete_worktrees,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snapshots = await asyncio.to_thread(
            context.supervisor.list_repos, use_cache=False
        )
        await _refresh_mounts(snapshots)
        return {"status": "ok"}

    @app.post("/hub/jobs/repos/{repo_id}/remove", response_model=HubJobResponse)
    async def remove_repo_job(
        repo_id: str, payload: Optional[HubRemoveRepoRequest] = None
    ):
        payload = payload or HubRemoveRepoRequest()

        async def _run_remove_repo():
            await asyncio.to_thread(
                context.supervisor.remove_repo,
                repo_id,
                force=payload.force,
                delete_dir=payload.delete_dir,
                delete_worktrees=payload.delete_worktrees,
            )
            snapshots = await asyncio.to_thread(
                context.supervisor.list_repos, use_cache=False
            )
            await _refresh_mounts(snapshots)
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.remove_repo", _run_remove_repo, request_id=get_request_id()
        )
        return job.to_dict()

    @app.post("/hub/worktrees/create")
    async def create_worktree(payload: HubCreateWorktreeRequest):
        base_repo_id = payload.base_repo_id
        branch = payload.branch
        force = payload.force
        start_point = payload.start_point
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub create worktree base=%s branch=%s force=%s start_point=%s"
            % (base_repo_id, branch, force, start_point),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.create_worktree,
                base_repo_id=str(base_repo_id),
                branch=str(branch),
                force=force,
                start_point=str(start_point) if start_point else None,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _refresh_mounts([snapshot], full_refresh=False)
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/jobs/worktrees/create", response_model=HubJobResponse)
    async def create_worktree_job(payload: HubCreateWorktreeRequest):
        async def _run_create_worktree():
            snapshot = await asyncio.to_thread(
                context.supervisor.create_worktree,
                base_repo_id=str(payload.base_repo_id),
                branch=str(payload.branch),
                force=payload.force,
                start_point=str(payload.start_point) if payload.start_point else None,
            )
            await _refresh_mounts([snapshot], full_refresh=False)
            return _add_mount_info(snapshot.to_dict(context.config.root))

        job = await context.job_manager.submit(
            "hub.create_worktree", _run_create_worktree, request_id=get_request_id()
        )
        return job.to_dict()

    @app.post("/hub/worktrees/cleanup")
    async def cleanup_worktree(payload: HubCleanupWorktreeRequest):
        worktree_repo_id = payload.worktree_repo_id
        delete_branch = payload.delete_branch
        delete_remote = payload.delete_remote
        archive = payload.archive
        force_archive = payload.force_archive
        archive_note = payload.archive_note
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub cleanup worktree id=%s delete_branch=%s delete_remote=%s archive=%s force_archive=%s"
            % (
                worktree_repo_id,
                delete_branch,
                delete_remote,
                archive,
                force_archive,
            ),
        )
        try:
            await asyncio.to_thread(
                context.supervisor.cleanup_worktree,
                worktree_repo_id=str(worktree_repo_id),
                delete_branch=delete_branch,
                delete_remote=delete_remote,
                archive=archive,
                force_archive=force_archive,
                archive_note=archive_note,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok"}

    @app.post("/hub/jobs/worktrees/cleanup", response_model=HubJobResponse)
    async def cleanup_worktree_job(payload: HubCleanupWorktreeRequest):
        def _run_cleanup_worktree():
            context.supervisor.cleanup_worktree(
                worktree_repo_id=str(payload.worktree_repo_id),
                delete_branch=payload.delete_branch,
                delete_remote=payload.delete_remote,
                archive=payload.archive,
                force_archive=payload.force_archive,
                archive_note=payload.archive_note,
            )
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.cleanup_worktree", _run_cleanup_worktree, request_id=get_request_id()
        )
        return job.to_dict()

    @app.post("/hub/worktrees/archive", response_model=HubArchiveWorktreeResponse)
    async def archive_worktree(payload: HubArchiveWorktreeRequest):
        worktree_repo_id = payload.worktree_repo_id
        archive_note = payload.archive_note
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub archive worktree id=%s" % (worktree_repo_id,),
        )
        try:
            result = await asyncio.to_thread(
                context.supervisor.archive_worktree,
                worktree_repo_id=str(worktree_repo_id),
                archive_note=archive_note,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @app.post("/hub/repos/{repo_id}/run")
    async def run_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub run %s once=%s" % (repo_id, once),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.run_repo, repo_id, once=once
            )
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _refresh_mounts([snapshot], full_refresh=False)
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/repos/{repo_id}/stop")
    async def stop_repo(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub stop {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.stop_repo, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/repos/{repo_id}/resume")
    async def resume_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub resume %s once=%s" % (repo_id, once),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.resume_repo, repo_id, once=once
            )
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _refresh_mounts([snapshot], full_refresh=False)
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/repos/{repo_id}/kill")
    async def kill_repo(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub kill {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.kill_repo, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/repos/{repo_id}/init")
    async def init_repo(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub init {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.init_repo, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _refresh_mounts([snapshot], full_refresh=False)
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/repos/{repo_id}/sync-main")
    async def sync_repo_main(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub sync main {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.sync_main, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _refresh_mounts([snapshot], full_refresh=False)
        return _add_mount_info(snapshot.to_dict(context.config.root))

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
