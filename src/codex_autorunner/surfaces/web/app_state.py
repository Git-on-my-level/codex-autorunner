import asyncio
import json
import logging
import os
import shlex
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, cast

from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...agents.registry import validate_agent_id
from ...bootstrap import ensure_hub_car_shim
from ...core.config import (
    AppServerConfig,
    ConfigError,
    HubConfig,
    _normalize_base_path,
    collect_env_overrides,
    derive_repo_config,
    load_hub_config,
    load_repo_config,
    resolve_env_for_root,
)
from ...core.logging_utils import safe_log, setup_rotating_logger
from ...core.optional_dependencies import require_optional_dependencies
from ...core.runtime import RuntimeContext
from ...core.runtime_services import RuntimeServices
from ...core.state import load_state
from ...core.utils import atomic_write
from ...integrations.agents import build_backend_orchestrator
from ...integrations.agents.opencode_supervisor_factory import (
    build_opencode_supervisor_from_repo_config,
)
from ...integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from ...integrations.app_server.client import ApprovalHandler, NotificationHandler
from ...integrations.app_server.env import build_app_server_env
from ...integrations.app_server.event_buffer import AppServerEventBuffer
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from ...integrations.app_server.threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from ...tickets.replies import resolve_reply_paths
from .hub_jobs import HubJobManager
from .runner_manager import RunnerManager
from .static_assets import (
    asset_version,
    materialize_static_assets,
    require_static_assets,
)
from .terminal_sessions import parse_tui_idle_seconds, prune_terminal_registry

_HUB_INBOX_DISMISSALS_FILENAME = "hub_inbox_dismissals.json"
_DEV_INCLUDE_ROOT_REPO_ENV = "CAR_DEV_INCLUDE_ROOT_REPO"


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
    runtime_services: RuntimeServices
    voice_config: Any
    voice_missing_reason: Optional[str]
    voice_service: Optional[Any]
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
    supervisor: Any
    job_manager: HubJobManager
    app_server_supervisor: Optional[WorkspaceAppServerSupervisor]
    app_server_prune_interval: Optional[float]
    app_server_threads: AppServerThreadRegistry
    app_server_events: AppServerEventBuffer
    opencode_supervisor: Optional[OpenCodeSupervisor]
    opencode_prune_interval: Optional[float]
    runtime_services: RuntimeServices
    static_dir: Path
    static_assets_context: Optional[object]
    asset_version: str
    logger: logging.Logger


@dataclass(frozen=True)
class ServerOverrides:
    allowed_hosts: Optional[list[str]] = None
    allowed_origins: Optional[list[str]] = None
    auth_token_env: Optional[str] = None


def _hub_inbox_dismissals_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / _HUB_INBOX_DISMISSALS_FILENAME


def _env_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_codex_autorunner_source_tree(root: Path) -> bool:
    return (
        (root / "src" / "codex_autorunner" / "__init__.py").exists()
        and (root / "Makefile").exists()
        and (root / "pyproject.toml").exists()
    )


def _dismissal_key(run_id: str, seq: int) -> str:
    return f"{run_id}:{seq}"


def _message_resolution_key(
    run_id: str, *, item_type: str, seq: Optional[int] = None
) -> str:
    normalized = (item_type or "").strip() or "run_dispatch"
    if normalized == "run_dispatch":
        if isinstance(seq, int) and seq > 0:
            return _dismissal_key(run_id, seq)
        return f"{run_id}:run_dispatch"
    if isinstance(seq, int) and seq > 0:
        return f"{run_id}:{normalized}:{seq}"
    return f"{run_id}:{normalized}"


def _message_resolution_state(item_type: str) -> str:
    if item_type == "run_dispatch":
        return "pending_dispatch"
    return "terminal_attention"


def _message_resolvable_actions(item_type: str) -> list[str]:
    if item_type == "run_dispatch":
        return ["reply_resume", "dismiss"]
    if item_type in {"run_failed", "run_stopped"}:
        return ["dismiss", "archive_run", "restart"]
    return ["dismiss", "reply_resume", "restart"]


def _find_message_resolution(
    dismissals: dict[str, dict[str, Any]],
    *,
    run_id: str,
    item_type: str,
    seq: Optional[int],
) -> Optional[dict[str, Any]]:
    keys = [_message_resolution_key(run_id, item_type=item_type, seq=seq)]
    if item_type != "run_dispatch" and isinstance(seq, int) and seq > 0:
        keys.append(_message_resolution_key(run_id, item_type=item_type))
    for key in keys:
        entry = dismissals.get(key)
        if isinstance(entry, dict):
            return entry
    return None


def _record_message_resolution(
    *,
    repo_root: Path,
    repo_id: str,
    run_id: str,
    item_type: str,
    seq: Optional[int],
    action: str,
    reason: Optional[str],
    actor: Optional[str],
) -> dict[str, Any]:
    items = _load_hub_inbox_dismissals(repo_root)
    resolved_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "repo_id": repo_id,
        "run_id": run_id,
        "item_type": item_type,
        "seq": seq if isinstance(seq, int) and seq > 0 else None,
        "action": action,
        "reason": reason or None,
        "resolved_at": resolved_at,
        "resolution_state": "dismissed" if action == "dismiss" else "resolved",
    }
    if actor:
        payload["resolved_by"] = actor
    key = _message_resolution_key(run_id, item_type=item_type, seq=seq)
    items[key] = payload
    _save_hub_inbox_dismissals(repo_root, items)
    return payload


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
    import posixpath

    normalized = posixpath.normpath(raw)
    if normalized.startswith("/") or normalized.startswith(".."):
        return False
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
        base_env_dict: Optional[dict[str, str]] = dict(base_env) if base_env else None
        return build_app_server_env(
            config.command,
            workspace_root,
            state_dir,
            logger=logger,
            event_prefix=event_prefix,
            base_env=base_env_dict,
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


def build_app_context(
    repo_root: Optional[Path],
    base_path: Optional[str],
    hub_config: Optional[HubConfig] = None,
) -> AppContext:
    from ...voice import VoiceConfig, VoiceService

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
        if voice_config.provider in {"local_whisper", "local"}:
            require_optional_dependencies(
                feature="voice (local_whisper)",
                deps=(("faster_whisper", "faster-whisper"),),
                extra="voice-local",
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
    opencode_supervisor = build_opencode_supervisor_from_repo_config(
        config,
        workspace_root=engine.repo_root,
        logger=logger,
        base_env=env,
    )
    if opencode_supervisor is None:
        safe_log(
            logger,
            logging.INFO,
            "OpenCode command unavailable; skipping opencode supervisor.",
        )
        opencode_prune_interval = None
    else:
        opencode_prune_interval = _app_server_prune_interval(
            config.app_server.idle_ttl_seconds
        )
    runtime_services = RuntimeServices(
        app_server_supervisor=app_server_supervisor,
        opencode_supervisor=opencode_supervisor,
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
        runtime_services=runtime_services,
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


def apply_app_context(app, context: AppContext) -> None:
    app.state.base_path = context.base_path
    app.state.env = context.env
    app.state.logger = context.logger
    app.state.engine = context.engine
    app.state.config = context.engine.config
    app.state.manager = context.manager
    app.state.app_server_supervisor = context.app_server_supervisor
    app.state.app_server_prune_interval = context.app_server_prune_interval
    app.state.app_server_threads = context.app_server_threads
    app.state.app_server_events = context.app_server_events
    app.state.opencode_supervisor = context.opencode_supervisor
    app.state.opencode_prune_interval = context.opencode_prune_interval
    app.state.runtime_services = context.runtime_services
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


def build_hub_context(
    hub_root: Optional[Path], base_path: Optional[str]
) -> HubAppContext:
    import sys

    from ...core.hub import HubSupervisor

    config = load_hub_config(hub_root or Path.cwd())
    dev_include_root_repo = _env_truthy(os.getenv(_DEV_INCLUDE_ROOT_REPO_ENV))
    dev_mode_root_repo_enabled = False
    if (
        dev_include_root_repo
        and not config.include_root_repo
        and _is_codex_autorunner_source_tree(config.root)
    ):
        config = replace(config, include_root_repo=True)
        dev_mode_root_repo_enabled = True
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
    if dev_mode_root_repo_enabled:
        safe_log(
            logger,
            logging.INFO,
            "Dev mode: enabling hub root repo discovery for source checkout",
        )
        try:
            snapshots = supervisor.list_repos(use_cache=False)
            if not snapshots:
                supervisor.scan()
        except Exception as exc:
            safe_log(
                logger,
                logging.WARNING,
                "Dev mode root repo bootstrap scan failed",
                exc=exc,
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
    opencode_supervisor = build_opencode_supervisor_from_repo_config(
        cast(Any, config),
        workspace_root=config.root,
        logger=logger,
        base_env=resolve_env_for_root(config.root),
    )
    if opencode_supervisor is None:
        safe_log(
            logger,
            logging.INFO,
            "OpenCode command unavailable; skipping opencode supervisor.",
        )
        opencode_prune_interval = None
    else:
        opencode_prune_interval = _app_server_prune_interval(
            config.app_server.idle_ttl_seconds
        )
    runtime_services = RuntimeServices(
        app_server_supervisor=app_server_supervisor,
        opencode_supervisor=opencode_supervisor,
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
        runtime_services=runtime_services,
        static_dir=static_dir,
        static_assets_context=static_context,
        asset_version=asset_version(static_dir),
        logger=logger,
    )


def apply_hub_context(app, context: HubAppContext) -> None:
    app.state.base_path = context.base_path
    app.state.logger = context.logger
    app.state.config = context.config
    app.state.job_manager = context.job_manager
    app.state.app_server_supervisor = context.app_server_supervisor
    app.state.app_server_prune_interval = context.app_server_prune_interval
    app.state.app_server_threads = context.app_server_threads
    app.state.app_server_events = context.app_server_events
    app.state.opencode_supervisor = context.opencode_supervisor
    app.state.opencode_prune_interval = context.opencode_prune_interval
    app.state.runtime_services = context.runtime_services
    app.state.static_dir = context.static_dir
    app.state.static_assets_context = context.static_assets_context
    app.state.asset_version = context.asset_version
    app.state.hub_supervisor = context.supervisor
