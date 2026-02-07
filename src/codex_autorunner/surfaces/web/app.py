import asyncio
import json
import logging
import os
import shlex
import sys
import threading
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Mount
from starlette.types import ASGIApp

from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...agents.registry import validate_agent_id
from ...bootstrap import ensure_hub_car_shim
from ...core.app_server_threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from ...core.config import (
    AppServerConfig,
    ConfigError,
    HubConfig,
    _is_loopback_host,
    _normalize_base_path,
    collect_env_overrides,
    derive_repo_config,
    load_hub_config,
    load_repo_config,
    resolve_env_for_root,
)
from ...core.flows.failure_diagnostics import (
    format_failure_summary,
    get_failure_payload,
)
from ...core.flows.models import FlowRunStatus
from ...core.flows.reconciler import reconcile_flow_runs
from ...core.flows.store import FlowStore
from ...core.hub import HubSupervisor
from ...core.logging_utils import safe_log, setup_rotating_logger
from ...core.optional_dependencies import require_optional_dependencies
from ...core.request_context import get_request_id
from ...core.runtime import LockError, RuntimeContext
from ...core.state import load_state, persist_session_registry
from ...core.usage import (
    UsageError,
    default_codex_home,
    get_hub_usage_series_cached,
    get_hub_usage_summary_cached,
    parse_iso_datetime,
)
from ...core.utils import (
    atomic_write,
    build_opencode_supervisor,
    reset_repo_root_context,
    set_repo_root_context,
)
from ...housekeeping import run_housekeeping_once
from ...integrations.agents import build_backend_orchestrator
from ...integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from ...integrations.app_server.client import ApprovalHandler, NotificationHandler
from ...integrations.app_server.env import build_app_server_env
from ...integrations.app_server.event_buffer import AppServerEventBuffer
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from ...manifest import load_manifest
from ...tickets.files import list_ticket_paths, safe_relpath, ticket_is_done
from ...tickets.models import Dispatch
from ...tickets.outbox import parse_dispatch, resolve_outbox_paths
from ...tickets.replies import resolve_reply_paths
from ...voice import VoiceConfig, VoiceService
from .hub_jobs import HubJobManager
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
    env: Mapping[str, str]
    engine: RuntimeContext
    manager: RunnerManager
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
    app_server_threads: AppServerThreadRegistry
    app_server_events: AppServerEventBuffer
    opencode_supervisor: Optional[OpenCodeSupervisor]
    opencode_prune_interval: Optional[float]
    static_dir: Path
    static_assets_context: Optional[object]
    asset_version: str
    logger: logging.Logger


@dataclass(frozen=True)
class ServerOverrides:
    allowed_hosts: Optional[list[str]] = None
    allowed_origins: Optional[list[str]] = None
    auth_token_env: Optional[str] = None


_HUB_INBOX_DISMISSALS_FILENAME = "hub_inbox_dismissals.json"


def _hub_inbox_dismissals_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / _HUB_INBOX_DISMISSALS_FILENAME


def _dismissal_key(run_id: str, seq: int) -> str:
    return f"{run_id}:{seq}"


def _load_hub_inbox_dismissals(repo_root: Path) -> dict[str, dict[str, Any]]:
    path = _hub_inbox_dismissals_path(repo_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    items = payload.get("items")
    if not isinstance(items, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in items.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        out[key] = dict(value)
    return out


def _save_hub_inbox_dismissals(
    repo_root: Path, items: dict[str, dict[str, Any]]
) -> None:
    path = _hub_inbox_dismissals_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "items": items}
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _resolve_workspace_and_runs(
    record_input: dict[str, Any], repo_root: Path
) -> tuple[Path, Path]:
    workspace_raw = record_input.get("workspace_root")
    workspace_root = Path(workspace_raw) if workspace_raw else repo_root
    if not workspace_root.is_absolute():
        workspace_root = (repo_root / workspace_root).resolve()
    else:
        workspace_root = workspace_root.resolve()
    resolved_repo = repo_root.resolve()
    if not (
        workspace_root == resolved_repo
        or str(workspace_root).startswith(str(resolved_repo) + os.sep)
    ):
        raise ValueError(f"workspace_root escapes repo boundary: {workspace_root}")
    runs_raw = record_input.get("runs_dir") or ".codex-autorunner/runs"
    runs_dir = Path(runs_raw)
    if not runs_dir.is_absolute():
        runs_dir = (workspace_root / runs_dir).resolve()
    return workspace_root, runs_dir


def _latest_reply_history_seq(
    repo_root: Path, run_id: str, record_input: dict[str, Any]
) -> int:
    workspace_root, runs_dir = _resolve_workspace_and_runs(record_input, repo_root)
    reply_paths = resolve_reply_paths(
        workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
    )
    history_dir = reply_paths.reply_history_dir
    if not history_dir.exists() or not history_dir.is_dir():
        return 0
    latest = 0
    try:
        for child in history_dir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if len(name) == 4 and name.isdigit():
                latest = max(latest, int(name))
    except OSError:
        return latest
    return latest


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


def _path_is_allowed_for_file_write(path: str) -> bool:
    raw = (path or "").strip()
    if not raw:
        return False
    # Collapse '..' segments so traversal payloads like
    # '.codex-autorunner/workspace/../../etc/passwd' are caught.
    import posixpath

    normalized = posixpath.normpath(raw)
    if normalized.startswith("/") or normalized.startswith(".."):
        return False
    # Canonical allowlist for all AI-assisted file edits via app-server approval:
    # - tickets: .codex-autorunner/tickets/**
    # - contextspace docs: .codex-autorunner/contextspace/**
    allowed_prefixes = (
        ".codex-autorunner/tickets/",
        ".codex-autorunner/contextspace/",
    )
    if normalized in (".codex-autorunner/tickets", ".codex-autorunner/contextspace"):
        return True
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in allowed_prefixes
    )


def _build_app_server_supervisor(
    config: AppServerConfig,
    *,
    logger: logging.Logger,
    event_prefix: str,
    base_env: Optional[Mapping[str, str]] = None,
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
            base_env=base_env,
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
        auto_restart=config.auto_restart,
        max_handles=config.max_handles,
        idle_ttl_seconds=config.idle_ttl_seconds,
        request_timeout=config.request_timeout,
        turn_stall_timeout_seconds=config.turn_stall_timeout_seconds,
        turn_stall_poll_interval_seconds=config.turn_stall_poll_interval_seconds,
        turn_stall_recovery_min_interval_seconds=config.turn_stall_recovery_min_interval_seconds,
        max_message_bytes=config.client.max_message_bytes,
        oversize_preview_bytes=config.client.oversize_preview_bytes,
        max_oversize_drain_bytes=config.client.max_oversize_drain_bytes,
        restart_backoff_initial_seconds=config.client.restart_backoff_initial_seconds,
        restart_backoff_max_seconds=config.client.restart_backoff_max_seconds,
        restart_backoff_jitter_ratio=config.client.restart_backoff_jitter_ratio,
        output_policy=config.output.policy,
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
    workspace_root: Path,
    opencode_binary: Optional[str],
    opencode_command: Optional[list[str]],
    logger: logging.Logger,
    env: Mapping[str, str],
    subagent_models: Optional[Mapping[str, str]] = None,
    session_stall_timeout_seconds: Optional[float] = None,
    max_text_chars: Optional[int] = None,
) -> tuple[Optional[OpenCodeSupervisor], Optional[float]]:
    supervisor = build_opencode_supervisor(
        opencode_command=opencode_command,
        opencode_binary=opencode_binary,
        workspace_root=workspace_root,
        logger=logger,
        request_timeout=config.request_timeout,
        max_handles=config.max_handles,
        idle_ttl_seconds=config.idle_ttl_seconds,
        session_stall_timeout_seconds=session_stall_timeout_seconds,
        max_text_chars=max_text_chars,
        base_env=env,
        subagent_models=subagent_models,
    )
    if supervisor is None:
        safe_log(
            logger,
            logging.INFO,
            "OpenCode command unavailable; skipping opencode supervisor.",
        )
        return None, None
    return supervisor, _app_server_prune_interval(config.idle_ttl_seconds)


def _build_app_context(
    repo_root: Optional[Path],
    base_path: Optional[str],
    hub_config: Optional[HubConfig] = None,
) -> AppContext:
    target_root = (repo_root or Path.cwd()).resolve()
    if hub_config is None:
        config = load_repo_config(target_root)
        env = dict(os.environ)
    else:
        env = resolve_env_for_root(target_root)
        config = derive_repo_config(hub_config, target_root, load_env=False)
    normalized_base = (
        _normalize_base_path(base_path)
        if base_path is not None
        else config.server_base_path
    )
    backend_orchestrator = build_backend_orchestrator(config.root, config)
    engine = RuntimeContext(
        config.root,
        config=config,
        backend_orchestrator=backend_orchestrator,
    )
    manager = RunnerManager(engine)
    voice_config = VoiceConfig.from_raw(config.voice, env=env)
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
    env_overrides = collect_env_overrides(env=env)
    if env_overrides:
        safe_log(
            logger,
            logging.INFO,
            "Environment overrides active: %s",
            ", ".join(env_overrides),
        )
    safe_log(
        logger,
        logging.INFO,
        f"Repo server ready at {engine.repo_root}",
    )
    app_server_events = AppServerEventBuffer()

    async def _file_write_approval_handler(message: dict) -> str:
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
            rejected = [
                path for path in normalized if not _path_is_allowed_for_file_write(path)
            ]
            if rejected:
                notice = "Rejected write outside allowlist: " + ", ".join(rejected)
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
            notice = "Rejected command execution in file write session."
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
        base_env=env,
        notification_handler=app_server_events.handle_notification,
        approval_handler=_file_write_approval_handler,
    )
    app_server_threads = AppServerThreadRegistry(
        default_app_server_threads_path(engine.repo_root)
    )
    opencode_command = config.agent_serve_command("opencode")
    try:
        opencode_binary = config.agent_binary("opencode")
    except ConfigError:
        opencode_binary = None
    agent_config = config.agents.get("opencode")
    subagent_models = agent_config.subagent_models if agent_config else None
    opencode_supervisor, opencode_prune_interval = _build_opencode_supervisor(
        config.app_server,
        workspace_root=engine.repo_root,
        opencode_binary=opencode_binary,
        opencode_command=opencode_command,
        logger=logger,
        env=env,
        subagent_models=subagent_models,
        session_stall_timeout_seconds=config.opencode.session_stall_timeout_seconds,
        max_text_chars=config.opencode.max_text_chars,
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

    def _load_static_assets(
        cache_root: Path, max_cache_entries: int, max_cache_age_days: Optional[int]
    ) -> tuple[Path, Optional[ExitStack]]:
        static_dir, static_context = materialize_static_assets(
            cache_root,
            max_cache_entries=max_cache_entries,
            max_cache_age_days=max_cache_age_days,
            logger=logger,
        )
        try:
            require_static_assets(static_dir, logger)
        except Exception as exc:
            if static_context is not None:
                static_context.close()
            safe_log(
                logger,
                logging.WARNING,
                "Static assets requirement check failed",
                exc=exc,
            )
            raise
        return static_dir, static_context

    try:
        static_dir, static_context = _load_static_assets(
            config.static_assets.cache_root,
            config.static_assets.max_cache_entries,
            config.static_assets.max_cache_age_days,
        )
    except Exception as exc:
        if hub_config is None:
            raise
        hub_static = hub_config.static_assets
        if hub_static.cache_root == config.static_assets.cache_root:
            raise
        safe_log(
            logger,
            logging.WARNING,
            "Repo static assets unavailable; retrying with hub cache root %s",
            hub_static.cache_root,
            exc=exc,
        )
        static_dir, static_context = _load_static_assets(
            hub_static.cache_root,
            hub_static.max_cache_entries,
            hub_static.max_cache_age_days,
        )
    return AppContext(
        base_path=normalized_base,
        env=env,
        engine=engine,
        manager=manager,
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
    app.state.env = context.env
    app.state.logger = context.logger
    app.state.engine = context.engine
    app.state.config = context.engine.config  # Expose config consistently
    app.state.manager = context.manager
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
    config = load_hub_config(hub_root or Path.cwd())
    normalized_base = (
        _normalize_base_path(base_path)
        if base_path is not None
        else config.server_base_path
    )
    supervisor = HubSupervisor(
        config,
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
        agent_id_validator=validate_agent_id,
    )
    logger = setup_rotating_logger(f"hub[{config.root}]", config.server_log)
    env_overrides = collect_env_overrides()
    if env_overrides:
        safe_log(
            logger,
            logging.INFO,
            "Environment overrides active: %s",
            ", ".join(env_overrides),
        )
    safe_log(
        logger,
        logging.INFO,
        f"Hub app ready at {config.root}",
    )
    try:
        ensure_hub_car_shim(config.root, python_executable=sys.executable)
    except Exception as exc:
        safe_log(
            logger,
            logging.WARNING,
            "Failed to ensure hub car shim",
            exc=exc,
        )
    app_server_events = AppServerEventBuffer()
    app_server_supervisor, app_server_prune_interval = _build_app_server_supervisor(
        config.app_server,
        logger=logger,
        event_prefix="hub.app_server",
        notification_handler=app_server_events.handle_notification,
    )
    app_server_threads = AppServerThreadRegistry(
        default_app_server_threads_path(config.root)
    )
    opencode_command = config.agent_serve_command("opencode")
    try:
        opencode_binary = config.agent_binary("opencode")
    except ConfigError:
        opencode_binary = None
    agent_config = config.agents.get("opencode")
    subagent_models = agent_config.subagent_models if agent_config else None
    opencode_supervisor, opencode_prune_interval = _build_opencode_supervisor(
        config.app_server,
        workspace_root=config.root,
        opencode_binary=opencode_binary,
        opencode_command=opencode_command,
        logger=logger,
        env=resolve_env_for_root(config.root),
        subagent_models=subagent_models,
        session_stall_timeout_seconds=config.opencode.session_stall_timeout_seconds,
        max_text_chars=config.opencode.max_text_chars,
    )
    static_dir, static_context = materialize_static_assets(
        config.static_assets.cache_root,
        max_cache_entries=config.static_assets.max_cache_entries,
        max_cache_age_days=config.static_assets.max_cache_age_days,
        logger=logger,
    )
    try:
        require_static_assets(static_dir, logger)
    except Exception as exc:
        if static_context is not None:
            static_context.close()
        safe_log(
            logger,
            logging.WARNING,
            "Static assets requirement check failed",
            exc=exc,
        )
        raise
    return HubAppContext(
        base_path=normalized_base,
        config=config,
        supervisor=supervisor,
        job_manager=HubJobManager(logger=logger),
        app_server_supervisor=app_server_supervisor,
        app_server_prune_interval=app_server_prune_interval,
        app_server_threads=app_server_threads,
        app_server_events=app_server_events,
        opencode_supervisor=opencode_supervisor,
        opencode_prune_interval=opencode_prune_interval,
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
    app.state.app_server_threads = context.app_server_threads
    app.state.app_server_events = context.app_server_events
    app.state.opencode_supervisor = context.opencode_supervisor
    app.state.opencode_prune_interval = context.opencode_prune_interval
    app.state.static_dir = context.static_dir
    app.state.static_assets_context = context.static_assets_context
    app.state.asset_version = context.asset_version
    app.state.hub_supervisor = context.supervisor


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
    context = _build_app_context(repo_root, base_path="", hub_config=hub_config)
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
    _apply_app_context(app, context)
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
    auth_token = _resolve_auth_token(auth_token_env, env=context.env)
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
    context = _build_app_context(repo_root, base_path, hub_config=hub_config)
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
    _apply_app_context(app, context)
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
    auth_token = _resolve_auth_token(auth_token_env, env=context.env)
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
    context = _build_hub_context(hub_root, base_path)
    app = FastAPI(redirect_slashes=False)
    _apply_hub_context(app, context)
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
        db_path = repo_path / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            return None
        try:
            config = load_repo_config(repo_path)
            with FlowStore(db_path, durable=config.durable_writes) as store:
                # Get the latest ticket_flow run (any status)
                runs = store.list_flow_runs(flow_type="ticket_flow")
                if not runs:
                    return None
                latest = runs[0]  # Already sorted by created_at DESC

                # Count tickets
                ticket_dir = repo_path / ".codex-autorunner" / "tickets"
                total = 0
                done = 0
                for path in list_ticket_paths(ticket_dir):
                    total += 1
                    try:
                        if ticket_is_done(path):
                            done += 1
                    except Exception:
                        continue

                if total == 0:
                    return None

                # Extract current step from ticket_engine state
                state = latest.state if isinstance(latest.state, dict) else {}
                engine = state.get("ticket_engine") if isinstance(state, dict) else {}
                engine = engine if isinstance(engine, dict) else {}
                current_step = engine.get("total_turns")

                failure_payload = get_failure_payload(latest)
                failure_summary = (
                    format_failure_summary(failure_payload) if failure_payload else None
                )
                return {
                    "status": latest.status.value,
                    "done_count": done,
                    "total_count": total,
                    "current_step": current_step,
                    "failure": failure_payload,
                    "failure_summary": failure_summary,
                }
        except Exception:
            return None

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
            config=context.config,
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
                config=context.config,
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
                handoff_candidate: Optional[dict] = None
                non_summary_candidate: Optional[dict] = None
                turn_summary_candidate: Optional[dict] = None
                error_candidate: Optional[dict] = None

                for seq_dir in seq_dirs:
                    seq = int(seq_dir.name)
                    dispatch_path = seq_dir / "DISPATCH.md"
                    dispatch, errors = parse_dispatch(dispatch_path)
                    if errors or dispatch is None:
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
                        paused = store.list_flow_runs(
                            flow_type="ticket_flow", status=FlowRunStatus.PAUSED
                        )
                except Exception:
                    continue
                if not paused:
                    continue
                for record in paused:
                    record_input = dict(record.input_data or {})
                    latest = _latest_dispatch(repo_root, str(record.id), record_input)
                    if not latest or not latest.get("dispatch"):
                        continue
                    seq = int(latest.get("seq") or 0)
                    if seq <= 0:
                        continue
                    if _dismissal_key(str(record.id), seq) in dismissals:
                        continue
                    failure_payload = get_failure_payload(record)
                    failure_summary = (
                        format_failure_summary(failure_payload)
                        if failure_payload
                        else None
                    )
                    # Reconcile stale inbox items: if reply history already
                    # reached this dispatch seq, treat it as resolved.
                    if (
                        _latest_reply_history_seq(
                            repo_root, str(record.id), record_input
                        )
                        >= seq
                    ):
                        continue
                    messages.append(
                        {
                            "item_type": "run_dispatch",
                            "next_action": "reply_and_resume",
                            "repo_id": snap.id,
                            "repo_display_name": snap.display_name,
                            "repo_path": str(snap.path),
                            "run_id": record.id,
                            "run_created_at": record.created_at,
                            "status": record.status.value,
                            "seq": latest["seq"],
                            "dispatch": latest["dispatch"],
                            "message": latest["dispatch"],
                            "files": latest.get("files") or [],
                            "failure": failure_payload,
                            "failure_summary": failure_summary,
                            "open_url": f"/repos/{snap.id}/?tab=inbox&run_id={record.id}",
                        }
                    )
            messages.sort(key=lambda m: (m.get("run_created_at") or ""), reverse=True)
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
            dismissed_at = datetime.now(timezone.utc).isoformat()
            items = _load_hub_inbox_dismissals(snapshot.path)
            items[_dismissal_key(run_id, seq)] = {
                "repo_id": repo_id,
                "run_id": run_id,
                "seq": seq,
                "reason": reason or None,
                "dismissed_at": dismissed_at,
            }
            _save_hub_inbox_dismissals(snapshot.path, items)
        return {
            "status": "ok",
            "dismissed": {
                "repo_id": repo_id,
                "run_id": run_id,
                "seq": seq,
                "reason": reason or None,
                "dismissed_at": dismissed_at,
            },
        }

    @app.get("/hub/repos")
    async def list_repos():
        safe_log(app.state.logger, logging.INFO, "Hub list_repos")
        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        await _refresh_mounts(snapshots)

        def _enrich_repo(snap):
            repo_dict = _add_mount_info(snap.to_dict(context.config.root))
            if snap.initialized and snap.exists_on_disk:
                repo_dict["ticket_flow"] = _get_ticket_flow_summary(snap.path)
            else:
                repo_dict["ticket_flow"] = None
            return repo_dict

        return {
            "last_scan_at": context.supervisor.state.last_scan_at,
            "repos": [_enrich_repo(snap) for snap in snapshots],
        }

    @app.get("/hub/version")
    def hub_version():
        return {"asset_version": app.state.asset_version}

    @app.post("/hub/repos/scan")
    async def scan_repos():
        safe_log(app.state.logger, logging.INFO, "Hub scan_repos")
        snapshots = await asyncio.to_thread(context.supervisor.scan)
        await _refresh_mounts(snapshots)

        def _enrich_repo(snap):
            repo_dict = _add_mount_info(snap.to_dict(context.config.root))
            if snap.initialized and snap.exists_on_disk:
                repo_dict["ticket_flow"] = _get_ticket_flow_summary(snap.path)
            else:
                repo_dict["ticket_flow"] = None
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


def _resolve_auth_token(
    env_name: str, *, env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    if not env_name:
        return None
    source = env if env is not None else os.environ
    value = source.get(env_name)
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
