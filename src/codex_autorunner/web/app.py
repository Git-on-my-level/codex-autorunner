import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI,
    HTTPException,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp

from ..api_routes import build_repo_router
from ..core.config import ConfigError, HubConfig, _normalize_base_path, load_config
from ..core.doc_chat import DocChatService
from ..core.engine import Engine, LockError
from ..core.hub import HubSupervisor
from ..core.logging_utils import safe_log, setup_rotating_logger
from ..core.optional_dependencies import require_optional_dependencies
from ..core.state import load_state, persist_session_registry
from ..core.usage import (
    UsageError,
    default_codex_home,
    get_hub_usage_series_cached,
    parse_iso_datetime,
    summarize_hub_usage,
)
from ..manifest import load_manifest
from ..housekeeping import run_housekeeping_once
from ..routes.system import build_system_routes
from ..voice import VoiceConfig, VoiceService
from .middleware import AuthTokenMiddleware, BasePathRouterMiddleware
from .runner_manager import RunnerManager
from .schemas import (
    HubCleanupWorktreeRequest,
    HubCreateRepoRequest,
    HubCreateWorktreeRequest,
    HubRemoveRepoRequest,
    RunControlRequest,
)
from .static_assets import (
    asset_version,
    index_response_headers,
    render_index_html,
    require_static_assets,
    resolve_static_dir,
)
from .terminal_sessions import parse_tui_idle_seconds, prune_terminal_registry


@dataclass(frozen=True)
class AppContext:
    base_path: str
    engine: Engine
    manager: RunnerManager
    doc_chat: DocChatService
    voice_config: VoiceConfig
    voice_missing_reason: Optional[str]
    voice_service: Optional[VoiceService]
    terminal_sessions: dict
    terminal_max_idle_seconds: Optional[float]
    terminal_lock: asyncio.Lock
    session_registry: dict
    repo_to_session: dict
    session_state_last_write: float
    session_state_dirty: bool
    static_dir: Path
    static_assets_context: Optional[object]
    asset_version: str
    logger: logging.Logger
    tui_idle_seconds: Optional[float]
    tui_idle_check_seconds: Optional[float]


@dataclass(frozen=True)
class HubAppContext:
    base_path: str
    config: HubConfig
    supervisor: HubSupervisor
    static_dir: Path
    static_assets_context: Optional[object]
    asset_version: str
    logger: logging.Logger


def _build_app_context(
    repo_root: Optional[Path], base_path: Optional[str]
) -> AppContext:
    config = load_config(repo_root or Path.cwd())
    if isinstance(config, HubConfig):
        raise ConfigError("create_app requires repo mode configuration")
    normalized_base = (
        _normalize_base_path(base_path)
        if base_path is not None
        else config.server_base_path
    )
    engine = Engine(config.root)
    manager = RunnerManager(engine)
    doc_chat = DocChatService(engine)
    voice_config = VoiceConfig.from_raw(config.voice, env=os.environ)
    voice_missing_reason: Optional[str] = None
    try:
        require_optional_dependencies(
            feature="voice",
            deps=(
                ("httpx", "httpx"),
                ("multipart", "python-multipart"),
            ),
            extra="voice",
        )
    except ConfigError as exc:
        voice_missing_reason = str(exc)
        voice_config.enabled = False
    terminal_max_idle_seconds = config.terminal_idle_timeout_seconds
    if terminal_max_idle_seconds is not None and terminal_max_idle_seconds <= 0:
        terminal_max_idle_seconds = None
    tui_idle_seconds = parse_tui_idle_seconds(config)
    tui_idle_check_seconds: Optional[float] = None
    if tui_idle_seconds is not None:
        tui_idle_check_seconds = min(10.0, max(1.0, tui_idle_seconds / 4))
    # Construct asyncio primitives without assuming a loop already exists.
    # This comes up in unit tests (sync context) and when mounting from a worker thread.
    try:
        terminal_lock = asyncio.Lock()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
        terminal_lock = asyncio.Lock()
    logger = setup_rotating_logger(
        f"repo[{engine.repo_root}]", engine.config.server_log
    )
    engine.notifier.set_logger(logger)
    safe_log(
        logger,
        logging.INFO,
        f"Repo server ready at {engine.repo_root}",
    )
    voice_service: Optional[VoiceService]
    if voice_missing_reason:
        voice_service = None
        safe_log(
            logger,
            logging.WARNING,
            voice_missing_reason,
        )
    else:
        try:
            voice_service = VoiceService(voice_config, logger=logger)
        except Exception as exc:
            voice_service = None
            safe_log(
                logger,
                logging.WARNING,
                "Voice service unavailable",
                exc,
            )
    session_registry: dict = {}
    repo_to_session: dict = {}
    initial_state = load_state(engine.state_path)
    session_registry = dict(initial_state.sessions)
    repo_to_session = dict(initial_state.repo_to_session)
    terminal_sessions: dict = {}
    if session_registry or repo_to_session:
        prune_terminal_registry(
            engine.state_path,
            terminal_sessions,
            session_registry,
            repo_to_session,
            terminal_max_idle_seconds,
        )
    static_dir, static_context = resolve_static_dir()
    try:
        require_static_assets(static_dir, logger)
    except Exception:
        if static_context is not None:
            static_context.close()
        raise
    return AppContext(
        base_path=normalized_base,
        engine=engine,
        manager=manager,
        doc_chat=doc_chat,
        voice_config=voice_config,
        voice_missing_reason=voice_missing_reason,
        voice_service=voice_service,
        terminal_sessions=terminal_sessions,
        terminal_max_idle_seconds=terminal_max_idle_seconds,
        terminal_lock=terminal_lock,
        session_registry=session_registry,
        repo_to_session=repo_to_session,
        session_state_last_write=0.0,
        session_state_dirty=False,
        static_dir=static_dir,
        static_assets_context=static_context,
        asset_version=asset_version(static_dir),
        logger=logger,
        tui_idle_seconds=tui_idle_seconds,
        tui_idle_check_seconds=tui_idle_check_seconds,
    )


def _apply_app_context(app: FastAPI, context: AppContext) -> None:
    app.state.base_path = context.base_path
    app.state.logger = context.logger
    app.state.engine = context.engine
    app.state.config = context.engine.config  # Expose config consistently
    app.state.manager = context.manager
    app.state.doc_chat = context.doc_chat
    app.state.voice_config = context.voice_config
    app.state.voice_missing_reason = context.voice_missing_reason
    app.state.voice_service = context.voice_service
    app.state.terminal_sessions = context.terminal_sessions
    app.state.terminal_max_idle_seconds = context.terminal_max_idle_seconds
    app.state.terminal_lock = context.terminal_lock
    app.state.session_registry = context.session_registry
    app.state.repo_to_session = context.repo_to_session
    app.state.session_state_last_write = context.session_state_last_write
    app.state.session_state_dirty = context.session_state_dirty
    app.state.static_dir = context.static_dir
    app.state.static_assets_context = context.static_assets_context
    app.state.asset_version = context.asset_version


def _build_hub_context(
    hub_root: Optional[Path], base_path: Optional[str]
) -> HubAppContext:
    config = load_config(hub_root or Path.cwd())
    if not isinstance(config, HubConfig):
        raise ConfigError("Hub app requires hub mode configuration")
    normalized_base = (
        _normalize_base_path(base_path)
        if base_path is not None
        else config.server_base_path
    )
    supervisor = HubSupervisor(config)
    logger = setup_rotating_logger(f"hub[{config.root}]", config.server_log)
    safe_log(
        logger,
        logging.INFO,
        f"Hub app ready at {config.root}",
    )
    static_dir, static_context = resolve_static_dir()
    try:
        require_static_assets(static_dir, logger)
    except Exception:
        if static_context is not None:
            static_context.close()
        raise
    return HubAppContext(
        base_path=normalized_base,
        config=config,
        supervisor=supervisor,
        static_dir=static_dir,
        static_assets_context=static_context,
        asset_version=asset_version(static_dir),
        logger=logger,
    )


def _apply_hub_context(app: FastAPI, context: HubAppContext) -> None:
    app.state.base_path = context.base_path
    app.state.logger = context.logger
    app.state.config = context.config  # Expose config for route modules
    app.state.static_dir = context.static_dir
    app.state.static_assets_context = context.static_assets_context
    app.state.asset_version = context.asset_version


def _app_lifespan(context: AppContext):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def _cleanup_loop():
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

        async def _housekeeping_loop():
            config = app.state.config.housekeeping
            interval = max(config.interval_seconds, 1)
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

        asyncio.create_task(_cleanup_loop())
        if app.state.config.housekeeping.enabled:
            asyncio.create_task(_housekeeping_loop())

        if (
            context.tui_idle_seconds is not None
            and context.tui_idle_check_seconds is not None
        ):

            async def _tui_idle_loop():
                while True:
                    await asyncio.sleep(context.tui_idle_check_seconds)
                    try:
                        async with app.state.terminal_lock:
                            terminal_sessions = app.state.terminal_sessions
                            session_registry = app.state.session_registry
                            for session_id, session in list(terminal_sessions.items()):
                                if not session.pty.isalive():
                                    continue
                                if not session.should_notify_idle(
                                    context.tui_idle_seconds
                                ):
                                    continue
                                record = session_registry.get(session_id)
                                repo_path = record.repo_path if record else None
                                notifier = getattr(app.state.engine, "notifier", None)
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

            asyncio.create_task(_tui_idle_loop())

        try:
            yield
        finally:
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
            static_context = getattr(app.state, "static_assets_context", None)
            if static_context is not None:
                static_context.close()

    return lifespan


def create_app(
    repo_root: Optional[Path] = None, base_path: Optional[str] = None
) -> ASGIApp:
    context = _build_app_context(repo_root, base_path)
    app = FastAPI(redirect_slashes=False, lifespan=_app_lifespan(context))
    _apply_app_context(app, context)
    app.mount("/static", StaticFiles(directory=context.static_dir), name="static")
    # Route handlers
    app.include_router(build_repo_router(context.static_dir))

    auth_token = _resolve_auth_token(context.engine.config.server_auth_token_env)
    asgi_app: ASGIApp = app
    if auth_token:
        asgi_app = AuthTokenMiddleware(asgi_app, auth_token, context.base_path)
    if context.base_path:
        asgi_app = BasePathRouterMiddleware(asgi_app, context.base_path)

    return asgi_app


def create_hub_app(
    hub_root: Optional[Path] = None, base_path: Optional[str] = None
) -> ASGIApp:
    context = _build_hub_context(hub_root, base_path)
    app = FastAPI(redirect_slashes=False)
    _apply_hub_context(app, context)
    app.mount("/static", StaticFiles(directory=context.static_dir), name="static")
    mounted_repos: set[str] = set()
    mount_errors: dict[str, str] = {}
    repo_apps: dict[str, ASGIApp] = {}
    repo_startup_complete: set[str] = set()
    app.state.hub_started = False

    def _unwrap_fastapi(sub_app: ASGIApp) -> Optional[FastAPI]:
        current: ASGIApp = sub_app
        while not isinstance(current, FastAPI):
            nested = getattr(current, "app", None)
            if nested is None:
                return None
            current = nested
        return current

    async def _start_repo_app(prefix: str, sub_app: ASGIApp) -> None:
        if prefix in repo_startup_complete:
            return
        fastapi_app = _unwrap_fastapi(sub_app)
        if fastapi_app is None:
            return
        try:
            await fastapi_app.router.startup()
            repo_startup_complete.add(prefix)
            safe_log(
                app.state.logger,
                logging.INFO,
                f"Repo app startup complete for {prefix}",
            )
        except Exception as exc:
            try:
                app.state.logger.warning("Repo startup failed for %s: %s", prefix, exc)
            except Exception:
                pass

    def _mount_repo(prefix: str, repo_path: Path) -> bool:
        if prefix in mounted_repos:
            return True
        if prefix in mount_errors:
            return False
        try:
            # Hub already handles the base path; avoid reapplying it in child apps.
            sub_app = create_app(repo_path, base_path="")
        except ConfigError as exc:
            mount_errors[prefix] = str(exc)
            try:
                app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception:
                pass
            return False
        except Exception as exc:
            mount_errors[prefix] = str(exc)
            try:
                app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception:
                pass
            return False
        app.mount(f"/repos/{prefix}", sub_app)
        mounted_repos.add(prefix)
        repo_apps[prefix] = sub_app
        mount_errors.pop(prefix, None)
        if app.state.hub_started:
            try:
                asyncio.create_task(_start_repo_app(prefix, sub_app))
            except RuntimeError:
                pass
        return True

    def _refresh_mounts(snapshots):
        for snap in snapshots:
            if snap.initialized and snap.exists_on_disk:
                _mount_repo(snap.id, snap.path)

    def _add_mount_info(repo_dict: dict) -> dict:
        """Add mount_status to repo dict for UI to know if navigation is possible."""
        repo_id = repo_dict.get("id")
        if repo_id in mounted_repos:
            repo_dict["mounted"] = True
        elif repo_id in mount_errors:
            repo_dict["mounted"] = False
            repo_dict["mount_error"] = mount_errors[repo_id]
        else:
            repo_dict["mounted"] = False
        return repo_dict

    initial_snapshots = context.supervisor.scan()
    _refresh_mounts(initial_snapshots)

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
        for prefix, sub_app in list(repo_apps.items()):
            await _start_repo_app(prefix, sub_app)
        try:
            yield
        finally:
            for prefix, sub_app in list(repo_apps.items()):
                if prefix not in repo_startup_complete:
                    continue
                try:
                    await sub_app.router.shutdown()
                except Exception as exc:
                    try:
                        app.state.logger.warning(
                            "Repo shutdown failed for %s: %s", prefix, exc
                        )
                    except Exception:
                        pass
            static_context = getattr(app.state, "static_assets_context", None)
            if static_context is not None:
                static_context.close()

    app.router.lifespan_context = lifespan

    @app.get("/hub/usage")
    def hub_usage(since: Optional[str] = None, until: Optional[str] = None):
        try:
            since_dt = parse_iso_datetime(since)
            until_dt = parse_iso_datetime(until)
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        manifest = load_manifest(context.config.manifest_path, context.config.root)
        repo_map = [
            (repo.id, (context.config.root / repo.path)) for repo in manifest.repos
        ]
        per_repo, unmatched = summarize_hub_usage(
            repo_map,
            default_codex_home(),
            since=since_dt,
            until=until_dt,
        )
        return {
            "mode": "hub",
            "hub_root": str(context.config.root),
            "codex_home": str(default_codex_home()),
            "since": since,
            "until": until,
            "repos": [
                {
                    "id": repo_id,
                    "events": summary.events,
                    "totals": summary.totals.to_dict(),
                    "latest_rate_limits": summary.latest_rate_limits,
                }
                for repo_id, summary in per_repo.items()
            ],
            "unmatched": unmatched.to_dict(),
        }

    @app.get("/hub/usage/series")
    def hub_usage_series(
        since: Optional[str] = None,
        until: Optional[str] = None,
        bucket: str = "day",
        segment: str = "none",
    ):
        try:
            since_dt = parse_iso_datetime(since)
            until_dt = parse_iso_datetime(until)
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        manifest = load_manifest(context.config.manifest_path, context.config.root)
        repo_map = [
            (repo.id, (context.config.root / repo.path)) for repo in manifest.repos
        ]
        try:
            series, status = get_hub_usage_series_cached(
                repo_map,
                default_codex_home(),
                since=since_dt,
                until=until_dt,
                bucket=bucket,
                segment=segment,
            )
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "mode": "hub",
            "hub_root": str(context.config.root),
            "codex_home": str(default_codex_home()),
            "since": since,
            "until": until,
            "status": status,
            **series,
        }

    @app.get("/hub/repos")
    async def list_repos():
        safe_log(app.state.logger, logging.INFO, "Hub list_repos")
        snapshots = context.supervisor.list_repos()
        _refresh_mounts(snapshots)
        return {
            "last_scan_at": context.supervisor.state.last_scan_at,
            "repos": [
                _add_mount_info(repo.to_dict(context.config.root)) for repo in snapshots
            ],
        }

    @app.get("/hub/version")
    def hub_version():
        return {"asset_version": app.state.asset_version}

    @app.post("/hub/repos/scan")
    async def scan_repos():
        safe_log(app.state.logger, logging.INFO, "Hub scan_repos")
        snapshots = context.supervisor.scan()
        _refresh_mounts(snapshots)
        return {
            "last_scan_at": context.supervisor.state.last_scan_at,
            "repos": [
                _add_mount_info(repo.to_dict(context.config.root)) for repo in snapshots
            ],
        }

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
                snapshot = context.supervisor.clone_repo(
                    git_url=str(git_url),
                    repo_id=str(repo_id) if repo_id else None,
                    repo_path=repo_path,
                    force=force,
                )
            else:
                snapshot = context.supervisor.create_repo(
                    str(repo_id), repo_path=repo_path, git_init=git_init, force=force
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.get("/hub/repos/{repo_id}/remove-check")
    async def remove_repo_check(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub remove-check {repo_id}")
        try:
            return context.supervisor.check_repo_removal(repo_id)
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
            context.supervisor.remove_repo(
                repo_id,
                force=force,
                delete_dir=delete_dir,
                delete_worktrees=delete_worktrees,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok"}

    @app.post("/hub/worktrees/create")
    async def create_worktree(payload: HubCreateWorktreeRequest):
        base_repo_id = payload.base_repo_id
        branch = payload.branch
        force = payload.force
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub create worktree base=%s branch=%s force=%s"
            % (base_repo_id, branch, force),
        )
        try:
            snapshot = context.supervisor.create_worktree(
                base_repo_id=str(base_repo_id), branch=str(branch), force=force
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/worktrees/cleanup")
    async def cleanup_worktree(payload: HubCleanupWorktreeRequest):
        worktree_repo_id = payload.worktree_repo_id
        delete_branch = payload.delete_branch
        delete_remote = payload.delete_remote
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub cleanup worktree id=%s delete_branch=%s delete_remote=%s"
            % (worktree_repo_id, delete_branch, delete_remote),
        )
        try:
            context.supervisor.cleanup_worktree(
                worktree_repo_id=str(worktree_repo_id),
                delete_branch=delete_branch,
                delete_remote=delete_remote,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok"}

    @app.post("/hub/repos/{repo_id}/run")
    async def run_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub run %s once=%s" % (repo_id, once),
        )
        try:
            snapshot = context.supervisor.run_repo(repo_id, once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/repos/{repo_id}/stop")
    async def stop_repo(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub stop {repo_id}")
        try:
            snapshot = context.supervisor.stop_repo(repo_id)
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
            snapshot = context.supervisor.resume_repo(repo_id, once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/repos/{repo_id}/kill")
    async def kill_repo(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub kill {repo_id}")
        try:
            snapshot = context.supervisor.kill_repo(repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/repos/{repo_id}/init")
    async def init_repo(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub init {repo_id}")
        try:
            snapshot = context.supervisor.init_repo(repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _refresh_mounts([snapshot])
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

    auth_token = _resolve_auth_token(context.config.server_auth_token_env)
    asgi_app: ASGIApp = app
    if auth_token:
        asgi_app = AuthTokenMiddleware(asgi_app, auth_token, context.base_path)
    if context.base_path:
        asgi_app = BasePathRouterMiddleware(asgi_app, context.base_path)

    return asgi_app


def _resolve_auth_token(env_name: str) -> Optional[str]:
    if not env_name:
        return None
    value = os.environ.get(env_name)
    if value is None:
        return None
    value = value.strip()
    return value or None
