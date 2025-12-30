import asyncio
from datetime import datetime, timezone
import logging
import os
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Optional

from fastapi import (
    File,
    FastAPI,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .config import ConfigError, HubConfig, _normalize_base_path, load_config
from .engine import Engine, LockError, doctor
from .logging_utils import safe_log, setup_rotating_logger
from .doc_chat import DocChatService
from .hub import HubSupervisor
from .state import (
    load_state,
    persist_session_registry,
)
from .utils import find_repo_root
from .usage import (
    UsageError,
    default_codex_home,
    get_hub_usage_series_cached,
    parse_iso_datetime,
    summarize_hub_usage,
    summarize_repo_usage,
)
from .manifest import load_manifest
from .static_assets import asset_version, index_response_headers, render_index_html
from .voice import VoiceConfig, VoiceService, VoiceServiceError
from .api_routes import build_repo_router, ActiveSession
from .routes.system import build_system_routes


class BasePathRouterMiddleware:
    """
    Middleware that keeps the app mounted at / while enforcing a canonical base path.
    - Requests that already include the base path are routed via root_path so routing stays rooted at /.
    - Requests missing the base path but pointing at known CAR prefixes are redirected to the
      canonical location (HTTP 308). WebSocket handshakes get the same redirect response.
    """

    def __init__(self, app, base_path: str, known_prefixes=None):
        self.app = app
        self.base_path = _normalize_base_path(base_path)
        self.base_path_bytes = self.base_path.encode("utf-8")
        self.known_prefixes = tuple(
            known_prefixes
            or (
                "/",
                "/api",
                "/hub",
                "/repos",
                "/static",
                "/cat",
            )
        )

    def __getattr__(self, name):
        return getattr(self.app, name)

    def _has_base(self, path: str, root_path: str) -> bool:
        if not self.base_path:
            return True
        full_path = f"{root_path}{path}" if root_path else path
        if full_path == self.base_path or full_path.startswith(f"{self.base_path}/"):
            return True
        return path == self.base_path or path.startswith(f"{self.base_path}/")

    def _should_redirect(self, path: str, root_path: str) -> bool:
        if not self.base_path:
            return False
        if self._has_base(path, root_path):
            return False
        return any(
            path == prefix
            or path.startswith(f"{prefix}/")
            or (root_path and root_path.startswith(prefix))
            for prefix in self.known_prefixes
        )

    async def _redirect(self, scope, receive, send, target: str):
        if scope["type"] == "websocket":
            headers = [(b"location", target.encode("utf-8"))]
            await send(
                {"type": "http.response.start", "status": 308, "headers": headers}
            )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        response = RedirectResponse(target, status_code=308)
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        path = scope.get("path") or "/"
        root_path = scope.get("root_path") or ""

        if not self.base_path:
            return await self.app(scope, receive, send)

        if self._has_base(path, root_path):
            scope = dict(scope)
            # Preserve the base path for downstream routing + URL generation.
            if not root_path:
                scope["root_path"] = self.base_path
                root_path = self.base_path

            # Starlette expects scope["path"] to include scope["root_path"] for
            # mounted sub-apps (including /repos/* and /static/*). If we detect
            # an already-stripped path (e.g., behind a proxy), re-prefix it.
            if root_path and not path.startswith(root_path):
                if path == "/":
                    scope["path"] = root_path
                else:
                    scope["path"] = f"{root_path}{path}"
                raw_path = scope.get("raw_path")
                if raw_path and not raw_path.startswith(self.base_path_bytes):
                    if raw_path == b"/":
                        scope["raw_path"] = self.base_path_bytes
                    else:
                        scope["raw_path"] = self.base_path_bytes + raw_path
            return await self.app(scope, receive, send)

        if self._should_redirect(path, root_path):
            target_path = f"{self.base_path}{path}"
            query_string = scope.get("query_string") or b""
            if query_string:
                target_path = f"{target_path}?{query_string.decode('latin-1')}"
            if not target_path:
                target_path = "/"
            return await self._redirect(scope, receive, send, target_path)

        return await self.app(scope, receive, send)


class RunnerManager:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.thread: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, once: bool = False) -> None:
        with self._lock:
            if self.running:
                return
            # Own the repo lock for the duration of the background runner.
            # This matches CLI/Hub semantics and prevents concurrent runners.
            self.engine.acquire_lock(force=False)
            self.stop_flag.clear()
            target_runs = 1 if once else None
            self.thread = threading.Thread(
                target=self._run_loop,
                kwargs={
                    "stop_after_runs": target_runs,
                    "external_stop_flag": self.stop_flag,
                },
                daemon=True,
            )
            self.thread.start()

    def _run_loop(
        self,
        stop_after_runs: Optional[int] = None,
        external_stop_flag: Optional[threading.Event] = None,
    ) -> None:
        try:
            self.engine.run_loop(
                stop_after_runs=stop_after_runs, external_stop_flag=external_stop_flag
            )
        finally:
            try:
                self.engine.release_lock()
            except Exception:
                pass

    def stop(self) -> None:
        with self._lock:
            if self.stop_flag:
                self.stop_flag.set()

    def kill(self) -> None:
        with self._lock:
            self.stop_flag.set()
            # Best-effort join to allow loop to exit.
            if self.thread:
                self.thread.join(timeout=1.0)


def _static_dir() -> Path:
    return Path(resources.files("codex_autorunner")) / "static"


def _parse_last_seen_at(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def _session_last_touch(session: ActiveSession, record) -> float:
    last_seen = _parse_last_seen_at(getattr(record, "last_seen_at", None))
    if last_seen is None:
        return session.pty.last_active
    return max(last_seen, session.pty.last_active)


def _parse_tui_idle_seconds(config) -> Optional[float]:
    notifications_cfg = (
        config.notifications if isinstance(config.notifications, dict) else {}
    )
    idle_seconds = notifications_cfg.get("tui_idle_seconds")
    if idle_seconds is None:
        return None
    try:
        idle_seconds = float(idle_seconds)
    except (TypeError, ValueError):
        return None
    if idle_seconds <= 0:
        return None
    return idle_seconds


def _prune_terminal_registry(
    state_path: Path,
    terminal_sessions: dict[str, ActiveSession],
    session_registry: dict,
    repo_to_session: dict[str, str],
    max_idle_seconds: Optional[int],
) -> bool:
    now = time.time()
    removed_any = False
    for session_id, session in list(terminal_sessions.items()):
        if not session.pty.isalive():
            session.close()
            terminal_sessions.pop(session_id, None)
            session_registry.pop(session_id, None)
            removed_any = True
            continue
        if max_idle_seconds is not None and max_idle_seconds > 0:
            last_touch = _session_last_touch(session, session_registry.get(session_id))
            if now - last_touch > max_idle_seconds:
                session.close()
                terminal_sessions.pop(session_id, None)
                session_registry.pop(session_id, None)
                removed_any = True
    for session_id in list(session_registry.keys()):
        if session_id not in terminal_sessions:
            session_registry.pop(session_id, None)
            removed_any = True
    for repo_path, session_id in list(repo_to_session.items()):
        if session_id not in session_registry:
            repo_to_session.pop(repo_path, None)
            removed_any = True
    if removed_any:
        persist_session_registry(state_path, session_registry, repo_to_session)
    return removed_any


def create_app(
    repo_root: Optional[Path] = None, base_path: Optional[str] = None
) -> FastAPI:
    config = load_config(repo_root or Path.cwd())
    if isinstance(config, HubConfig):
        raise ConfigError("create_app requires repo mode configuration")
    base_path = (
        _normalize_base_path(base_path)
        if base_path is not None
        else config.server_base_path
    )
    engine = Engine(config.root)
    manager = RunnerManager(engine)
    doc_chat = DocChatService(engine)
    voice_config = VoiceConfig.from_raw(config.voice, env=os.environ)
    terminal_max_idle_seconds = config.terminal_idle_timeout_seconds
    if terminal_max_idle_seconds is not None and terminal_max_idle_seconds <= 0:
        terminal_max_idle_seconds = None
    tui_idle_seconds = _parse_tui_idle_seconds(config)
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

    app = FastAPI(redirect_slashes=False)
    app.state.base_path = base_path
    # IMPORTANT: keep server/system logs separate from the agent execution trace.
    app.state.logger = setup_rotating_logger(
        f"repo[{engine.repo_root}]", engine.config.server_log
    )
    engine.notifier.set_logger(app.state.logger)
    safe_log(
        app.state.logger,
        logging.INFO,
        f"Repo server ready at {engine.repo_root}",
    )
    voice_service: Optional[VoiceService]
    try:
        voice_service = VoiceService(voice_config, logger=app.state.logger)
    except Exception as exc:
        voice_service = None
        safe_log(
            app.state.logger,
            logging.WARNING,
            "Voice service unavailable",
            exc,
        )
    # Store shared state for routers/handlers.
    app.state.engine = engine
    app.state.config = engine.config  # Expose config consistently
    app.state.manager = manager
    app.state.doc_chat = doc_chat
    app.state.voice_config = voice_config
    # Optional: if initialization failed, API handlers should degrade gracefully.
    app.state.voice_service = voice_service
    app.state.terminal_sessions = {}
    app.state.terminal_max_idle_seconds = terminal_max_idle_seconds
    app.state.terminal_lock = terminal_lock
    app.state.session_registry = {}
    app.state.repo_to_session = {}
    app.state.session_state_last_write = 0.0
    app.state.session_state_dirty = False
    initial_state = load_state(engine.state_path)
    app.state.session_registry = dict(initial_state.sessions)
    app.state.repo_to_session = dict(initial_state.repo_to_session)
    if app.state.session_registry or app.state.repo_to_session:
        _prune_terminal_registry(
            engine.state_path,
            app.state.terminal_sessions,
            app.state.session_registry,
            app.state.repo_to_session,
            terminal_max_idle_seconds,
        )

    static_dir = _static_dir()
    app.state.asset_version = asset_version(static_dir)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    # Route handlers
    app.include_router(build_repo_router(static_dir))

    @app.on_event("startup")
    async def start_cleanup_task():
        async def _cleanup_loop():
            while True:
                await asyncio.sleep(600)  # Check every 10 mins
                try:
                    async with app.state.terminal_lock:
                        _prune_terminal_registry(
                            engine.state_path,
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

        asyncio.create_task(_cleanup_loop())

        if tui_idle_seconds is None or tui_idle_check_seconds is None:
            return

        async def _tui_idle_loop():
            while True:
                await asyncio.sleep(tui_idle_check_seconds)
                try:
                    async with app.state.terminal_lock:
                        terminal_sessions = app.state.terminal_sessions
                        session_registry = app.state.session_registry
                        for session_id, session in list(terminal_sessions.items()):
                            if not session.pty.isalive():
                                continue
                            if not session.should_notify_idle(tui_idle_seconds):
                                continue
                            record = session_registry.get(session_id)
                            repo_path = record.repo_path if record else None
                            notifier = getattr(engine, "notifier", None)
                            if notifier:
                                asyncio.create_task(
                                    notifier.notify_tui_idle_async(
                                        session_id=session_id,
                                        idle_seconds=tui_idle_seconds,
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

    @app.on_event("shutdown")
    async def shutdown_terminal_sessions():
        async with app.state.terminal_lock:
            for session in app.state.terminal_sessions.values():
                session.close()
            app.state.terminal_sessions.clear()
            app.state.session_registry.clear()
            app.state.repo_to_session.clear()
            persist_session_registry(
                engine.state_path,
                app.state.session_registry,
                app.state.repo_to_session,
            )

    if base_path:
        app = BasePathRouterMiddleware(app, base_path)

    return app


def create_hub_app(
    hub_root: Optional[Path] = None, base_path: Optional[str] = None
) -> FastAPI:
    config = load_config(hub_root or Path.cwd())
    if not isinstance(config, HubConfig):
        raise ConfigError("Hub app requires hub mode configuration")
    base_path = (
        _normalize_base_path(base_path)
        if base_path is not None
        else config.server_base_path
    )
    supervisor = HubSupervisor(config)
    app = FastAPI(redirect_slashes=False)
    app.state.base_path = base_path
    # Hub server/system logs (separate from any repo agent logs).
    app.state.logger = setup_rotating_logger(f"hub[{config.root}]", config.server_log)
    app.state.config = config  # Expose config for route modules
    safe_log(
        app.state.logger,
        logging.INFO,
        f"Hub app ready at {config.root}",
    )
    static_dir = _static_dir()
    app.state.asset_version = asset_version(static_dir)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    mounted_repos: set[str] = set()
    mount_errors: dict[str, str] = {}
    repo_apps: dict[str, FastAPI] = {}
    repo_startup_complete: set[str] = set()
    app.state.hub_started = False

    async def _start_repo_app(prefix: str, sub_app: FastAPI) -> None:
        if prefix in repo_startup_complete:
            return
        try:
            await sub_app.router.startup()
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

    initial_snapshots = supervisor.scan()
    _refresh_mounts(initial_snapshots)

    @app.on_event("startup")
    async def hub_startup():
        app.state.hub_started = True
        for prefix, sub_app in list(repo_apps.items()):
            await _start_repo_app(prefix, sub_app)

    @app.on_event("shutdown")
    async def hub_shutdown():
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

    @app.get("/hub/usage")
    def hub_usage(since: Optional[str] = None, until: Optional[str] = None):
        try:
            since_dt = parse_iso_datetime(since)
            until_dt = parse_iso_datetime(until)
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        manifest = load_manifest(config.manifest_path, config.root)
        repo_map = [(repo.id, (config.root / repo.path)) for repo in manifest.repos]
        per_repo, unmatched = summarize_hub_usage(
            repo_map,
            default_codex_home(),
            since=since_dt,
            until=until_dt,
        )
        return {
            "mode": "hub",
            "hub_root": str(config.root),
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
            raise HTTPException(status_code=400, detail=str(exc))

        manifest = load_manifest(config.manifest_path, config.root)
        repo_map = [(repo.id, (config.root / repo.path)) for repo in manifest.repos]
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
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "mode": "hub",
            "hub_root": str(config.root),
            "codex_home": str(default_codex_home()),
            "since": since,
            "until": until,
            "status": status,
            **series,
        }

    @app.get("/hub/repos")
    async def list_repos():
        safe_log(app.state.logger, logging.INFO, "Hub list_repos")
        snapshots = supervisor.list_repos()
        _refresh_mounts(snapshots)
        return {
            "last_scan_at": supervisor.state.last_scan_at,
            "repos": [_add_mount_info(repo.to_dict(config.root)) for repo in snapshots],
        }

    @app.get("/hub/version")
    def hub_version():
        return {"asset_version": app.state.asset_version}

    @app.post("/hub/repos/scan")
    async def scan_repos():
        safe_log(app.state.logger, logging.INFO, "Hub scan_repos")
        snapshots = supervisor.scan()
        _refresh_mounts(snapshots)
        return {
            "last_scan_at": supervisor.state.last_scan_at,
            "repos": [_add_mount_info(repo.to_dict(config.root)) for repo in snapshots],
        }

    @app.post("/hub/repos")
    async def create_repo(payload: Optional[dict] = None):
        if not payload or not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        repo_id = payload.get("id") or payload.get("repo_id")
        if not repo_id:
            raise HTTPException(status_code=400, detail="Missing repo id")
        repo_path_val = payload.get("path")
        repo_path = Path(repo_path_val) if repo_path_val else None
        git_init = bool(payload.get("git_init", True))
        force = bool(payload.get("force", False))
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub create repo id=%s path=%s git_init=%s force=%s"
            % (repo_id, repo_path_val, git_init, force),
        )
        try:
            snapshot = supervisor.create_repo(
                str(repo_id), repo_path=repo_path, git_init=git_init, force=force
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/worktrees/create")
    async def create_worktree(payload: Optional[dict] = None):
        if not payload or not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        base_repo_id = payload.get("base_repo_id") or payload.get("baseRepoId")
        branch = payload.get("branch")
        force = bool(payload.get("force", False))
        if not base_repo_id or not branch:
            raise HTTPException(
                status_code=400, detail="Missing base_repo_id or branch"
            )
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub create worktree base=%s branch=%s force=%s"
            % (base_repo_id, branch, force),
        )
        try:
            snapshot = supervisor.create_worktree(
                base_repo_id=str(base_repo_id), branch=str(branch), force=force
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/worktrees/cleanup")
    async def cleanup_worktree(payload: Optional[dict] = None):
        if not payload or not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        worktree_repo_id = payload.get("worktree_repo_id") or payload.get(
            "worktreeRepoId"
        )
        if not worktree_repo_id:
            raise HTTPException(status_code=400, detail="Missing worktree_repo_id")
        delete_branch = bool(payload.get("delete_branch", False))
        delete_remote = bool(payload.get("delete_remote", False))
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub cleanup worktree id=%s delete_branch=%s delete_remote=%s"
            % (worktree_repo_id, delete_branch, delete_remote),
        )
        try:
            supervisor.cleanup_worktree(
                worktree_repo_id=str(worktree_repo_id),
                delete_branch=delete_branch,
                delete_remote=delete_remote,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "ok"}

    @app.post("/hub/repos/{repo_id}/run")
    async def run_repo(repo_id: str, payload: Optional[dict] = None):
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub run %s once=%s" % (repo_id, once),
        )
        try:
            snapshot = supervisor.run_repo(repo_id, once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/stop")
    async def stop_repo(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub stop {repo_id}")
        try:
            snapshot = supervisor.stop_repo(repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/resume")
    async def resume_repo(repo_id: str, payload: Optional[dict] = None):
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub resume %s once=%s" % (repo_id, once),
        )
        try:
            snapshot = supervisor.resume_repo(repo_id, once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/kill")
    async def kill_repo(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub kill {repo_id}")
        try:
            snapshot = supervisor.kill_repo(repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/init")
    async def init_repo(repo_id: str):
        safe_log(app.state.logger, logging.INFO, f"Hub init {repo_id}")
        try:
            snapshot = supervisor.init_repo(repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.get("/", include_in_schema=False)
    def hub_index():
        index_path = static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=500, detail="Static UI assets missing; reinstall package"
            )
        html = render_index_html(static_dir, app.state.asset_version)
        return HTMLResponse(html, headers=index_response_headers())

    app.include_router(build_system_routes())

    if base_path:
        app = BasePathRouterMiddleware(app, base_path)

    return app
