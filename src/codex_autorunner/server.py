import asyncio
import os
import threading
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
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .config import ConfigError, HubConfig, _normalize_base_path, load_config
from .engine import Engine, LockError, doctor
from .logging_utils import setup_rotating_logger
from .doc_chat import DocChatService
from .hub import HubSupervisor
from .state import load_state, save_state, RunnerState, now_iso
from .utils import find_repo_root
from .usage import (
    UsageError,
    default_codex_home,
    parse_iso_datetime,
    summarize_hub_usage,
    summarize_repo_usage,
)
from .manifest import load_manifest
from .voice import VoiceConfig, VoiceService, VoiceServiceError
from .api_routes import build_repo_router, ActiveSession


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
    terminal_max_idle_seconds = 3600
    terminal_lock = asyncio.Lock()

    app = FastAPI(redirect_slashes=False)
    app.state.base_path = base_path
    # IMPORTANT: keep server/system logs separate from the agent execution trace.
    app.state.logger = setup_rotating_logger(
        f"repo[{engine.repo_root}]", engine.config.server_log
    )
    app.state.logger.info("Repo server ready at %s", engine.repo_root)
    voice_service: Optional[VoiceService]
    try:
        voice_service = VoiceService(voice_config, logger=app.state.logger)
    except Exception as exc:
        voice_service = None
        app.state.logger.warning("Voice service unavailable: %s", exc, exc_info=False)
    # Store shared state for routers/handlers.
    app.state.engine = engine
    app.state.manager = manager
    app.state.doc_chat = doc_chat
    app.state.voice_config = voice_config
    # Optional: if initialization failed, API handlers should degrade gracefully.
    app.state.voice_service = voice_service
    app.state.terminal_sessions = {}
    app.state.terminal_max_idle_seconds = terminal_max_idle_seconds
    app.state.terminal_lock = terminal_lock

    static_dir = _static_dir()
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
                        to_remove = []
                        for sid, session in app.state.terminal_sessions.items():
                            if session.pty.is_stale(
                                app.state.terminal_max_idle_seconds
                            ):
                                session.close()
                                to_remove.append(sid)
                        for sid in to_remove:
                            app.state.terminal_sessions.pop(sid, None)
                except Exception:
                    pass

        asyncio.create_task(_cleanup_loop())

    @app.on_event("shutdown")
    async def shutdown_terminal_sessions():
        async with app.state.terminal_lock:
            for session in app.state.terminal_sessions.values():
                session.close()
            app.state.terminal_sessions.clear()

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
    try:
        app.state.logger.info("Hub app ready at %s", config.root)
    except Exception:
        pass
    static_dir = _static_dir()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    mounted_repos: set[str] = set()
    mount_errors: dict[str, str] = {}

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
        mount_errors.pop(prefix, None)
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

    @app.get("/hub/repos")
    def list_repos():
        try:
            app.state.logger.info("Hub list_repos")
        except Exception:
            pass
        snapshots = supervisor.list_repos()
        _refresh_mounts(snapshots)
        return {
            "last_scan_at": supervisor.state.last_scan_at,
            "repos": [_add_mount_info(repo.to_dict(config.root)) for repo in snapshots],
        }

    @app.post("/hub/repos/scan")
    def scan_repos():
        try:
            app.state.logger.info("Hub scan_repos")
        except Exception:
            pass
        snapshots = supervisor.scan()
        _refresh_mounts(snapshots)
        return {
            "last_scan_at": supervisor.state.last_scan_at,
            "repos": [_add_mount_info(repo.to_dict(config.root)) for repo in snapshots],
        }

    @app.post("/hub/repos")
    def create_repo(payload: Optional[dict] = None):
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
        try:
            app.state.logger.info(
                "Hub create repo id=%s path=%s git_init=%s force=%s",
                repo_id,
                repo_path_val,
                git_init,
                force,
            )
        except Exception:
            pass
        try:
            snapshot = supervisor.create_repo(
                str(repo_id), repo_path=repo_path, git_init=git_init, force=force
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/worktrees/create")
    def create_worktree(payload: Optional[dict] = None):
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
        try:
            app.state.logger.info(
                "Hub create worktree base=%s branch=%s force=%s",
                base_repo_id,
                branch,
                force,
            )
        except Exception:
            pass
        try:
            snapshot = supervisor.create_worktree(
                base_repo_id=str(base_repo_id), branch=str(branch), force=force
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/worktrees/cleanup")
    def cleanup_worktree(payload: Optional[dict] = None):
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
        try:
            app.state.logger.info(
                "Hub cleanup worktree id=%s delete_branch=%s delete_remote=%s",
                worktree_repo_id,
                delete_branch,
                delete_remote,
            )
        except Exception:
            pass
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
    def run_repo(repo_id: str, payload: Optional[dict] = None):
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        try:
            app.state.logger.info("Hub run %s once=%s", repo_id, once)
        except Exception:
            pass
        try:
            snapshot = supervisor.run_repo(repo_id, once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/stop")
    def stop_repo(repo_id: str):
        try:
            app.state.logger.info("Hub stop %s", repo_id)
        except Exception:
            pass
        try:
            snapshot = supervisor.stop_repo(repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/resume")
    def resume_repo(repo_id: str, payload: Optional[dict] = None):
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        try:
            app.state.logger.info("Hub resume %s once=%s", repo_id, once)
        except Exception:
            pass
        try:
            snapshot = supervisor.resume_repo(repo_id, once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/kill")
    def kill_repo(repo_id: str):
        try:
            app.state.logger.info("Hub kill %s", repo_id)
        except Exception:
            pass
        try:
            snapshot = supervisor.kill_repo(repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/init")
    def init_repo(repo_id: str):
        try:
            app.state.logger.info("Hub init %s", repo_id)
        except Exception:
            pass
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
        return FileResponse(index_path)

    if base_path:
        app = BasePathRouterMiddleware(app, base_path)

    return app
