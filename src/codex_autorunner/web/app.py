import asyncio
import logging
import os
import shlex
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
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp

from ..agents.opencode.supervisor import OpenCodeSupervisor
from ..core.app_server_events import AppServerEventBuffer
from ..core.app_server_threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from ..core.config import (
    AppServerConfig,
    ConfigError,
    HubConfig,
    _is_loopback_host,
    _normalize_base_path,
    load_config,
)
from ..core.doc_chat import DocChatService
from ..core.engine import Engine, LockError
from ..core.hub import HubSupervisor
from ..core.logging_utils import safe_log, setup_rotating_logger
from ..core.optional_dependencies import require_optional_dependencies
from ..core.request_context import get_request_id
from ..core.snapshot import SnapshotService
from ..core.state import load_state, persist_session_registry
from ..core.usage import (
    UsageError,
    default_codex_home,
    get_hub_usage_series_cached,
    get_hub_usage_summary_cached,
    parse_iso_datetime,
)
from ..housekeeping import run_housekeeping_once
from ..integrations.app_server.client import ApprovalHandler, NotificationHandler
from ..integrations.app_server.env import build_app_server_env
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from ..manifest import load_manifest
from ..routes import build_repo_router
from ..routes.system import build_system_routes
from ..spec_ingest import SpecIngestService
from ..voice import VoiceConfig, VoiceService
from .hub_jobs import HubJobManager
from .middleware import (
    AuthTokenMiddleware,
    BasePathRouterMiddleware,
    HostOriginMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from .runner_manager import RunnerManager
from .schemas import (
    HubCleanupWorktreeRequest,
    HubCreateRepoRequest,
    HubCreateWorktreeRequest,
    HubJobResponse,
    HubRemoveRepoRequest,
    RunControlRequest,
)
from .static_assets import (
    asset_version,
    index_response_headers,
    materialize_static_assets,
    render_index_html,
    require_static_assets,
)
from .terminal_sessions import parse_tui_idle_seconds, prune_terminal_registry


@dataclass(frozen=True)
class AppContext:
    base_path: str
    engine: Engine
    manager: RunnerManager
    doc_chat: DocChatService
    spec_ingest: SpecIngestService
    snapshot_service: SnapshotService
    app_server_supervisor: Optional[WorkspaceAppServerSupervisor]
    app_server_prune_interval: Optional[float]
    app_server_threads: AppServerThreadRegistry
    app_server_events: AppServerEventBuffer
    opencode_supervisor: Optional[OpenCodeSupervisor]
    opencode_prune_interval: Optional[float]
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
    job_manager: HubJobManager
    app_server_supervisor: Optional[WorkspaceAppServerSupervisor]
    app_server_prune_interval: Optional[float]
    static_dir: Path
    static_assets_context: Optional[object]
    asset_version: str
    logger: logging.Logger


@dataclass(frozen=True)
class ServerOverrides:
    allowed_hosts: Optional[list[str]] = None
    allowed_origins: Optional[list[str]] = None
    auth_token_env: Optional[str] = None


def _app_server_prune_interval(idle_ttl_seconds: Optional[int]) -> Optional[float]:
    if not idle_ttl_seconds or idle_ttl_seconds <= 0:
        return None
    return float(min(600.0, max(60.0, idle_ttl_seconds / 2)))


def _normalize_approval_path(path: str, repo_root: Path) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    if raw.startswith("./"):
        raw = raw[2:]
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(repo_root)
        except ValueError:
            return raw
    return candidate.as_posix()


def _extract_approval_paths(params: dict, *, repo_root: Path) -> list[str]:
    paths: list[str] = []

    def _add(entry: object) -> None:
        if isinstance(entry, str):
            normalized = _normalize_approval_path(entry, repo_root)
            if normalized:
                paths.append(normalized)
            return
        if isinstance(entry, dict):
            raw = entry.get("path") or entry.get("file") or entry.get("name")
            if isinstance(raw, str):
                normalized = _normalize_approval_path(raw, repo_root)
                if normalized:
                    paths.append(normalized)

    for payload in (params, params.get("item") if isinstance(params, dict) else None):
        if not isinstance(payload, dict):
            continue
        for key in ("files", "fileChanges", "paths"):
            entries = payload.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    _add(entry)
        for key in ("path", "file", "name"):
            _add(payload.get(key))
    return paths


def _extract_turn_context(params: dict) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(params, dict):
        return None, None
    turn_id = params.get("turnId") or params.get("turn_id") or params.get("id")
    thread_id = params.get("threadId") or params.get("thread_id")
    turn = params.get("turn")
    if isinstance(turn, dict):
        turn_id = turn_id or turn.get("id") or turn.get("turnId")
        thread_id = thread_id or turn.get("threadId") or turn.get("thread_id")
    item = params.get("item")
    if isinstance(item, dict):
        thread_id = thread_id or item.get("threadId") or item.get("thread_id")
    turn_id = str(turn_id) if isinstance(turn_id, str) and turn_id else None
    thread_id = str(thread_id) if isinstance(thread_id, str) and thread_id else None
    return thread_id, turn_id


def _build_app_server_supervisor(
    config: AppServerConfig,
    *,
    logger: logging.Logger,
    event_prefix: str,
    notification_handler: Optional[NotificationHandler] = None,
    approval_handler: Optional[ApprovalHandler] = None,
) -> tuple[Optional[WorkspaceAppServerSupervisor], Optional[float]]:
    if not config.command:
        return None, None

    def _env_builder(
        workspace_root: Path, _workspace_id: str, state_dir: Path
    ) -> dict[str, str]:
        state_dir.mkdir(parents=True, exist_ok=True)
        return build_app_server_env(
            config.command,
            workspace_root,
            state_dir,
            logger=logger,
            event_prefix=event_prefix,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    supervisor = WorkspaceAppServerSupervisor(
        config.command,
        state_root=config.state_root,
        env_builder=_env_builder,
        logger=logger,
        max_handles=config.max_handles,
        idle_ttl_seconds=config.idle_ttl_seconds,
        request_timeout=config.request_timeout,
        notification_handler=notification_handler,
        approval_handler=approval_handler,
    )
    return supervisor, _app_server_prune_interval(config.idle_ttl_seconds)


def _parse_command(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        return [part for part in shlex.split(raw) if part]
    except ValueError:
        return []


def _build_opencode_supervisor(
    config: AppServerConfig,
    *,
    logger: logging.Logger,
) -> tuple[Optional[OpenCodeSupervisor], Optional[float]]:
    raw_command = os.environ.get("CAR_OPENCODE_COMMAND")
    command = _parse_command(raw_command) if raw_command else []
    if not command:
        command = ["opencode", "serve", "--hostname", "127.0.0.1", "--port", "0"]
    if not command:
        return None, None
    username = os.environ.get("OPENCODE_SERVER_USERNAME")
    password = os.environ.get("OPENCODE_SERVER_PASSWORD")
    supervisor = OpenCodeSupervisor(
        command,
        logger=logger,
        request_timeout=config.request_timeout,
        max_handles=config.max_handles,
        idle_ttl_seconds=config.idle_ttl_seconds,
        username=username if username and password else None,
        password=password if username and password else None,
    )
    return supervisor, _app_server_prune_interval(config.idle_ttl_seconds)


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
    voice_config = VoiceConfig.from_raw(config.voice, env=os.environ)
    voice_missing_reason: Optional[str] = None
    try:
        require_optional_dependencies(
            feature="voice",
            deps=(
                ("httpx", "httpx"),
                (("multipart", "python_multipart"), "python-multipart"),
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
    app_server_events = AppServerEventBuffer()
    allowed_doc_paths = {
        path
        for kind in ("todo", "progress", "opinions", "spec", "summary")
        for path in [
            _normalize_approval_path(
                str(engine.config.doc_path(kind).relative_to(engine.config.root)),
                engine.config.root,
            )
        ]
        if path
    }

    async def _doc_chat_approval_handler(message: dict) -> str:
        method = message.get("method")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        thread_id, turn_id = _extract_turn_context(params)
        if method == "item/fileChange/requestApproval":
            paths = _extract_approval_paths(params, repo_root=engine.config.root)
            normalized = [path for path in paths if path]
            if not normalized:
                notice = "Rejected file change without explicit paths."
                await app_server_events.handle_notification(
                    {
                        "method": "error",
                        "params": {
                            "message": notice,
                            "turnId": turn_id,
                            "threadId": thread_id,
                        },
                    }
                )
                return "decline"
            rejected = [path for path in normalized if path not in allowed_doc_paths]
            if rejected:
                notice = "Rejected write to non-doc files: " + ", ".join(rejected)
                await app_server_events.handle_notification(
                    {
                        "method": "error",
                        "params": {
                            "message": notice,
                            "turnId": turn_id,
                            "threadId": thread_id,
                        },
                    }
                )
                return "decline"
            return "accept"
        if method == "item/commandExecution/requestApproval":
            notice = "Rejected command execution in doc chat session."
            await app_server_events.handle_notification(
                {
                    "method": "error",
                    "params": {
                        "message": notice,
                        "turnId": turn_id,
                        "threadId": thread_id,
                    },
                }
            )
            return "decline"
        return "decline"

    app_server_supervisor, app_server_prune_interval = _build_app_server_supervisor(
        engine.config.app_server,
        logger=logger,
        event_prefix="web.app_server",
        notification_handler=app_server_events.handle_notification,
        approval_handler=_doc_chat_approval_handler,
    )
    app_server_threads = AppServerThreadRegistry(
        default_app_server_threads_path(engine.repo_root)
    )
    opencode_supervisor, opencode_prune_interval = _build_opencode_supervisor(
        config.app_server,
        logger=logger,
    )
    doc_chat = DocChatService(
        engine,
        app_server_supervisor=app_server_supervisor,
        app_server_threads=app_server_threads,
        app_server_events=app_server_events,
        opencode_supervisor=opencode_supervisor,
    )
    spec_ingest = SpecIngestService(
        engine,
        app_server_supervisor=app_server_supervisor,
        app_server_threads=app_server_threads,
        app_server_events=app_server_events,
        opencode_supervisor=opencode_supervisor,
    )
    snapshot_service = SnapshotService(
        engine,
        app_server_supervisor=app_server_supervisor,
        app_server_threads=app_server_threads,
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
    # Normalize persisted keys from older/newer versions:
    # - Prefer bare repo keys for the default "codex" agent.
    # - Preserve `repo:agent` keys for non-default agents (e.g. opencode).
    normalized_repo_to_session: dict[str, str] = {}
    for raw_key, session_id in repo_to_session.items():
        key = str(raw_key)
        if ":" in key:
            repo, agent = key.split(":", 1)
            agent_norm = agent.strip().lower()
            if not agent_norm or agent_norm == "codex":
                key = repo
            else:
                key = f"{repo}:{agent_norm}"
        # Keep the first mapping we see to avoid surprising overrides.
        normalized_repo_to_session.setdefault(key, session_id)
    repo_to_session = normalized_repo_to_session
    terminal_sessions: dict = {}
    if session_registry or repo_to_session:
        prune_terminal_registry(
            engine.state_path,
            terminal_sessions,
            session_registry,
            repo_to_session,
            terminal_max_idle_seconds,
        )
    static_dir, static_context = materialize_static_assets(
        config.static_assets.cache_root,
        max_cache_entries=config.static_assets.max_cache_entries,
        max_cache_age_days=config.static_assets.max_cache_age_days,
        logger=logger,
    )
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
        spec_ingest=spec_ingest,
        snapshot_service=snapshot_service,
        app_server_supervisor=app_server_supervisor,
        app_server_prune_interval=app_server_prune_interval,
        app_server_threads=app_server_threads,
        app_server_events=app_server_events,
        opencode_supervisor=opencode_supervisor,
        opencode_prune_interval=opencode_prune_interval,
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
    app.state.spec_ingest = context.spec_ingest
    app.state.snapshot_service = context.snapshot_service
    app.state.app_server_supervisor = context.app_server_supervisor
    app.state.app_server_prune_interval = context.app_server_prune_interval
    app.state.app_server_threads = context.app_server_threads
    app.state.app_server_events = context.app_server_events
    app.state.opencode_supervisor = context.opencode_supervisor
    app.state.opencode_prune_interval = context.opencode_prune_interval
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
    app_server_supervisor, app_server_prune_interval = _build_app_server_supervisor(
        config.app_server,
        logger=logger,
        event_prefix="hub.app_server",
    )
    static_dir, static_context = materialize_static_assets(
        config.static_assets.cache_root,
        max_cache_entries=config.static_assets.max_cache_entries,
        max_cache_age_days=config.static_assets.max_cache_age_days,
        logger=logger,
    )
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
        job_manager=HubJobManager(logger=logger),
        app_server_supervisor=app_server_supervisor,
        app_server_prune_interval=app_server_prune_interval,
        static_dir=static_dir,
        static_assets_context=static_context,
        asset_version=asset_version(static_dir),
        logger=logger,
    )


def _apply_hub_context(app: FastAPI, context: HubAppContext) -> None:
    app.state.base_path = context.base_path
    app.state.logger = context.logger
    app.state.config = context.config  # Expose config for route modules
    app.state.job_manager = context.job_manager
    app.state.app_server_supervisor = context.app_server_supervisor
    app.state.app_server_prune_interval = context.app_server_prune_interval
    app.state.static_dir = context.static_dir
    app.state.static_assets_context = context.static_assets_context
    app.state.asset_version = context.asset_version


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

        tasks.append(asyncio.create_task(_cleanup_loop()))
        if app.state.config.housekeeping.enabled:
            tasks.append(asyncio.create_task(_housekeeping_loop()))
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
                except Exception:
                    pass
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


def create_app(
    repo_root: Optional[Path] = None,
    base_path: Optional[str] = None,
    server_overrides: Optional[ServerOverrides] = None,
) -> ASGIApp:
    context = _build_app_context(repo_root, base_path)
    app = FastAPI(redirect_slashes=False, lifespan=_app_lifespan(context))
    _apply_app_context(app, context)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    app.mount(
        "/static",
        CacheStaticFiles(directory=context.static_dir),
        name="static",
    )
    # Route handlers
    app.include_router(build_repo_router(context.static_dir))

    allowed_hosts = _resolve_allowed_hosts(
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
    auth_token = _resolve_auth_token(auth_token_env)
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


def create_hub_app(
    hub_root: Optional[Path] = None, base_path: Optional[str] = None
) -> ASGIApp:
    context = _build_hub_context(hub_root, base_path)
    app = FastAPI(redirect_slashes=False)
    _apply_hub_context(app, context)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    app.mount(
        "/static",
        CacheStaticFiles(directory=context.static_dir),
        name="static",
    )
    mounted_repos: set[str] = set()
    mount_errors: dict[str, str] = {}
    repo_apps: dict[str, ASGIApp] = {}
    repo_startup_complete: set[str] = set()
    app.state.hub_started = False
    repo_server_overrides: Optional[ServerOverrides] = None
    if context.config.repo_server_inherit:
        repo_server_overrides = ServerOverrides(
            allowed_hosts=_resolve_allowed_hosts(
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
            sub_app = create_app(
                repo_path,
                base_path="",
                server_overrides=repo_server_overrides,
            )
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
        per_repo, unmatched, status = get_hub_usage_summary_cached(
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
            "status": status,
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
        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
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
        snapshots = await asyncio.to_thread(context.supervisor.scan)
        _refresh_mounts(snapshots)
        return {
            "last_scan_at": context.supervisor.state.last_scan_at,
            "repos": [
                _add_mount_info(repo.to_dict(context.config.root)) for repo in snapshots
            ],
        }

    @app.post("/hub/jobs/scan", response_model=HubJobResponse)
    async def scan_repos_job():
        def _run_scan():
            snapshots = context.supervisor.scan()
            _refresh_mounts(snapshots)
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
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/jobs/repos", response_model=HubJobResponse)
    async def create_repo_job(payload: HubCreateRepoRequest):
        def _run_create_repo():
            git_url = payload.git_url
            repo_id = payload.repo_id
            if not repo_id and not git_url:
                raise ValueError("Missing repo id")
            repo_path_val = payload.path
            repo_path = Path(repo_path_val) if repo_path_val else None
            git_init = payload.git_init
            force = payload.force
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
            _refresh_mounts([snapshot])
            return _add_mount_info(snapshot.to_dict(context.config.root))

        job = await context.job_manager.submit(
            "hub.create_repo", _run_create_repo, request_id=get_request_id()
        )
        return job.to_dict()

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
        return {"status": "ok"}

    @app.post("/hub/jobs/repos/{repo_id}/remove", response_model=HubJobResponse)
    async def remove_repo_job(
        repo_id: str, payload: Optional[HubRemoveRepoRequest] = None
    ):
        payload = payload or HubRemoveRepoRequest()

        def _run_remove_repo():
            context.supervisor.remove_repo(
                repo_id,
                force=payload.force,
                delete_dir=payload.delete_dir,
                delete_worktrees=payload.delete_worktrees,
            )
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
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub create worktree base=%s branch=%s force=%s"
            % (base_repo_id, branch, force),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.create_worktree,
                base_repo_id=str(base_repo_id),
                branch=str(branch),
                force=force,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(context.config.root))

    @app.post("/hub/jobs/worktrees/create", response_model=HubJobResponse)
    async def create_worktree_job(payload: HubCreateWorktreeRequest):
        def _run_create_worktree():
            snapshot = context.supervisor.create_worktree(
                base_repo_id=str(payload.base_repo_id),
                branch=str(payload.branch),
                force=payload.force,
            )
            _refresh_mounts([snapshot])
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
        safe_log(
            app.state.logger,
            logging.INFO,
            "Hub cleanup worktree id=%s delete_branch=%s delete_remote=%s"
            % (worktree_repo_id, delete_branch, delete_remote),
        )
        try:
            await asyncio.to_thread(
                context.supervisor.cleanup_worktree,
                worktree_repo_id=str(worktree_repo_id),
                delete_branch=delete_branch,
                delete_remote=delete_remote,
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
            )
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.cleanup_worktree", _run_cleanup_worktree, request_id=get_request_id()
        )
        return job.to_dict()

    @app.get("/hub/jobs/{job_id}", response_model=HubJobResponse)
    async def get_hub_job(job_id: str):
        job = await context.job_manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.to_dict()

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
        _refresh_mounts([snapshot])
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
        _refresh_mounts([snapshot])
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

    allowed_hosts = _resolve_allowed_hosts(
        context.config.server_host, context.config.server_allowed_hosts
    )
    allowed_origins = context.config.server_allowed_origins
    auth_token = _resolve_auth_token(context.config.server_auth_token_env)
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


def _resolve_auth_token(env_name: str) -> Optional[str]:
    if not env_name:
        return None
    value = os.environ.get(env_name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _resolve_allowed_hosts(host: str, allowed_hosts: list[str]) -> list[str]:
    cleaned = [entry.strip() for entry in allowed_hosts if entry and entry.strip()]
    if cleaned:
        return cleaned
    if _is_loopback_host(host):
        return ["localhost", "127.0.0.1", "::1", "testserver"]
    return []


_STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"


class CacheStaticFiles(StaticFiles):
    def __init__(self, *args, cache_control: str = _STATIC_CACHE_CONTROL, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache_control = cache_control

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code in (200, 206, 304):
            response.headers.setdefault("Cache-Control", self._cache_control)
        return response
