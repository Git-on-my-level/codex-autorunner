import asyncio
import dataclasses
import enum
import importlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, cast

from ..bootstrap import seed_repo_files
from ..discovery import DiscoveryRecord, discover_and_init
from ..manifest import (
    Manifest,
    ManifestAgentWorkspace,
    ManifestRepo,
    ensure_unique_repo_id,
    load_manifest,
    normalize_manifest_destination,
    sanitize_repo_id,
    save_manifest,
)
from ..tickets.outbox import set_lifecycle_emitter
from .archive import (
    ArchiveProfile,
    archive_workspace_for_fresh_start,
    archive_workspace_managed_threads,
    archive_worktree_snapshot,
    build_snapshot_id,
    dirty_car_state_paths,
    resolve_worktree_archive_intent,
)
from .archive_retention import resolve_worktree_archive_retention_policy
from .chat_bindings import repo_has_active_non_pma_chat_binding
from .config import (
    ConfigError,
    HubConfig,
    RepoConfig,
    derive_repo_config,
    load_hub_config,
    load_repo_config,
)
from .destinations import (
    DockerDestination,
    default_car_docker_container_name,
    default_local_destination,
    resolve_effective_agent_workspace_destination,
    resolve_effective_repo_destination,
)
from .flows import FlowRunStatus, FlowStore, archive_flow_run_artifacts
from .force_attestation import enforce_force_attestation
from .git_utils import (
    GitError,
    git_available,
    git_branch,
    git_default_branch,
    git_head_sha,
    git_is_clean,
    git_upstream_status,
    run_git,
)
from .hub_lifecycle import (
    HubLifecycleWorker,
    LifecycleEventProcessor,
    LifecycleRetryPolicy,
)
from .lifecycle_events import (
    LifecycleEvent,
    LifecycleEventEmitter,
    LifecycleEventStore,
    LifecycleEventType,
)
from .locks import DEFAULT_RUNNER_CMD_HINTS, assess_lock, process_alive
from .orchestration.sqlite import open_orchestration_sqlite
from .pma_automation_store import DEFAULT_PMA_LANE_ID, PmaAutomationStore
from .pma_dispatch_interceptor import PmaDispatchInterceptor
from .pma_queue import PmaQueue
from .pma_reactive import PmaReactiveStore
from .pma_safety import PmaSafetyChecker, PmaSafetyConfig
from .pma_thread_store import PmaThreadStore
from .ports.backend_orchestrator import (
    BackendOrchestrator as BackendOrchestratorProtocol,
)
from .runner_controller import ProcessRunnerController, SpawnRunnerFn
from .runtime import RuntimeContext
from .state import RunnerState, load_state, now_iso
from .state_roots import resolve_hub_agent_workspace_root
from .types import AppServerSupervisorFactory, BackendFactory
from .utils import atomic_write, is_within, subprocess_env

logger = logging.getLogger("codex_autorunner.hub")

BackendFactoryBuilder = Callable[[Path, RepoConfig], BackendFactory]
AppServerSupervisorFactoryBuilder = Callable[[RepoConfig], AppServerSupervisorFactory]
BackendOrchestratorBuilder = Callable[[Path, RepoConfig], BackendOrchestratorProtocol]


def _git_failure_detail(proc) -> str:
    return (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"


def _resolve_ref_sha(repo_root: Path, ref: str) -> str:
    try:
        proc = run_git(["rev-parse", "--verify", ref], repo_root, check=False)
    except GitError as exc:
        raise ValueError(f"git rev-parse failed for {ref}: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(f"Unable to resolve ref {ref}: {_git_failure_detail(proc)}")
    sha = (proc.stdout or "").strip()
    if not sha:
        raise ValueError(f"Unable to resolve ref {ref}: empty output")
    return sha


def _load_managed_runtime_module() -> Any:
    try:
        return importlib.import_module("codex_autorunner.agents.managed_runtime")
    except Exception:
        return None


def _coerce_runtime_preflight_payload(result: Any) -> Optional[dict[str, Any]]:
    if result is None:
        return None
    if isinstance(result, dict):
        return dict(result)
    payload: dict[str, Any] = {}
    for field in ("runtime_id", "status", "version", "launch_mode", "message", "fix"):
        value = getattr(result, field, None)
        if value is not None:
            payload[field] = value
    return payload or None


def known_agent_workspace_runtime_ids() -> tuple[str, ...]:
    module = _load_managed_runtime_module()
    if module is not None:
        for attr in (
            "managed_agent_workspace_runtime_ids",
            "list_managed_agent_workspace_runtime_ids",
            "known_agent_workspace_runtime_ids",
        ):
            func = getattr(module, attr, None)
            if not callable(func):
                continue
            try:
                raw_ids = func()
            except Exception:
                continue
            normalized = tuple(
                sorted(
                    {
                        str(item).strip().lower()
                        for item in (raw_ids or ())
                        if str(item).strip()
                    }
                )
            )
            if normalized:
                return normalized
    return ("zeroclaw",)


def probe_agent_workspace_runtime(
    hub_config: HubConfig,
    workspace: ManifestAgentWorkspace,
) -> Optional[dict[str, Any]]:
    module = _load_managed_runtime_module()
    if module is None:
        return None
    for attr in (
        "probe_agent_workspace_runtime",
        "preflight_agent_workspace_runtime",
    ):
        func = getattr(module, attr, None)
        if not callable(func):
            continue
        for kwargs in (
            {"hub_config": hub_config, "workspace": workspace},
            {"config": hub_config, "workspace": workspace},
        ):
            try:
                return _coerce_runtime_preflight_payload(func(**kwargs))
            except TypeError:
                continue
    return None


def _runtime_preflight_blocks_enable(
    preflight: Optional[Mapping[str, Any]],
) -> bool:
    if not preflight:
        return False
    status = str(preflight.get("status") or "").strip().lower()
    return bool(status and status not in {"ready", "deferred"})


class RepoStatus(str, enum.Enum):
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"
    LOCKED = "locked"
    MISSING = "missing"
    INIT_ERROR = "init_error"


class LockStatus(str, enum.Enum):
    UNLOCKED = "unlocked"
    LOCKED_ALIVE = "locked_alive"
    LOCKED_STALE = "locked_stale"


@dataclasses.dataclass
class RepoSnapshot:
    id: str
    path: Path
    display_name: str
    enabled: bool
    auto_run: bool
    worktree_setup_commands: Optional[List[str]]
    kind: str  # base|worktree
    worktree_of: Optional[str]
    branch: Optional[str]
    exists_on_disk: bool
    is_clean: Optional[bool]
    initialized: bool
    init_error: Optional[str]
    status: RepoStatus
    lock_status: LockStatus
    last_run_id: Optional[int]
    last_run_started_at: Optional[str]
    last_run_finished_at: Optional[str]
    last_exit_code: Optional[int]
    runner_pid: Optional[int]
    last_run_duration_seconds: Optional[float] = None
    effective_destination: Dict[str, Any] = dataclasses.field(
        default_factory=default_local_destination
    )
    chat_bound: bool = False
    chat_bound_thread_count: int = 0
    pma_chat_bound_thread_count: int = 0
    discord_chat_bound_thread_count: int = 0
    telegram_chat_bound_thread_count: int = 0
    non_pma_chat_bound_thread_count: int = 0
    unbound_managed_thread_count: int = 0
    cleanup_blocked_by_chat_binding: bool = False
    has_car_state: bool = False
    resource_kind: str = "repo"

    def to_dict(self, hub_root: Path) -> Dict[str, object]:
        try:
            rel_path = self.path.relative_to(hub_root)
        except Exception:
            rel_path = self.path
        return {
            "id": self.id,
            "path": str(rel_path),
            "display_name": self.display_name,
            "enabled": self.enabled,
            "auto_run": self.auto_run,
            "worktree_setup_commands": self.worktree_setup_commands,
            "kind": self.kind,
            "worktree_of": self.worktree_of,
            "branch": self.branch,
            "exists_on_disk": self.exists_on_disk,
            "is_clean": self.is_clean,
            "initialized": self.initialized,
            "init_error": self.init_error,
            "status": self.status.value,
            "lock_status": self.lock_status.value,
            "last_run_id": self.last_run_id,
            "last_run_started_at": self.last_run_started_at,
            "last_run_finished_at": self.last_run_finished_at,
            "last_run_duration_seconds": self.last_run_duration_seconds,
            "last_exit_code": self.last_exit_code,
            "runner_pid": self.runner_pid,
            "effective_destination": self.effective_destination,
            "chat_bound": self.chat_bound,
            "chat_bound_thread_count": self.chat_bound_thread_count,
            "pma_chat_bound_thread_count": self.pma_chat_bound_thread_count,
            "discord_chat_bound_thread_count": self.discord_chat_bound_thread_count,
            "telegram_chat_bound_thread_count": self.telegram_chat_bound_thread_count,
            "non_pma_chat_bound_thread_count": self.non_pma_chat_bound_thread_count,
            "unbound_managed_thread_count": self.unbound_managed_thread_count,
            "cleanup_blocked_by_chat_binding": self.cleanup_blocked_by_chat_binding,
            "has_car_state": self.has_car_state,
            "resource_kind": self.resource_kind,
        }


@dataclasses.dataclass
class AgentWorkspaceSnapshot:
    id: str
    runtime: str
    path: Path
    display_name: str
    enabled: bool
    exists_on_disk: bool
    effective_destination: Dict[str, Any] = dataclasses.field(
        default_factory=default_local_destination
    )
    resource_kind: str = "agent_workspace"

    def to_dict(self, hub_root: Path) -> Dict[str, object]:
        try:
            rel_path = self.path.relative_to(hub_root)
        except Exception:
            rel_path = self.path
        return {
            "id": self.id,
            "runtime": self.runtime,
            "path": str(rel_path),
            "display_name": self.display_name,
            "enabled": self.enabled,
            "exists_on_disk": self.exists_on_disk,
            "effective_destination": self.effective_destination,
            "resource_kind": self.resource_kind,
        }


@dataclasses.dataclass
class HubState:
    last_scan_at: Optional[str]
    repos: List[RepoSnapshot]
    agent_workspaces: List[AgentWorkspaceSnapshot] = dataclasses.field(
        default_factory=list
    )
    pinned_parent_repo_ids: List[str] = dataclasses.field(default_factory=list)

    def to_dict(self, hub_root: Path) -> Dict[str, object]:
        return {
            "last_scan_at": self.last_scan_at,
            "repos": [repo.to_dict(hub_root) for repo in self.repos],
            "agent_workspaces": [
                workspace.to_dict(hub_root) for workspace in self.agent_workspaces
            ],
            "pinned_parent_repo_ids": list(self.pinned_parent_repo_ids or []),
        }


def read_lock_status(lock_path: Path) -> LockStatus:
    if not lock_path.exists():
        return LockStatus.UNLOCKED
    assessment = assess_lock(
        lock_path,
        expected_cmd_substrings=DEFAULT_RUNNER_CMD_HINTS,
    )
    if not assessment.freeable and assessment.pid and process_alive(assessment.pid):
        return LockStatus.LOCKED_ALIVE
    return LockStatus.LOCKED_STALE


def load_hub_state(state_path: Path, hub_root: Path) -> HubState:
    if not state_path.exists():
        return HubState(
            last_scan_at=None,
            repos=[],
            agent_workspaces=[],
            pinned_parent_repo_ids=[],
        )
    data = state_path.read_text(encoding="utf-8")
    try:
        import json

        payload = json.loads(data)
    except Exception as exc:
        logger.warning("Failed to parse hub state from %s: %s", state_path, exc)
        return HubState(
            last_scan_at=None,
            repos=[],
            agent_workspaces=[],
            pinned_parent_repo_ids=[],
        )
    last_scan_at = payload.get("last_scan_at")
    pinned_parent_repo_ids = _normalize_pinned_parent_repo_ids(
        payload.get("pinned_parent_repo_ids")
    )
    repos_payload = payload.get("repos") or []
    agent_workspaces_payload = payload.get("agent_workspaces") or []
    repos: List[RepoSnapshot] = []
    agent_workspaces: List[AgentWorkspaceSnapshot] = []
    for entry in repos_payload:
        try:
            repo = RepoSnapshot(
                id=str(entry.get("id")),
                path=hub_root / entry.get("path", ""),
                display_name=str(entry.get("display_name", "")),
                enabled=bool(entry.get("enabled", True)),
                auto_run=bool(entry.get("auto_run", False)),
                worktree_setup_commands=(
                    [
                        str(cmd).strip()
                        for cmd in (entry.get("worktree_setup_commands") or [])
                        if isinstance(cmd, str) and str(cmd).strip()
                    ]
                    or None
                ),
                kind=str(entry.get("kind", "base")),
                worktree_of=entry.get("worktree_of"),
                branch=entry.get("branch"),
                exists_on_disk=bool(entry.get("exists_on_disk", False)),
                is_clean=entry.get("is_clean"),
                initialized=bool(entry.get("initialized", False)),
                init_error=entry.get("init_error"),
                status=RepoStatus(entry.get("status", RepoStatus.UNINITIALIZED.value)),
                lock_status=LockStatus(
                    entry.get("lock_status", LockStatus.UNLOCKED.value)
                ),
                last_run_id=entry.get("last_run_id"),
                last_run_started_at=entry.get("last_run_started_at"),
                last_run_finished_at=entry.get("last_run_finished_at"),
                last_run_duration_seconds=entry.get("last_run_duration_seconds"),
                last_exit_code=entry.get("last_exit_code"),
                runner_pid=entry.get("runner_pid"),
                effective_destination=(
                    normalize_manifest_destination(entry.get("effective_destination"))
                    or default_local_destination()
                ),
            )
            repos.append(repo)
        except Exception as exc:
            repo_id = entry.get("id", "unknown")
            logger.warning(
                "Failed to load repo snapshot for id=%s from hub state: %s",
                repo_id,
                exc,
            )
            continue
    for entry in agent_workspaces_payload:
        try:
            workspace = AgentWorkspaceSnapshot(
                id=str(entry.get("id")),
                runtime=str(entry.get("runtime", "")),
                path=hub_root / entry.get("path", ""),
                display_name=str(entry.get("display_name", "")),
                enabled=bool(entry.get("enabled", True)),
                exists_on_disk=bool(entry.get("exists_on_disk", False)),
                effective_destination=(
                    normalize_manifest_destination(entry.get("effective_destination"))
                    or default_local_destination()
                ),
            )
            agent_workspaces.append(workspace)
        except Exception as exc:
            workspace_id = entry.get("id", "unknown")
            logger.warning(
                "Failed to load agent workspace snapshot for id=%s from hub state: %s",
                workspace_id,
                exc,
            )
            continue
    return HubState(
        last_scan_at=last_scan_at,
        repos=repos,
        agent_workspaces=agent_workspaces,
        pinned_parent_repo_ids=pinned_parent_repo_ids,
    )


def save_hub_state(state_path: Path, state: HubState, hub_root: Path) -> None:
    payload = state.to_dict(hub_root)
    atomic_write(state_path, json.dumps(payload, indent=2) + "\n")
    try:
        _save_pma_threads_artifact(hub_root)
    except Exception as exc:
        logger.warning("Failed to write PMA thread snapshot artifact: %s", exc)


def _save_pma_threads_artifact(hub_root: Path) -> None:
    from .pma_context import _snapshot_pma_threads

    payload = {
        "generated_at": now_iso(),
        "threads": _snapshot_pma_threads(hub_root),
    }
    artifact_path = hub_root / ".codex-autorunner" / "pma_threads.json"
    atomic_write(artifact_path, json.dumps(payload, indent=2) + "\n")


def _normalize_pinned_parent_repo_ids(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        repo_id = item.strip()
        if not repo_id or repo_id in seen:
            continue
        seen.add(repo_id)
        out.append(repo_id)
    return out


class RepoRunner:
    def __init__(
        self,
        repo_id: str,
        repo_root: Path,
        *,
        repo_config: RepoConfig,
        spawn_fn: Optional[SpawnRunnerFn] = None,
        backend_factory_builder: Optional[BackendFactoryBuilder] = None,
        app_server_supervisor_factory_builder: Optional[
            AppServerSupervisorFactoryBuilder
        ] = None,
        backend_orchestrator_builder: Optional[BackendOrchestratorBuilder] = None,
        agent_id_validator: Optional[Callable[[str], str]] = None,
    ):
        self.repo_id = repo_id
        backend_orchestrator = (
            backend_orchestrator_builder(repo_root, repo_config)
            if backend_orchestrator_builder is not None
            else None
        )
        if backend_orchestrator is None:
            raise ValueError(
                "backend_orchestrator_builder is required for HubSupervisor"
            )
        self._ctx = RuntimeContext(
            repo_root=repo_root,
            config=repo_config,
            backend_orchestrator=backend_orchestrator,
        )
        self._controller = ProcessRunnerController(self._ctx, spawn_fn=spawn_fn)

    @property
    def running(self) -> bool:
        return self._controller.running

    def start(self, once: bool = False) -> None:
        self._controller.start(once=once)

    def stop(self) -> None:
        self._controller.stop()

    def reconcile(self) -> None:
        self._controller.reconcile()

    def kill(self) -> Optional[int]:
        return self._controller.kill()

    def resume(self, once: bool = False) -> None:
        self._controller.resume(once=once)


class HubSupervisor:
    def __init__(
        self,
        hub_config: HubConfig,
        *,
        spawn_fn: Optional[SpawnRunnerFn] = None,
        backend_factory_builder: Optional[BackendFactoryBuilder] = None,
        app_server_supervisor_factory_builder: Optional[
            AppServerSupervisorFactoryBuilder
        ] = None,
        backend_orchestrator_builder: Optional[BackendOrchestratorBuilder] = None,
        agent_id_validator: Optional[Callable[[str], str]] = None,
        scm_poll_processor: Optional[Callable[[int], dict[str, int]]] = None,
    ):
        self.hub_config = hub_config
        self.state_path = hub_config.root / ".codex-autorunner" / "hub_state.json"
        self._runners: Dict[str, RepoRunner] = {}
        self._spawn_fn = spawn_fn
        self._backend_factory_builder = backend_factory_builder
        self._app_server_supervisor_factory_builder = (
            app_server_supervisor_factory_builder
        )
        self._backend_orchestrator_builder = backend_orchestrator_builder
        self._agent_id_validator = agent_id_validator
        self.state = load_hub_state(self.state_path, self.hub_config.root)
        self._list_cache_at: Optional[float] = None
        self._list_cache: Optional[List[RepoSnapshot]] = None
        self._list_lock = threading.Lock()
        self._lifecycle_emitter = LifecycleEventEmitter(hub_config.root)
        self._lifecycle_event_processor = LifecycleEventProcessor(
            store=self.lifecycle_store,
            process_event=lambda event: self._process_lifecycle_event(event),
            retry_policy=self._build_lifecycle_retry_policy(),
            logger=logger,
        )
        self._lifecycle_worker = HubLifecycleWorker(
            process_once=self._process_lifecycle_event_cycle,
            poll_interval_seconds=5.0,
            join_timeout_seconds=2.0,
            thread_name="lifecycle-event-processor",
            logger=logger,
        )
        self._dispatch_interceptor: Optional[PmaDispatchInterceptor] = None
        self._pma_safety_checker: Optional[PmaSafetyChecker] = None
        self._pma_automation_store: Optional[PmaAutomationStore] = None
        self._pma_lane_worker_starter: Optional[Callable[[str], None]] = None
        self._scm_poll_processor = scm_poll_processor
        self._wire_outbox_lifecycle()
        self._reconcile_startup()
        self._start_lifecycle_event_processor()

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        backend_factory_builder: Optional[BackendFactoryBuilder] = None,
        app_server_supervisor_factory_builder: Optional[
            AppServerSupervisorFactoryBuilder
        ] = None,
        backend_orchestrator_builder: Optional[BackendOrchestratorBuilder] = None,
        scm_poll_processor: Optional[Callable[[int], dict[str, int]]] = None,
    ) -> "HubSupervisor":
        config = load_hub_config(path)
        return cls(
            config,
            backend_factory_builder=backend_factory_builder,
            app_server_supervisor_factory_builder=app_server_supervisor_factory_builder,
            backend_orchestrator_builder=backend_orchestrator_builder,
            scm_poll_processor=scm_poll_processor,
        )

    def scan(self) -> List[RepoSnapshot]:
        self._invalidate_list_cache()
        manifest, records = discover_and_init(self.hub_config)
        snapshots = self._build_snapshots(records)
        agent_workspaces = self._build_agent_workspace_snapshots(
            manifest.agent_workspaces
        )
        pinned_parent_repo_ids = self._prune_pinned_parent_repo_ids(snapshots)
        self.state = HubState(
            last_scan_at=now_iso(),
            repos=snapshots,
            agent_workspaces=agent_workspaces,
            pinned_parent_repo_ids=pinned_parent_repo_ids,
        )
        save_hub_state(self.state_path, self.state, self.hub_config.root)
        return snapshots

    def list_repos(self, *, use_cache: bool = True) -> List[RepoSnapshot]:
        with self._list_lock:
            if use_cache and self._list_cache and self._list_cache_at is not None:
                if time.monotonic() - self._list_cache_at < 2.0:
                    return self._list_cache
            manifest, records = self._manifest_records(manifest_only=True)
            snapshots = self._build_snapshots(records)
            agent_workspaces = self._build_agent_workspace_snapshots(
                manifest.agent_workspaces
            )
            pinned_parent_repo_ids = self._prune_pinned_parent_repo_ids(snapshots)
            self.state = HubState(
                last_scan_at=self.state.last_scan_at,
                repos=snapshots,
                agent_workspaces=agent_workspaces,
                pinned_parent_repo_ids=pinned_parent_repo_ids,
            )
            save_hub_state(self.state_path, self.state, self.hub_config.root)
            self._list_cache = snapshots
            self._list_cache_at = time.monotonic()
            return snapshots

    def list_agent_workspaces(
        self, *, use_cache: bool = True
    ) -> List[AgentWorkspaceSnapshot]:
        self.list_repos(use_cache=use_cache)
        return list(self.state.agent_workspaces)

    def set_parent_repo_pinned(self, repo_id: str, pinned: bool) -> List[str]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        if repo.kind != "base":
            raise ValueError("Only base repos can be pinned")

        with self._list_lock:
            current = list(self.state.pinned_parent_repo_ids or [])
            if pinned:
                if repo_id not in current:
                    current.append(repo_id)
            else:
                current = [item for item in current if item != repo_id]
            self.state = HubState(
                last_scan_at=self.state.last_scan_at,
                repos=self.state.repos,
                agent_workspaces=self.state.agent_workspaces,
                pinned_parent_repo_ids=_normalize_pinned_parent_repo_ids(current),
            )
            save_hub_state(self.state_path, self.state, self.hub_config.root)
            return list(self.state.pinned_parent_repo_ids)

    def create_agent_workspace(
        self,
        *,
        workspace_id: str,
        runtime: str,
        display_name: Optional[str] = None,
        enabled: bool = True,
    ) -> AgentWorkspaceSnapshot:
        self._invalidate_list_cache()
        raw_workspace_id = (workspace_id or "").strip()
        raw_runtime = (runtime or "").strip()
        if not raw_workspace_id:
            raise ValueError("workspace_id is required")
        if not raw_runtime:
            raise ValueError("runtime is required")
        normalized_workspace_id = sanitize_repo_id(raw_workspace_id)
        normalized_runtime = sanitize_repo_id(raw_runtime)
        known_runtimes = set(known_agent_workspace_runtime_ids())
        if normalized_runtime not in known_runtimes:
            supported = ", ".join(sorted(known_runtimes))
            raise ValueError(
                f"Unknown agent workspace runtime '{normalized_runtime}'. "
                f"Supported runtimes: {supported}"
            )

        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        existing = manifest.get_agent_workspace(normalized_workspace_id)
        target = resolve_hub_agent_workspace_root(
            self.hub_config.root,
            runtime=normalized_runtime,
            workspace_id=normalized_workspace_id,
        )
        if existing:
            existing_path = (self.hub_config.root / existing.path).resolve()
            if existing.runtime != normalized_runtime or existing_path != target:
                raise ValueError(
                    "Agent workspace id %s already exists for runtime %s at %s"
                    % (normalized_workspace_id, existing.runtime, existing.path)
                )
        workspace = manifest.ensure_agent_workspace(
            self.hub_config.root,
            workspace_id=normalized_workspace_id,
            runtime=normalized_runtime,
            display_name=display_name or workspace_id,
        )
        workspace.enabled = bool(enabled)
        preflight = (
            probe_agent_workspace_runtime(self.hub_config, workspace)
            if workspace.enabled
            else None
        )
        if _runtime_preflight_blocks_enable(preflight):
            assert preflight is not None
            message = str(preflight.get("message") or "").strip()
            fix = str(preflight.get("fix") or "").strip()
            detail = message or "runtime preflight failed"
            if fix:
                detail = f"{detail} Fix: {fix}"
            raise ValueError(detail)
        target.mkdir(parents=True, exist_ok=True)
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)
        return self._snapshot_for_agent_workspace(normalized_workspace_id)

    def remove_agent_workspace(
        self,
        workspace_id: str,
        *,
        delete_dir: bool = True,
    ) -> None:
        self._invalidate_list_cache()
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        workspace = manifest.get_agent_workspace(workspace_id)
        if not workspace:
            raise ValueError(f"Agent workspace {workspace_id} not found in manifest")

        workspace_root = (self.hub_config.root / workspace.path).resolve()
        if delete_dir and workspace_root.exists():
            shutil.rmtree(workspace_root)

        manifest.agent_workspaces = [
            entry for entry in manifest.agent_workspaces if entry.id != workspace_id
        ]
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)
        self.list_repos(use_cache=False)

    def get_agent_workspace_snapshot(self, workspace_id: str) -> AgentWorkspaceSnapshot:
        return self._snapshot_for_agent_workspace(workspace_id)

    def get_agent_workspace_runtime_readiness(
        self, workspace_id: str
    ) -> Optional[dict[str, Any]]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        workspace = manifest.get_agent_workspace(workspace_id)
        if workspace is None:
            raise ValueError(f"Agent workspace {workspace_id} not found in manifest")
        return probe_agent_workspace_runtime(self.hub_config, workspace)

    def update_agent_workspace(
        self,
        workspace_id: str,
        *,
        enabled: Optional[bool] = None,
        display_name: Optional[str] = None,
    ) -> AgentWorkspaceSnapshot:
        self._invalidate_list_cache()
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        workspace = manifest.get_agent_workspace(workspace_id)
        if not workspace:
            raise ValueError(f"Agent workspace {workspace_id} not found in manifest")

        if enabled is not None:
            workspace.enabled = bool(enabled)
            preflight = (
                probe_agent_workspace_runtime(self.hub_config, workspace)
                if workspace.enabled
                else None
            )
            if _runtime_preflight_blocks_enable(preflight):
                assert preflight is not None
                message = str(preflight.get("message") or "").strip()
                fix = str(preflight.get("fix") or "").strip()
                detail = message or "runtime preflight failed"
                if fix:
                    detail = f"{detail} Fix: {fix}"
                raise ValueError(detail)
        if display_name is not None:
            normalized_display_name = str(display_name).strip()
            if not normalized_display_name:
                raise ValueError("display_name must be non-empty when provided")
            workspace.display_name = normalized_display_name

        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)
        return self._snapshot_for_agent_workspace(workspace_id)

    def set_agent_workspace_destination(
        self, workspace_id: str, destination: Optional[Dict[str, Any]]
    ) -> AgentWorkspaceSnapshot:
        self._invalidate_list_cache()
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        workspace = manifest.get_agent_workspace(workspace_id)
        if not workspace:
            raise ValueError(f"Agent workspace {workspace_id} not found in manifest")
        workspace.destination = normalize_manifest_destination(destination)
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)
        return self._snapshot_for_agent_workspace(workspace_id)

    def _reconcile_startup(self) -> None:
        try:
            _, records = self._manifest_records(manifest_only=True)
        except Exception as exc:
            logger.warning("Failed to load hub manifest for reconciliation: %s", exc)
            return
        for record in records:
            if not record.initialized:
                continue
            try:
                repo_config = derive_repo_config(
                    self.hub_config, record.absolute_path, load_env=False
                )
                backend_orchestrator = (
                    self._backend_orchestrator_builder(
                        record.absolute_path, repo_config
                    )
                    if self._backend_orchestrator_builder is not None
                    else None
                )
                controller = ProcessRunnerController(
                    RuntimeContext(
                        repo_root=record.absolute_path,
                        config=repo_config,
                        backend_orchestrator=backend_orchestrator,
                    )
                )
                controller.reconcile()
            except Exception as exc:
                logger.warning(
                    "Failed to reconcile runner state for %s: %s",
                    record.absolute_path,
                    exc,
                )

    def run_repo(self, repo_id: str, once: bool = False) -> RepoSnapshot:
        runner = self._ensure_runner(repo_id)
        assert runner is not None
        runner.start(once=once)
        return self._snapshot_for_repo(repo_id)

    def stop_repo(self, repo_id: str) -> RepoSnapshot:
        runner = self._ensure_runner(repo_id, allow_uninitialized=True)
        if runner:
            runner.stop()
        return self._snapshot_for_repo(repo_id)

    def _stop_runner_and_wait_for_exit(
        self,
        *,
        repo_id: str,
        repo_path: Path,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        runner = self._ensure_runner(repo_id, allow_uninitialized=True)
        if not runner:
            return

        runner.stop()
        deadline = time.monotonic() + max(timeout_seconds, poll_interval_seconds)
        lock_path = repo_path / ".codex-autorunner" / "lock"
        state_path = repo_path / ".codex-autorunner" / "state.sqlite3"
        last_error: Optional[Exception] = None

        while True:
            runner.reconcile()

            try:
                lock_status = read_lock_status(lock_path)
                runner_state = load_state(state_path) if state_path.exists() else None
                last_error = None
            except Exception as exc:
                last_error = exc
            else:
                if lock_status != LockStatus.LOCKED_ALIVE and (
                    runner_state is None
                    or (
                        runner_state.runner_pid is None
                        and runner_state.status != "running"
                    )
                ):
                    return

            if time.monotonic() >= deadline:
                message = f"Timed out waiting for repo runner to stop before proceeding: {repo_id}"
                if last_error is not None:
                    raise ValueError(f"{message} ({last_error})") from last_error
                raise ValueError(message)

            time.sleep(poll_interval_seconds)

    def resume_repo(self, repo_id: str, once: bool = False) -> RepoSnapshot:
        runner = self._ensure_runner(repo_id)
        assert runner is not None
        runner.resume(once=once)
        return self._snapshot_for_repo(repo_id)

    def kill_repo(self, repo_id: str) -> RepoSnapshot:
        runner = self._ensure_runner(repo_id, allow_uninitialized=True)
        if runner:
            runner.kill()
        return self._snapshot_for_repo(repo_id)

    def init_repo(self, repo_id: str) -> RepoSnapshot:
        self._invalidate_list_cache()
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repo_path = (self.hub_config.root / repo.path).resolve()
        if not repo_path.exists():
            raise ValueError(f"Repo {repo_id} missing on disk")
        seed_repo_files(repo_path, force=False, git_required=False)
        return self._snapshot_for_repo(repo_id)

    def sync_main(self, repo_id: str) -> RepoSnapshot:
        self._invalidate_list_cache()
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repo_root = (self.hub_config.root / repo.path).resolve()
        if not repo_root.exists():
            raise ValueError(f"Repo {repo_id} missing on disk")
        if not git_available(repo_root):
            raise ValueError(f"Repo {repo_id} is not a git repository")
        if not git_is_clean(repo_root):
            raise ValueError("Repo has uncommitted changes; commit or stash first")

        try:
            proc = run_git(
                ["fetch", "--prune", "origin"],
                repo_root,
                check=False,
                timeout_seconds=120,
            )
        except GitError as exc:
            raise ValueError(f"git fetch failed: {exc}") from exc
        if proc.returncode != 0:
            raise ValueError(f"git fetch failed: {_git_failure_detail(proc)}")

        default_branch = git_default_branch(repo_root)
        if not default_branch:
            raise ValueError("Unable to resolve origin default branch")

        try:
            proc = run_git(["checkout", default_branch], repo_root, check=False)
        except GitError as exc:
            raise ValueError(f"git checkout failed: {exc}") from exc
        if proc.returncode != 0:
            try:
                proc = run_git(
                    ["checkout", "-B", default_branch, f"origin/{default_branch}"],
                    repo_root,
                    check=False,
                )
            except GitError as exc:
                raise ValueError(f"git checkout failed: {exc}") from exc
            if proc.returncode != 0:
                raise ValueError(f"git checkout failed: {_git_failure_detail(proc)}")

        try:
            proc = run_git(
                ["pull", "--ff-only", "origin", default_branch],
                repo_root,
                check=False,
                timeout_seconds=120,
            )
        except GitError as exc:
            raise ValueError(f"git pull failed: {exc}") from exc
        if proc.returncode != 0:
            raise ValueError(f"git pull failed: {_git_failure_detail(proc)}")
        local_sha = git_head_sha(repo_root)
        if not local_sha:
            raise ValueError("Unable to resolve local HEAD after sync")
        origin_ref = f"refs/remotes/origin/{default_branch}"
        origin_sha = _resolve_ref_sha(repo_root, origin_ref)
        if local_sha != origin_sha:
            raise ValueError(
                "Sync main did not land on origin/%s: local=%s origin=%s. "
                "Local branch may contain extra commits; resolve divergence first."
                % (default_branch, local_sha[:12], origin_sha[:12])
            )
        return self._snapshot_for_repo(repo_id)

    def create_repo(
        self,
        repo_id: str,
        repo_path: Optional[Path] = None,
        git_init: bool = True,
        force: bool = False,
    ) -> RepoSnapshot:
        self._invalidate_list_cache()
        display_name = repo_id
        safe_repo_id = sanitize_repo_id(repo_id)
        base_dir = self.hub_config.repos_root
        target = repo_path if repo_path is not None else Path(safe_repo_id)
        if not target.is_absolute():
            target = (base_dir / target).resolve()
        else:
            target = target.resolve()

        try:
            target.relative_to(base_dir)
        except ValueError as exc:
            raise ValueError(
                f"Repo path must live under repos_root ({base_dir})"
            ) from exc

        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        existing = manifest.get(safe_repo_id)
        if existing:
            existing_path = (self.hub_config.root / existing.path).resolve()
            if existing_path != target:
                raise ValueError(
                    f"Repo id {safe_repo_id} already exists at {existing.path}; choose a different id"
                )

        if target.exists() and not force:
            raise ValueError(f"Repo path already exists: {target}")

        target.mkdir(parents=True, exist_ok=True)

        if git_init and not (target / ".git").exists():
            try:
                proc = run_git(["init"], target, check=False)
            except GitError as exc:
                raise ValueError(f"git init failed: {exc}") from exc
            if proc.returncode != 0:
                raise ValueError(f"git init failed: {_git_failure_detail(proc)}")
        if git_init and not (target / ".git").exists():
            raise ValueError(f"git init failed for {target}")

        seed_repo_files(target, force=force)
        existing_ids = {repo.id for repo in manifest.repos}
        if safe_repo_id in existing_ids and not existing:
            safe_repo_id = ensure_unique_repo_id(safe_repo_id, existing_ids)
        manifest.ensure_repo(
            self.hub_config.root,
            target,
            repo_id=safe_repo_id,
            display_name=display_name,
            kind="base",
        )
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)

        return self._snapshot_for_repo(safe_repo_id)

    def clone_repo(
        self,
        *,
        git_url: str,
        repo_id: Optional[str] = None,
        repo_path: Optional[Path] = None,
        force: bool = False,
    ) -> RepoSnapshot:
        self._invalidate_list_cache()
        git_url = (git_url or "").strip()
        if not git_url:
            raise ValueError("git_url is required")
        inferred_name = (repo_id or "").strip() or _repo_id_from_url(git_url)
        if not inferred_name:
            raise ValueError("Unable to infer repo id from git_url")
        display_name = inferred_name
        safe_repo_id = sanitize_repo_id(inferred_name)
        base_dir = self.hub_config.repos_root
        target = repo_path if repo_path is not None else Path(safe_repo_id)
        if not target.is_absolute():
            target = (base_dir / target).resolve()
        else:
            target = target.resolve()

        try:
            target.relative_to(base_dir)
        except ValueError as exc:
            raise ValueError(
                f"Repo path must live under repos_root ({base_dir})"
            ) from exc

        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        existing = manifest.get(safe_repo_id)
        if existing:
            existing_path = (self.hub_config.root / existing.path).resolve()
            if existing_path != target:
                raise ValueError(
                    f"Repo id {safe_repo_id} already exists at {existing.path}; choose a different id"
                )

        if target.exists() and not force:
            raise ValueError(f"Repo path already exists: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            proc = run_git(
                ["clone", git_url, str(target)],
                target.parent,
                check=False,
                timeout_seconds=300,
            )
        except GitError as exc:
            raise ValueError(f"git clone failed: {exc}") from exc
        if proc.returncode != 0:
            raise ValueError(f"git clone failed: {_git_failure_detail(proc)}")

        seed_repo_files(target, force=False, git_required=False)
        existing_ids = {repo.id for repo in manifest.repos}
        if safe_repo_id in existing_ids and not existing:
            safe_repo_id = ensure_unique_repo_id(safe_repo_id, existing_ids)
        manifest.ensure_repo(
            self.hub_config.root,
            target,
            repo_id=safe_repo_id,
            display_name=display_name,
            kind="base",
        )
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)
        return self._snapshot_for_repo(safe_repo_id)

    def create_worktree(
        self,
        *,
        base_repo_id: str,
        branch: str,
        force: bool = False,
        start_point: Optional[str] = None,
    ) -> RepoSnapshot:
        self._invalidate_list_cache()
        """
        Create a git worktree under hub.worktrees_root and register it as a hub repo entry.
        Worktrees are treated as full repos (own .codex-autorunner docs/state).
        """
        branch = (branch or "").strip()
        if not branch:
            raise ValueError("branch is required")

        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        base = manifest.get(base_repo_id)
        if not base or base.kind != "base":
            raise ValueError(f"Base repo not found: {base_repo_id}")
        base_path = (self.hub_config.root / base.path).resolve()
        if not base_path.exists():
            raise ValueError(f"Base repo missing on disk: {base_repo_id}")

        self.hub_config.worktrees_root.mkdir(parents=True, exist_ok=True)
        worktrees_root = self.hub_config.worktrees_root.resolve()
        safe_branch = re.sub(r"[^a-zA-Z0-9._/-]+", "-", branch).strip("-") or "work"
        repo_id = f"{base_repo_id}--{safe_branch.replace('/', '-')}"
        if manifest.get(repo_id) and not force:
            raise ValueError(f"Worktree repo already exists: {repo_id}")
        worktree_path = (worktrees_root / repo_id).resolve()
        if not is_within(root=worktrees_root, target=worktree_path):
            raise ValueError(
                "Worktree path escapes worktrees_root: "
                f"{worktree_path} (root={worktrees_root})"
            )
        if worktree_path.exists() and not force:
            raise ValueError(f"Worktree path already exists: {worktree_path}")

        # Create the worktree (branch may or may not exist locally).
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        explicit_start_ref = (
            start_point.strip() if start_point and start_point.strip() else None
        )
        effective_start_ref = explicit_start_ref

        if explicit_start_ref is None or explicit_start_ref.startswith("origin/"):
            try:
                fetch_proc = run_git(
                    ["fetch", "--prune", "origin"],
                    base_path,
                    check=False,
                    timeout_seconds=120,
                )
            except GitError as exc:
                raise ValueError(
                    "Unable to refresh origin before creating worktree: %s" % exc
                ) from exc
            if fetch_proc.returncode != 0:
                raise ValueError(
                    "Unable to refresh origin before creating worktree: %s"
                    % _git_failure_detail(fetch_proc)
                )

        if effective_start_ref is None:
            default_branch = git_default_branch(base_path)
            if not default_branch:
                raise ValueError("Unable to resolve origin default branch")
            effective_start_ref = f"origin/{default_branch}"

        assert effective_start_ref is not None
        start_sha = _resolve_ref_sha(base_path, effective_start_ref)
        try:
            exists = run_git(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                base_path,
                check=False,
            )
        except GitError as exc:
            raise ValueError(f"git worktree add failed: {exc}") from exc
        try:
            if exists.returncode == 0:
                branch_sha = _resolve_ref_sha(base_path, f"refs/heads/{branch}")
                if branch_sha != start_sha:
                    raise ValueError(
                        "Branch %r already exists and points to %s, but %s resolves to %s. "
                        "Use a different branch name or realign the existing branch first."
                        % (
                            branch,
                            branch_sha[:12],
                            effective_start_ref,
                            start_sha[:12],
                        )
                    )
                proc = run_git(
                    ["worktree", "add", str(worktree_path), branch],
                    base_path,
                    check=False,
                    timeout_seconds=120,
                )
            else:
                cmd = [
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree_path),
                    effective_start_ref,
                ]
                proc = run_git(
                    cmd,
                    base_path,
                    check=False,
                    timeout_seconds=120,
                )
        except GitError as exc:
            raise ValueError(f"git worktree add failed: {exc}") from exc
        if proc.returncode != 0:
            raise ValueError(f"git worktree add failed: {_git_failure_detail(proc)}")

        seed_repo_files(worktree_path, force=force, git_required=False)
        manifest.ensure_repo(
            self.hub_config.root,
            worktree_path,
            repo_id=repo_id,
            kind="worktree",
            worktree_of=base_repo_id,
            branch=branch,
        )
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)
        self._run_worktree_setup_commands(
            worktree_path, base.worktree_setup_commands, base_repo_id=base_repo_id
        )
        return self._snapshot_for_repo(repo_id)

    def set_worktree_setup_commands(
        self, repo_id: str, commands: List[str]
    ) -> RepoSnapshot:
        self._invalidate_list_cache()
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        entry = manifest.get(repo_id)
        if not entry:
            raise ValueError(f"Repo not found: {repo_id}")
        if entry.kind != "base":
            raise ValueError(
                "Worktree setup commands can only be configured on base repos"
            )
        normalized = [str(cmd).strip() for cmd in commands if str(cmd).strip()]
        entry.worktree_setup_commands = normalized or None
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)
        return self._snapshot_for_repo(repo_id)

    def run_setup_commands_for_workspace(
        self,
        workspace_path: Path,
        *,
        repo_id_hint: Optional[str] = None,
    ) -> int:
        """Run configured setup commands for a hub-tracked workspace.

        Returns the number of setup commands executed. If the workspace is not
        tracked by the hub manifest or no setup commands are configured, returns 0.
        """
        workspace_root = workspace_path.expanduser().resolve()
        snapshots = self.list_repos(use_cache=False)
        snapshots_by_id = {snapshot.id: snapshot for snapshot in snapshots}
        target: Optional[RepoSnapshot] = None

        for snapshot in snapshots:
            try:
                if snapshot.path.expanduser().resolve() == workspace_root:
                    target = snapshot
                    break
            except Exception:
                continue

        if target is None:
            hint = (repo_id_hint or "").strip()
            if hint:
                target = snapshots_by_id.get(hint)

        if target is None:
            return 0

        try:
            execution_root = target.path.expanduser().resolve()
        except Exception:
            return 0

        base_snapshot: Optional[RepoSnapshot] = target
        if target.kind == "worktree":
            base_id = (target.worktree_of or "").strip()
            if not base_id:
                return 0
            base_snapshot = snapshots_by_id.get(base_id)
        if base_snapshot is None or base_snapshot.kind != "base":
            return 0

        commands = [
            str(cmd).strip()
            for cmd in (base_snapshot.worktree_setup_commands or [])
            if str(cmd).strip()
        ]
        if not commands:
            return 0

        self._run_worktree_setup_commands(
            execution_root,
            commands,
            base_repo_id=base_snapshot.id,
        )
        return len(commands)

    def _archive_worktree_snapshot(
        self,
        *,
        worktree_repo_id: str,
        archive_note: Optional[str] = None,
        force: bool = False,
        archive_profile: Optional[str] = None,
        cleanup: bool = False,
    ):
        from .archive import ArchiveResult

        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        entry = manifest.get(worktree_repo_id)
        if not entry or entry.kind != "worktree":
            raise ValueError(f"Worktree repo not found: {worktree_repo_id}")
        if not entry.worktree_of:
            raise ValueError("Worktree repo is missing worktree_of metadata")
        base = manifest.get(entry.worktree_of)
        if not base or base.kind != "base":
            raise ValueError(f"Base repo not found: {entry.worktree_of}")

        base_path = (self.hub_config.root / base.path).resolve()
        worktree_path = (self.hub_config.root / entry.path).resolve()

        if not worktree_path.exists():
            raise ValueError(f"Worktree path does not exist: {worktree_path}")

        self._stop_runner_and_wait_for_exit(
            repo_id=worktree_repo_id,
            repo_path=worktree_path,
        )

        branch_name = entry.branch or git_branch(worktree_path) or "unknown"
        head_sha = git_head_sha(worktree_path) or "unknown"
        snapshot_id = build_snapshot_id(branch_name, head_sha)
        logger.info(
            "Hub archive worktree start id=%s snapshot_id=%s",
            worktree_repo_id,
            snapshot_id,
        )
        profile = cast(
            ArchiveProfile,
            archive_profile or self.hub_config.pma.worktree_archive_profile,
        )
        intent = resolve_worktree_archive_intent(profile=profile, cleanup=cleanup)
        retention_policy = resolve_worktree_archive_retention_policy(
            self.hub_config.pma
        )
        try:
            result: ArchiveResult = archive_worktree_snapshot(
                base_repo_root=base_path,
                base_repo_id=base.id,
                worktree_repo_root=worktree_path,
                worktree_repo_id=worktree_repo_id,
                branch=branch_name,
                worktree_of=entry.worktree_of,
                note=archive_note,
                snapshot_id=snapshot_id,
                head_sha=head_sha,
                source_path=entry.path,
                intent=intent,
                retention_policy=retention_policy,
            )
        except Exception as exc:
            logger.exception(
                "Hub archive worktree failed id=%s snapshot_id=%s",
                worktree_repo_id,
                snapshot_id,
            )
            if not force:
                raise ValueError(f"Worktree archive failed: {exc}") from exc
            return None
        else:
            logger.info(
                "Hub archive worktree complete id=%s snapshot_id=%s status=%s",
                worktree_repo_id,
                result.snapshot_id,
                result.status,
            )
            return result

    def _archive_bound_pma_threads(
        self,
        *,
        worktree_repo_id: str,
        worktree_path: Path,
    ) -> list[str]:
        return list(
            archive_workspace_managed_threads(
                hub_root=self.hub_config.root,
                worktree_repo_id=worktree_repo_id,
                worktree_path=worktree_path,
            )
        )

    def _bound_thread_target_ids(self) -> set[str]:
        try:
            with open_orchestration_sqlite(self.hub_config.root) as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT target_id
                      FROM orch_bindings
                     WHERE disabled_at IS NULL
                       AND target_kind = 'thread'
                       AND TRIM(COALESCE(target_id, '')) != ''
                    """
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return set()
            raise
        return {
            str(row["target_id"]).strip()
            for row in rows
            if isinstance(row["target_id"], str) and row["target_id"].strip()
        }

    def _base_repo_paths(self, manifest: Manifest) -> dict[str, Path]:
        base_repo_paths: dict[str, Path] = {}
        for entry in manifest.repos:
            if entry.kind != "base":
                continue
            base_repo_paths[entry.id] = (self.hub_config.root / entry.path).resolve()
        return base_repo_paths

    def _collect_unbound_repo_threads(
        self,
        *,
        manifest: Optional[Manifest] = None,
    ) -> dict[str, list[str]]:
        if manifest is None:
            manifest = load_manifest(
                self.hub_config.manifest_path,
                self.hub_config.root,
            )
        base_repo_paths = self._base_repo_paths(manifest)
        if not base_repo_paths:
            return {}

        store = PmaThreadStore(self.hub_config.root)
        bound_thread_ids = self._bound_thread_target_ids()
        seen_ids: set[str] = set()
        workspace_to_repo_id = {
            str(path): repo_id for repo_id, path in base_repo_paths.items()
        }
        thread_ids_by_repo: dict[str, list[str]] = {}

        for thread in store.list_threads(status="active", limit=None):
            managed_thread_id = str(thread.get("managed_thread_id") or "").strip()
            if not managed_thread_id or managed_thread_id in seen_ids:
                continue
            if managed_thread_id in bound_thread_ids:
                continue

            thread_repo_id = str(thread.get("repo_id") or "").strip()
            workspace_root = str(thread.get("workspace_root") or "").strip()
            matched_repo_id: Optional[str] = None
            if thread_repo_id in base_repo_paths:
                matched_repo_id = thread_repo_id
            if workspace_root:
                try:
                    resolved_workspace = str(Path(workspace_root).resolve())
                except Exception:
                    resolved_workspace = ""
                if not matched_repo_id and resolved_workspace:
                    matched_repo_id = workspace_to_repo_id.get(resolved_workspace)
            if not matched_repo_id:
                continue

            thread_ids_by_repo.setdefault(matched_repo_id, []).append(managed_thread_id)
            seen_ids.add(managed_thread_id)

        return thread_ids_by_repo

    def _archive_unbound_repo_threads(
        self,
        *,
        repo_id: str,
        unbound_threads_by_repo: Optional[dict[str, list[str]]] = None,
    ) -> list[str]:
        thread_ids = list((unbound_threads_by_repo or {}).get(repo_id, ()))
        if not thread_ids:
            thread_ids = self._collect_unbound_repo_threads().get(repo_id, [])
        if not thread_ids:
            return []

        store = PmaThreadStore(self.hub_config.root)
        archived_thread_ids: list[str] = []
        for managed_thread_id in thread_ids:
            store.archive_thread(managed_thread_id)
            archived_thread_ids.append(managed_thread_id)
        return archived_thread_ids

    def unbound_repo_thread_counts(self) -> dict[str, int]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        thread_ids_by_repo = self._collect_unbound_repo_threads(manifest=manifest)
        return {
            repo_id: len(thread_ids)
            for repo_id, thread_ids in thread_ids_by_repo.items()
            if thread_ids
        }

    def _ensure_worktree_clean_for_archive(
        self, *, worktree_repo_id: str, worktree_path: Path
    ) -> None:
        if not worktree_path.exists():
            return
        if git_available(worktree_path) and not git_is_clean(worktree_path):
            raise ValueError(
                f"Worktree {worktree_repo_id} has uncommitted changes; commit or stash before archiving"
            )

    def _run_docker_command(
        self, args: List[str], *, timeout_seconds: Optional[float] = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *[str(part) for part in args]],
            capture_output=True,
            text=True,
            check=False,
            env=subprocess_env(),
            timeout=timeout_seconds,
        )

    def _cleanup_worktree_docker_container(
        self,
        *,
        worktree_repo_id: str,
        worktree_path: Path,
        destination: DockerDestination,
    ) -> Dict[str, object]:
        explicit_name = bool(destination.container_name)
        container_name = (
            destination.container_name
            or default_car_docker_container_name(worktree_path.resolve())
        )
        if explicit_name:
            message = (
                "Skipping docker container cleanup for explicit container_name "
                "(treated as shared)"
            )
            logger.info(
                "Hub cleanup worktree docker skipped id=%s container=%s reason=%s",
                worktree_repo_id,
                container_name,
                message,
            )
            return {
                "status": "skipped_explicit",
                "container_name": container_name,
                "managed": False,
                "message": message,
            }

        try:
            inspect_proc = self._run_docker_command(
                ["inspect", "--format", "{{.State.Running}}", container_name],
                timeout_seconds=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            message = f"docker inspect failed: {exc}"
            logger.warning(
                "Hub cleanup worktree docker inspect failed id=%s container=%s: %s",
                worktree_repo_id,
                container_name,
                exc,
            )
            return {
                "status": "error",
                "container_name": container_name,
                "managed": True,
                "message": message,
            }
        if inspect_proc.returncode != 0:
            inspect_detail = (inspect_proc.stderr or inspect_proc.stdout or "").strip()
            inspect_detail_lower = inspect_detail.lower()
            if (
                "no such object" in inspect_detail_lower
                or "no such container" in inspect_detail_lower
            ):
                logger.info(
                    "Hub cleanup worktree docker container missing id=%s container=%s",
                    worktree_repo_id,
                    container_name,
                )
                return {
                    "status": "not_found",
                    "container_name": container_name,
                    "managed": True,
                    "message": "container not found",
                }
            message = f"docker inspect failed: {inspect_detail or 'unknown error'}"
            logger.warning(
                "Hub cleanup worktree docker inspect failed id=%s container=%s: %s",
                worktree_repo_id,
                container_name,
                inspect_detail,
            )
            return {
                "status": "error",
                "container_name": container_name,
                "managed": True,
                "message": message,
            }

        running = (inspect_proc.stdout or "").strip().lower() == "true"
        if running:
            try:
                stop_proc = self._run_docker_command(
                    ["stop", "-t", "10", container_name],
                    timeout_seconds=15,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                message = f"docker stop failed: {exc}"
                logger.warning(
                    "Hub cleanup worktree docker stop failed id=%s container=%s: %s",
                    worktree_repo_id,
                    container_name,
                    exc,
                )
                return {
                    "status": "error",
                    "container_name": container_name,
                    "managed": True,
                    "message": message,
                }
            if stop_proc.returncode != 0:
                stop_detail = (stop_proc.stderr or stop_proc.stdout or "").strip()
                message = f"docker stop failed: {stop_detail or 'unknown error'}"
                logger.warning(
                    "Hub cleanup worktree docker stop failed id=%s container=%s: %s",
                    worktree_repo_id,
                    container_name,
                    stop_detail,
                )
                return {
                    "status": "error",
                    "container_name": container_name,
                    "managed": True,
                    "message": message,
                }

        try:
            rm_proc = self._run_docker_command(
                ["rm", container_name],
                timeout_seconds=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            message = f"docker rm failed: {exc}"
            logger.warning(
                "Hub cleanup worktree docker remove failed id=%s container=%s: %s",
                worktree_repo_id,
                container_name,
                exc,
            )
            return {
                "status": "error",
                "container_name": container_name,
                "managed": True,
                "message": message,
            }

        if rm_proc.returncode != 0:
            rm_detail = (rm_proc.stderr or rm_proc.stdout or "").strip()
            rm_detail_lower = rm_detail.lower()
            if (
                "no such object" in rm_detail_lower
                or "no such container" in rm_detail_lower
            ):
                logger.info(
                    "Hub cleanup worktree docker container already removed id=%s container=%s",
                    worktree_repo_id,
                    container_name,
                )
                return {
                    "status": "not_found",
                    "container_name": container_name,
                    "managed": True,
                    "message": "container not found",
                }
            message = f"docker rm failed: {rm_detail or 'unknown error'}"
            logger.warning(
                "Hub cleanup worktree docker remove failed id=%s container=%s: %s",
                worktree_repo_id,
                container_name,
                rm_detail,
            )
            return {
                "status": "error",
                "container_name": container_name,
                "managed": True,
                "message": message,
            }

        logger.info(
            "Hub cleanup worktree docker removed id=%s container=%s",
            worktree_repo_id,
            container_name,
        )
        return {
            "status": "removed",
            "container_name": container_name,
            "managed": True,
            "message": "container stopped and removed",
        }

    def cleanup_worktree(
        self,
        *,
        worktree_repo_id: str,
        delete_branch: bool = False,
        delete_remote: bool = False,
        archive: bool = True,
        force_archive: bool = False,
        archive_note: Optional[str] = None,
        force: bool = False,
        force_attestation: Optional[Mapping[str, object]] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        if self.hub_config.pma.cleanup_require_archive and not archive:
            raise ValueError(
                "Worktree cleanup requires archiving per PMA policy "
                "(pma.cleanup_require_archive is enabled). "
                "Use archive=True or omit the --no-archive flag."
            )
        enforce_force_attestation(
            force=force or force_archive,
            force_attestation=force_attestation,
            logger=logger,
            action="hub.cleanup_worktree",
        )
        self._invalidate_list_cache()
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        entry = manifest.get(worktree_repo_id)
        if not entry or entry.kind != "worktree":
            raise ValueError(f"Worktree repo not found: {worktree_repo_id}")
        if not entry.worktree_of:
            raise ValueError("Worktree repo is missing worktree_of metadata")
        base = manifest.get(entry.worktree_of)
        if not base or base.kind != "base":
            raise ValueError(f"Base repo not found: {entry.worktree_of}")

        base_path = (self.hub_config.root / base.path).resolve()
        worktree_path = (self.hub_config.root / entry.path).resolve()
        branch_name = entry.branch or "unknown"
        try:
            has_active_chat_binding = self._has_active_chat_binding(worktree_repo_id)
        except Exception as exc:
            if not force:
                raise ValueError(
                    "Unable to verify active chat bindings for "
                    f"{worktree_repo_id} (branch={branch_name}); refusing cleanup. "
                    "Re-run with --force to proceed."
                ) from exc
            logger.warning(
                "Proceeding with forced worktree cleanup despite chat-binding "
                "lookup failure for repo %s",
                worktree_repo_id,
                exc_info=exc,
            )
            has_active_chat_binding = False
        if has_active_chat_binding and not force:
            raise ValueError(
                f"Refusing to clean up chat-bound worktree {worktree_repo_id} "
                f"(branch={branch_name}). This worktree is bound to a chat. "
                "Re-run with --force to proceed."
            )

        self._stop_runner_and_wait_for_exit(
            repo_id=worktree_repo_id,
            repo_path=worktree_path,
        )

        if archive:
            self._ensure_worktree_clean_for_archive(
                worktree_repo_id=worktree_repo_id,
                worktree_path=worktree_path,
            )
            self._archive_worktree_snapshot(
                worktree_repo_id=worktree_repo_id,
                archive_note=archive_note,
                force=force_archive,
                archive_profile=archive_profile,
                cleanup=True,
            )

        repos_by_id = {repo.id: repo for repo in manifest.repos}
        effective_destination = resolve_effective_repo_destination(entry, repos_by_id)
        docker_cleanup: Dict[str, object] = {
            "status": "not_applicable",
            "message": "effective destination is not docker",
        }
        if isinstance(effective_destination.destination, DockerDestination):
            docker_cleanup = self._cleanup_worktree_docker_container(
                worktree_repo_id=worktree_repo_id,
                worktree_path=worktree_path,
                destination=effective_destination.destination,
            )

        # Remove worktree from base repo.
        try:
            proc = run_git(
                ["worktree", "remove", "--force", str(worktree_path)],
                base_path,
                check=False,
                timeout_seconds=120,
            )
        except GitError as exc:
            raise ValueError(f"git worktree remove failed: {exc}") from exc
        if proc.returncode != 0:
            detail = _git_failure_detail(proc)
            detail_lower = detail.lower()
            # If the worktree is already gone (deleted via UI/Hub), continue cleanup.
            if "not a working tree" not in detail_lower:
                raise ValueError(f"git worktree remove failed: {detail}")
        try:
            proc = run_git(["worktree", "prune"], base_path, check=False)
            if proc.returncode != 0:
                logger.warning(
                    "git worktree prune failed: %s", _git_failure_detail(proc)
                )
        except GitError as exc:
            logger.warning("git worktree prune failed: %s", exc)

        if delete_branch and entry.branch:
            try:
                proc = run_git(["branch", "-D", entry.branch], base_path, check=False)
                if proc.returncode != 0:
                    logger.warning(
                        "git branch delete failed: %s", _git_failure_detail(proc)
                    )
            except GitError as exc:
                logger.warning("git branch delete failed: %s", exc)
        if delete_remote and entry.branch:
            try:
                proc = run_git(
                    ["push", "origin", "--delete", entry.branch],
                    base_path,
                    check=False,
                    timeout_seconds=120,
                )
                if proc.returncode != 0:
                    logger.warning(
                        "git push delete failed: %s", _git_failure_detail(proc)
                    )
            except GitError as exc:
                logger.warning("git push delete failed: %s", exc)

        manifest.repos = [r for r in manifest.repos if r.id != worktree_repo_id]
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)
        self._archive_bound_pma_threads(
            worktree_repo_id=worktree_repo_id,
            worktree_path=worktree_path,
        )
        return {"status": "ok", "docker_cleanup": docker_cleanup}

    def _has_active_chat_binding(self, repo_id: str) -> bool:
        return repo_has_active_non_pma_chat_binding(
            hub_root=self.hub_config.root,
            raw_config=self.hub_config.raw,
            repo_id=repo_id,
        )

    def archive_worktree(
        self,
        *,
        worktree_repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        entry = manifest.get(worktree_repo_id)
        if not entry or entry.kind != "worktree":
            raise ValueError(f"Worktree repo not found: {worktree_repo_id}")
        if not entry.worktree_of:
            raise ValueError("Worktree repo is missing worktree_of metadata")
        worktree_path = (self.hub_config.root / entry.path).resolve()

        if not worktree_path.exists():
            raise ValueError(f"Worktree path does not exist: {worktree_path}")

        self._ensure_worktree_clean_for_archive(
            worktree_repo_id=worktree_repo_id,
            worktree_path=worktree_path,
        )

        result = self._archive_worktree_snapshot(
            worktree_repo_id=worktree_repo_id,
            archive_note=archive_note,
            force=False,
            archive_profile=archive_profile,
        )
        self._archive_bound_pma_threads(
            worktree_repo_id=worktree_repo_id,
            worktree_path=worktree_path,
        )
        if result is None:
            raise ValueError("Archive failed unexpectedly")
        return {
            "snapshot_id": result.snapshot_id,
            "snapshot_path": str(result.snapshot_path),
            "meta_path": str(result.meta_path),
            "status": result.status,
            "file_count": result.file_count,
            "total_bytes": result.total_bytes,
            "flow_run_count": result.flow_run_count,
            "latest_flow_run_id": result.latest_flow_run_id,
        }

    def archive_repo_state(
        self,
        *,
        repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        entry = manifest.get(repo_id)
        if not entry:
            raise ValueError(f"Repo not found: {repo_id}")

        repo_path = (self.hub_config.root / entry.path).resolve()
        if not repo_path.exists():
            raise ValueError(f"Repo path does not exist: {repo_path}")

        base_path = repo_path
        base_repo_id = entry.id
        worktree_of = entry.worktree_of or entry.id
        if entry.kind == "worktree":
            if not entry.worktree_of:
                raise ValueError("Worktree repo is missing worktree_of metadata")
            base = manifest.get(entry.worktree_of)
            if not base or base.kind != "base":
                raise ValueError(f"Base repo not found: {entry.worktree_of}")
            base_path = (self.hub_config.root / base.path).resolve()
            base_repo_id = base.id

        has_car_state = bool(dirty_car_state_paths(repo_path))
        if has_car_state:
            self._stop_runner_and_wait_for_exit(
                repo_id=repo_id,
                repo_path=repo_path,
            )

        branch_name = entry.branch or git_branch(repo_path) or "unknown"
        result = archive_workspace_for_fresh_start(
            hub_root=self.hub_config.root,
            base_repo_root=base_path,
            base_repo_id=base_repo_id,
            worktree_repo_root=repo_path,
            worktree_repo_id=repo_id,
            branch=branch_name,
            worktree_of=worktree_of,
            note=archive_note,
            source_path=entry.path,
            retention_policy=resolve_worktree_archive_retention_policy(
                self.hub_config.pma
            ),
        )
        return {
            "snapshot_id": result.snapshot_id,
            "snapshot_path": (
                str(result.snapshot_path) if result.snapshot_path else None
            ),
            "meta_path": str(result.meta_path) if result.meta_path else None,
            "status": result.status,
            "file_count": result.file_count,
            "total_bytes": result.total_bytes,
            "flow_run_count": result.flow_run_count,
            "latest_flow_run_id": result.latest_flow_run_id,
            "archived_paths": list(result.archived_paths),
            "reset_paths": list(result.reset_paths),
            "archived_thread_ids": list(result.archived_thread_ids),
            "archived_thread_count": len(result.archived_thread_ids),
        }

    def archive_worktree_state(
        self,
        *,
        worktree_repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        entry = manifest.get(worktree_repo_id)
        if not entry or entry.kind != "worktree":
            raise ValueError(f"Worktree repo not found: {worktree_repo_id}")
        return self.archive_repo_state(
            repo_id=worktree_repo_id,
            archive_note=archive_note,
            archive_profile=archive_profile,
        )

    def cleanup_repo_threads(self, *, repo_id: str) -> Dict[str, object]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        entry = manifest.get(repo_id)
        if not entry or entry.kind != "base":
            raise ValueError(f"Base repo not found: {repo_id}")

        repo_path = (self.hub_config.root / entry.path).resolve()
        if not repo_path.exists():
            raise ValueError(f"Repo path does not exist: {repo_path}")

        unbound_threads_by_repo = self._collect_unbound_repo_threads(manifest=manifest)
        archived_thread_ids = self._archive_unbound_repo_threads(
            repo_id=repo_id,
            unbound_threads_by_repo=unbound_threads_by_repo,
        )
        archived_count = len(archived_thread_ids)
        if archived_count == 0:
            message = f"No stale non-chat-bound threads found for {repo_id}."
        elif archived_count == 1:
            message = f"Archived 1 stale non-chat-bound thread for {repo_id}."
        else:
            message = (
                f"Archived {archived_count} stale non-chat-bound threads for {repo_id}."
            )
        return {
            "status": "ok",
            "repo_id": repo_id,
            "archived_thread_ids": archived_thread_ids,
            "archived_count": archived_count,
            "message": message,
        }

    def cleanup_all_repo_threads(self) -> Dict[str, object]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        base_repo_paths = self._base_repo_paths(manifest)
        unbound_threads_by_repo = self._collect_unbound_repo_threads(manifest=manifest)
        dirty_repo_ids: list[str] = []
        results: list[dict[str, object]] = []
        total_archived = 0

        for repo_id, repo_path in base_repo_paths.items():
            is_dirty = False
            if repo_path.exists() and git_available(repo_path):
                try:
                    is_dirty = not git_is_clean(repo_path)
                except Exception:
                    is_dirty = False
            if is_dirty:
                dirty_repo_ids.append(repo_id)

            archived_thread_ids = self._archive_unbound_repo_threads(
                repo_id=repo_id,
                unbound_threads_by_repo=unbound_threads_by_repo,
            )
            archived_count = len(archived_thread_ids)
            total_archived += archived_count
            if archived_count > 0 or is_dirty:
                results.append(
                    {
                        "repo_id": repo_id,
                        "archived_thread_ids": archived_thread_ids,
                        "archived_count": archived_count,
                        "is_dirty": is_dirty,
                    }
                )

        cleaned_repo_count = 0
        for item in results:
            archived_count_value = item.get("archived_count")
            if isinstance(archived_count_value, int) and archived_count_value > 0:
                cleaned_repo_count += 1
        if total_archived == 0:
            message = "No stale non-chat-bound threads found across base repos."
        elif total_archived == 1:
            message = "Archived 1 stale non-chat-bound thread across base repos."
        else:
            message = f"Archived {total_archived} stale non-chat-bound threads across base repos."
        return {
            "status": "ok",
            "archived_count": total_archived,
            "cleaned_repo_count": cleaned_repo_count,
            "dirty_repo_ids": dirty_repo_ids,
            "dirty_repo_count": len(dirty_repo_ids),
            "results": results,
            "message": message,
        }

    def cleanup_all(self, *, dry_run: bool = False) -> Dict[str, object]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        unbound_threads_by_repo = self._collect_unbound_repo_threads(manifest=manifest)

        threads_by_repo: list[dict[str, object]] = []
        total_thread_count = 0
        for repo_id, thread_ids in sorted(unbound_threads_by_repo.items()):
            count = len(thread_ids)
            if count == 0:
                continue
            total_thread_count += count
            threads_by_repo.append({"repo_id": repo_id, "count": count})

        if not dry_run:
            for repo_id in self._base_repo_paths(manifest).keys():
                self._archive_unbound_repo_threads(
                    repo_id=repo_id,
                    unbound_threads_by_repo=unbound_threads_by_repo,
                )

        worktree_items: list[dict[str, str]] = []
        worktree_errors: list[dict[str, str]] = []
        for entry in manifest.repos:
            if entry.kind != "worktree":
                continue
            if not entry.worktree_of:
                continue
            try:
                if self._has_active_chat_binding(entry.id):
                    continue
            except Exception as exc:
                logger.warning(
                    "cleanup_all: chat binding check failed for %s",
                    entry.id,
                    exc_info=exc,
                )
                continue
            worktree_path = (self.hub_config.root / entry.path).resolve()
            if not worktree_path.exists():
                continue
            if git_available(worktree_path):
                try:
                    if not git_is_clean(worktree_path):
                        continue
                except Exception:
                    continue
            branch_name = entry.branch or git_branch(worktree_path) or "unknown"
            if dry_run:
                worktree_items.append({"id": entry.id, "branch": branch_name})
                continue
            try:
                self.cleanup_worktree(
                    worktree_repo_id=entry.id,
                    archive=True,
                )
                worktree_items.append({"id": entry.id, "branch": branch_name})
            except Exception as exc:
                logger.warning(
                    "cleanup_all: worktree cleanup failed for %s",
                    entry.id,
                    exc_info=exc,
                )
                worktree_errors.append({"id": entry.id, "error": str(exc)})

        flow_by_repo: list[dict[str, object]] = []
        total_flow_count = 0
        for entry in manifest.repos:
            repo_root = (self.hub_config.root / entry.path).resolve()
            db_path = repo_root / ".codex-autorunner" / "flows.db"
            if not db_path.exists():
                continue
            try:
                repo_config = load_repo_config(repo_root)
                durable = bool(getattr(repo_config, "durable_writes", False))
            except ConfigError:
                durable = False
            store = FlowStore(db_path, durable=durable)
            records: list[Any] = []
            try:
                store.initialize()
                records = list(store.list_flow_runs(flow_type="ticket_flow"))
            except Exception as exc:
                logger.warning(
                    "cleanup_all: flow store failed for %s",
                    entry.id,
                    exc_info=exc,
                )
                continue
            finally:
                store.close()
            completed = [r for r in records if r.status == FlowRunStatus.COMPLETED]
            if not completed:
                continue
            if dry_run:
                n = len(completed)
                flow_by_repo.append({"repo_id": entry.id, "count": n})
                total_flow_count += n
                continue
            archived_here = 0
            for record in completed:
                try:
                    archive_flow_run_artifacts(
                        repo_root,
                        run_id=record.id,
                        force=False,
                        delete_run=True,
                    )
                    archived_here += 1
                except Exception as exc:
                    logger.warning(
                        "cleanup_all: archive flow run failed repo=%s run=%s",
                        entry.id,
                        record.id,
                        exc_info=exc,
                    )
            if archived_here:
                flow_by_repo.append({"repo_id": entry.id, "count": archived_here})
            total_flow_count += archived_here

        if not dry_run and (
            total_thread_count or len(worktree_items) or total_flow_count
        ):
            self._invalidate_list_cache()

        if dry_run:
            if total_thread_count or len(worktree_items) or total_flow_count:
                message = (
                    f"Would archive {total_thread_count} threads, "
                    f"{len(worktree_items)} worktrees, {total_flow_count} flow runs"
                )
            else:
                message = "Nothing to clean up"
        else:
            if total_thread_count or len(worktree_items) or total_flow_count:
                message = (
                    f"Archived {total_thread_count} threads, "
                    f"{len(worktree_items)} worktrees, {total_flow_count} flow runs"
                )
            else:
                message = "Nothing to clean up"

        return {
            "status": "ok",
            "dry_run": dry_run,
            "threads": {
                "archived_count": total_thread_count,
                "by_repo": threads_by_repo,
            },
            "worktrees": {
                "archived_count": len(worktree_items),
                "items": worktree_items,
                "errors": worktree_errors,
            },
            "flow_runs": {
                "archived_count": total_flow_count,
                "by_repo": flow_by_repo,
            },
            "message": message,
        }

    def check_repo_removal(self, repo_id: str) -> Dict[str, object]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repo_root = (self.hub_config.root / repo.path).resolve()
        exists_on_disk = repo_root.exists()
        clean: Optional[bool] = None
        upstream = None
        if exists_on_disk and git_available(repo_root):
            clean = git_is_clean(repo_root)
            upstream = git_upstream_status(repo_root)
        worktrees = []
        if repo.kind == "base":
            worktrees = [
                r.id
                for r in manifest.repos
                if r.kind == "worktree" and r.worktree_of == repo_id
            ]
        return {
            "id": repo.id,
            "path": str(repo_root),
            "kind": repo.kind,
            "exists_on_disk": exists_on_disk,
            "is_clean": clean,
            "upstream": upstream,
            "worktrees": worktrees,
        }

    def remove_repo(
        self,
        repo_id: str,
        *,
        force: bool = False,
        delete_dir: bool = True,
        delete_worktrees: bool = False,
        force_attestation: Optional[Mapping[str, object]] = None,
    ) -> None:
        self._invalidate_list_cache()
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        enforce_force_attestation(
            force=force,
            force_attestation=force_attestation,
            logger=logger,
            action="hub.remove_repo",
        )

        if repo.kind == "worktree":
            self.cleanup_worktree(
                worktree_repo_id=repo_id,
                force=force,
                force_attestation=force_attestation,
            )
            return

        worktrees = [
            r
            for r in manifest.repos
            if r.kind == "worktree" and r.worktree_of == repo_id
        ]
        if worktrees and not delete_worktrees:
            ids = ", ".join(r.id for r in worktrees)
            raise ValueError(f"Repo {repo_id} has worktrees: {ids}")
        if worktrees and delete_worktrees:
            for worktree in worktrees:
                self.cleanup_worktree(
                    worktree_repo_id=worktree.id,
                    force=force,
                    force_attestation=force_attestation,
                )
            manifest = load_manifest(
                self.hub_config.manifest_path, self.hub_config.root
            )
            repo = manifest.get(repo_id)
            if not repo:
                raise ValueError(f"Repo {repo_id} missing after worktree cleanup")

        repo_root = (self.hub_config.root / repo.path).resolve()
        if repo_root.exists() and git_available(repo_root):
            if not git_is_clean(repo_root) and not force:
                raise ValueError("Repo has uncommitted changes; use force to remove")
            upstream = git_upstream_status(repo_root)
            if (
                upstream
                and upstream.get("has_upstream")
                and upstream.get("ahead", 0) > 0
                and not force
            ):
                raise ValueError("Repo has unpushed commits; use force to remove")

        self._stop_runner_and_wait_for_exit(
            repo_id=repo_id,
            repo_path=repo_root,
        )
        self._runners.pop(repo_id, None)

        if delete_dir and repo_root.exists():
            shutil.rmtree(repo_root)

        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        manifest.repos = [r for r in manifest.repos if r.id != repo_id]
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)
        self.list_repos(use_cache=False)

    def _ensure_runner(
        self, repo_id: str, allow_uninitialized: bool = False
    ) -> Optional[RepoRunner]:
        if repo_id in self._runners:
            return self._runners[repo_id]
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repo_root = (self.hub_config.root / repo.path).resolve()
        tickets_dir = repo_root / ".codex-autorunner" / "tickets"
        if not allow_uninitialized and not tickets_dir.exists():
            raise ValueError(f"Repo {repo_id} is not initialized")
        if not tickets_dir.exists():
            return None
        repo_config = derive_repo_config(self.hub_config, repo_root, load_env=False)
        runner = RepoRunner(
            repo_id,
            repo_root,
            repo_config=repo_config,
            spawn_fn=self._spawn_fn,
            backend_factory_builder=self._backend_factory_builder,
            app_server_supervisor_factory_builder=(
                self._app_server_supervisor_factory_builder
            ),
            backend_orchestrator_builder=self._backend_orchestrator_builder,
            agent_id_validator=self._agent_id_validator,
        )
        self._runners[repo_id] = runner
        return runner

    def _manifest_records(
        self, manifest_only: bool = False
    ) -> Tuple[Manifest, List[DiscoveryRecord]]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        records: List[DiscoveryRecord] = []
        for entry in manifest.repos:
            repo_path = (self.hub_config.root / entry.path).resolve()
            initialized = (repo_path / ".codex-autorunner" / "tickets").exists()
            records.append(
                DiscoveryRecord(
                    repo=entry,
                    absolute_path=repo_path,
                    added_to_manifest=False,
                    exists_on_disk=repo_path.exists(),
                    initialized=initialized,
                    init_error=None,
                )
            )
        if manifest_only:
            return manifest, records
        return manifest, records

    def _build_snapshots(self, records: List[DiscoveryRecord]) -> List[RepoSnapshot]:
        repos_by_id = {record.repo.id: record.repo for record in records}
        snapshots: List[RepoSnapshot] = []
        for record in records:
            snapshots.append(self._snapshot_from_record(record, repos_by_id))
        return snapshots

    def _build_agent_workspace_snapshots(
        self, workspaces: List[ManifestAgentWorkspace]
    ) -> List[AgentWorkspaceSnapshot]:
        return [self._snapshot_from_agent_workspace(entry) for entry in workspaces]

    def _prune_pinned_parent_repo_ids(self, snapshots: List[RepoSnapshot]) -> List[str]:
        base_repo_ids = {snap.id for snap in snapshots if snap.kind == "base"}
        pinned = _normalize_pinned_parent_repo_ids(self.state.pinned_parent_repo_ids)
        return [repo_id for repo_id in pinned if repo_id in base_repo_ids]

    def _snapshot_for_repo(self, repo_id: str) -> RepoSnapshot:
        _, records = self._manifest_records(manifest_only=True)
        record = next((r for r in records if r.repo.id == repo_id), None)
        if not record:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repos_by_id = {entry.repo.id: entry.repo for entry in records}
        snapshot = self._snapshot_from_record(record, repos_by_id)
        self.list_repos(use_cache=False)
        return snapshot

    def _snapshot_for_agent_workspace(
        self, workspace_id: str
    ) -> AgentWorkspaceSnapshot:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        workspace = manifest.get_agent_workspace(workspace_id)
        if not workspace:
            raise ValueError(f"Agent workspace {workspace_id} not found in manifest")
        snapshot = self._snapshot_from_agent_workspace(workspace)
        self.list_repos(use_cache=False)
        return snapshot

    def _invalidate_list_cache(self) -> None:
        with self._list_lock:
            self._list_cache = None
            self._list_cache_at = None

    @property
    def lifecycle_emitter(self) -> LifecycleEventEmitter:
        return self._lifecycle_emitter

    @property
    def lifecycle_store(self) -> LifecycleEventStore:
        return self._lifecycle_emitter._store

    def ensure_pma_automation_store(self) -> PmaAutomationStore:
        if self._pma_automation_store is not None:
            return self._pma_automation_store
        self._pma_automation_store = PmaAutomationStore(self.hub_config.root)
        return self._pma_automation_store

    def ensure_automation_store(self) -> PmaAutomationStore:
        return self.ensure_pma_automation_store()

    def get_pma_automation_store(self) -> PmaAutomationStore:
        return self.ensure_pma_automation_store()

    def get_automation_store(self) -> PmaAutomationStore:
        return self.ensure_automation_store()

    @property
    def pma_automation_store(self) -> PmaAutomationStore:
        return self.ensure_pma_automation_store()

    @property
    def automation_store(self) -> PmaAutomationStore:
        return self.ensure_automation_store()

    def set_pma_lane_worker_starter(
        self, starter: Optional[Callable[[str], None]]
    ) -> None:
        self._pma_lane_worker_starter = starter

    def _request_pma_lane_worker_start(self, lane_id: Optional[str]) -> None:
        starter = self._pma_lane_worker_starter
        if starter is None:
            return
        normalized_lane_id = (
            lane_id.strip()
            if isinstance(lane_id, str) and lane_id.strip()
            else DEFAULT_PMA_LANE_ID
        )
        try:
            starter(normalized_lane_id)
        except Exception:
            logger.exception(
                "Failed requesting PMA lane worker startup for lane_id=%s",
                normalized_lane_id,
            )

    def process_pma_automation_now(
        self, *, include_timers: bool = True, limit: int = 100
    ) -> dict[str, int]:
        timer_wakeups = (
            self.process_pma_automation_timers(limit=limit) if include_timers else 0
        )
        dispatched_wakeups = self.drain_pma_automation_wakeups(limit=limit)
        return {
            "timers_processed": timer_wakeups,
            "wakeups_dispatched": dispatched_wakeups,
        }

    def trigger_pma_from_lifecycle_event(self, event: LifecycleEvent) -> None:
        self._process_lifecycle_event(event)

    def _process_lifecycle_event_cycle(self) -> None:
        self.process_lifecycle_events()
        self.process_scm_automation_polls()
        self.process_pma_automation_timers()
        self.drain_pma_automation_wakeups()

    def process_lifecycle_events(self) -> None:
        self._lifecycle_event_processor.process_events(limit=100)
        try:
            self.drain_pma_automation_wakeups()
        except Exception:
            logger.exception("Failed draining PMA automation wake-ups")

    def process_scm_automation_polls(self, *, limit: int = 20) -> dict[str, int]:
        processor = self._scm_poll_processor
        if processor is None:
            return {
                "due": 0,
                "polled": 0,
                "events_emitted": 0,
                "expired": 0,
                "closed": 0,
                "errors": 0,
                "candidate_workspaces": 0,
                "bindings_discovered": 0,
                "watches_armed": 0,
                "discovery_errors": 0,
            }
        try:
            return processor(limit)
        except Exception:
            logger.exception("Failed processing SCM automation polling watches")
            return {
                "due": 0,
                "polled": 0,
                "events_emitted": 0,
                "expired": 0,
                "closed": 0,
                "errors": 1,
                "candidate_workspaces": 0,
                "bindings_discovered": 0,
                "watches_armed": 0,
                "discovery_errors": 1,
            }

    def _start_lifecycle_event_processor(self) -> None:
        self._lifecycle_worker.start()

    def _stop_lifecycle_event_processor(self) -> None:
        self._lifecycle_worker.stop()

    def shutdown(self) -> None:
        self._stop_lifecycle_event_processor()
        set_lifecycle_emitter(None)

    def _wire_outbox_lifecycle(self) -> None:
        if not self.hub_config.pma.enabled:
            set_lifecycle_emitter(None)
            return

        def _emit_outbox_event(
            event_type: str,
            repo_id: str,
            run_id: str,
            data: Dict[str, Any],
            origin: str,
        ) -> None:
            if event_type == "dispatch_created":
                self._lifecycle_emitter.emit_dispatch_created(
                    repo_id, run_id, data=data, origin=origin
                )

        set_lifecycle_emitter(_emit_outbox_event)

    def _on_dispatch_intercept(self, event_id: str, result: Any) -> None:
        logger.info(
            "Dispatch intercepted: event_id=%s action=%s reason=%s",
            event_id,
            (
                result.get("action")
                if isinstance(result, dict)
                else getattr(result, "action", None)
            ),
            (
                result.get("reason")
                if isinstance(result, dict)
                else getattr(result, "reason", None)
            ),
        )

    def _ensure_dispatch_interceptor(self) -> Optional[PmaDispatchInterceptor]:
        if not self.hub_config.pma.enabled:
            return None
        if not self.hub_config.pma.dispatch_interception_enabled:
            return None
        if self._dispatch_interceptor is None:
            self._dispatch_interceptor = PmaDispatchInterceptor(
                hub_root=self.hub_config.root,
                supervisor=self,
                on_intercept=self._on_dispatch_intercept,
            )
        return self._dispatch_interceptor

    def _run_coroutine(self, coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        else:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

    def _lifecycle_transition_payload(
        self, event: LifecycleEvent
    ) -> dict[str, Optional[str]]:
        data = event.data if isinstance(event.data, dict) else {}
        to_state_fallback = {
            LifecycleEventType.FLOW_PAUSED: "blocked",
            LifecycleEventType.FLOW_COMPLETED: "completed",
            LifecycleEventType.FLOW_FAILED: "failed",
            LifecycleEventType.FLOW_STOPPED: "stopped",
            LifecycleEventType.DISPATCH_CREATED: "dispatch_created",
        }
        from_state = (
            str(data.get("from_state")).strip()
            if isinstance(data.get("from_state"), str)
            else None
        )
        to_state = (
            str(data.get("to_state")).strip()
            if isinstance(data.get("to_state"), str)
            else to_state_fallback.get(event.event_type)
        )
        if from_state is None:
            if event.event_type == LifecycleEventType.DISPATCH_CREATED:
                from_state = "paused"
            elif to_state in {"paused", "blocked", "completed", "failed", "stopped"}:
                from_state = "running"

        reason = (
            str(data.get("reason")).strip()
            if isinstance(data.get("reason"), str) and str(data.get("reason")).strip()
            else event.event_type.value
        )
        timestamp = (
            str(event.timestamp).strip()
            if isinstance(event.timestamp, str) and str(event.timestamp).strip()
            else now_iso()
        )
        thread_id = (
            str(data.get("thread_id")).strip()
            if isinstance(data.get("thread_id"), str)
            and str(data.get("thread_id")).strip()
            else None
        )
        repo_id = (
            event.repo_id.strip()
            if isinstance(event.repo_id, str) and event.repo_id.strip()
            else (
                str(data.get("repo_id")).strip()
                if isinstance(data.get("repo_id"), str)
                and str(data.get("repo_id")).strip()
                else None
            )
        )
        run_id = (
            event.run_id.strip()
            if isinstance(event.run_id, str) and event.run_id.strip()
            else (
                str(data.get("run_id")).strip()
                if isinstance(data.get("run_id"), str)
                and str(data.get("run_id")).strip()
                else None
            )
        )
        return {
            "repo_id": repo_id,
            "run_id": run_id,
            "thread_id": thread_id,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
            "timestamp": timestamp,
        }

    def _enqueue_automation_wakeups_for_lifecycle_event(
        self, event: LifecycleEvent
    ) -> int:
        transition = self._lifecycle_transition_payload(event)
        try:
            store = self.ensure_pma_automation_store()
            matches = store.match_lifecycle_subscriptions(
                event_type=event.event_type.value,
                repo_id=transition.get("repo_id"),
                run_id=transition.get("run_id"),
                thread_id=transition.get("thread_id"),
                from_state=transition.get("from_state"),
                to_state=transition.get("to_state"),
            )
        except Exception:
            logger.exception(
                "Failed to match lifecycle subscriptions for event %s", event.event_id
            )
            return 0

        if not matches:
            return 0

        created = 0
        for subscription in matches:
            subscription_id = str(subscription.get("subscription_id") or "").strip()
            idempotency_key = f"lifecycle:{event.event_id}:subscription:{subscription_id or 'unknown'}"
            reason = (
                str(subscription.get("reason")).strip()
                if isinstance(subscription.get("reason"), str)
                and str(subscription.get("reason")).strip()
                else transition.get("reason")
            )
            _, deduped = store.enqueue_wakeup(
                source="lifecycle_subscription",
                repo_id=transition.get("repo_id"),
                run_id=transition.get("run_id"),
                thread_id=transition.get("thread_id"),
                lane_id=(
                    str(subscription.get("lane_id")).strip()
                    if isinstance(subscription.get("lane_id"), str)
                    and str(subscription.get("lane_id")).strip()
                    else "pma:default"
                ),
                from_state=transition.get("from_state"),
                to_state=transition.get("to_state"),
                reason=reason,
                timestamp=transition.get("timestamp"),
                idempotency_key=idempotency_key,
                subscription_id=subscription_id or None,
                event_id=event.event_id,
                event_type=event.event_type.value,
                event_data=event.data if isinstance(event.data, dict) else {},
                metadata={"origin": event.origin},
            )
            if not deduped:
                created += 1
        return created

    def process_pma_automation_timers(self, *, limit: int = 100) -> int:
        take = max(0, int(limit))
        if take <= 0:
            return 0

        store = self.ensure_pma_automation_store()
        dequeue_due_timers = getattr(store, "dequeue_due_timers", None)
        if not callable(dequeue_due_timers):
            return 0
        due_timers = dequeue_due_timers(limit=take)
        if not due_timers:
            return 0

        created = 0
        for timer in due_timers:
            timer_id = str(timer.get("timer_id") or "").strip()
            timestamp = (
                str(timer.get("fired_at")).strip()
                if isinstance(timer.get("fired_at"), str)
                and str(timer.get("fired_at")).strip()
                else now_iso()
            )
            _, deduped = store.enqueue_wakeup(
                source="timer",
                repo_id=(
                    str(timer.get("repo_id")).strip()
                    if isinstance(timer.get("repo_id"), str)
                    and str(timer.get("repo_id")).strip()
                    else None
                ),
                run_id=(
                    str(timer.get("run_id")).strip()
                    if isinstance(timer.get("run_id"), str)
                    and str(timer.get("run_id")).strip()
                    else None
                ),
                thread_id=(
                    str(timer.get("thread_id")).strip()
                    if isinstance(timer.get("thread_id"), str)
                    and str(timer.get("thread_id")).strip()
                    else None
                ),
                lane_id=(
                    str(timer.get("lane_id")).strip()
                    if isinstance(timer.get("lane_id"), str)
                    and str(timer.get("lane_id")).strip()
                    else "pma:default"
                ),
                from_state=(
                    str(timer.get("from_state")).strip()
                    if isinstance(timer.get("from_state"), str)
                    and str(timer.get("from_state")).strip()
                    else None
                ),
                to_state=(
                    str(timer.get("to_state")).strip()
                    if isinstance(timer.get("to_state"), str)
                    and str(timer.get("to_state")).strip()
                    else None
                ),
                reason=(
                    str(timer.get("reason")).strip()
                    if isinstance(timer.get("reason"), str)
                    and str(timer.get("reason")).strip()
                    else "timer_due"
                ),
                timestamp=timestamp,
                idempotency_key=f"timer:{timer_id}:{timestamp}",
                timer_id=timer_id or None,
                metadata=(
                    dict(timer.get("metadata"))
                    if isinstance(timer.get("metadata"), dict)
                    else {}
                ),
            )
            if not deduped:
                created += 1
        return created

    def _build_pma_wakeup_message(self, wake_up: dict[str, Any]) -> str:
        lines = ["Automation wake-up received."]
        source = wake_up.get("source")
        event_type = wake_up.get("event_type")
        subscription_id = wake_up.get("subscription_id")
        timer_id = wake_up.get("timer_id")
        repo_id = wake_up.get("repo_id")
        run_id = wake_up.get("run_id")
        thread_id = wake_up.get("thread_id")
        lane_id = wake_up.get("lane_id")
        if source:
            lines.append(f"source: {source}")
        if event_type:
            lines.append(f"event_type: {event_type}")
        if subscription_id:
            lines.append(f"subscription_id: {subscription_id}")
        if timer_id:
            lines.append(f"timer_id: {timer_id}")
        if repo_id:
            lines.append(f"repo_id: {repo_id}")
        if run_id:
            lines.append(f"run_id: {run_id}")
        if thread_id:
            lines.append(f"thread_id: {thread_id}")
        if lane_id:
            lines.append(f"lane_id: {lane_id}")
        if wake_up.get("from_state"):
            lines.append(f"from_state: {wake_up['from_state']}")
        if wake_up.get("to_state"):
            lines.append(f"to_state: {wake_up['to_state']}")
        if wake_up.get("reason"):
            lines.append(f"reason: {wake_up['reason']}")
        if wake_up.get("timestamp"):
            lines.append(f"timestamp: {wake_up['timestamp']}")
        if source == "timer":
            lines.append(
                "suggested_next_action: verify progress, then use /hub/pma/timers/{timer_id}/touch or /hub/pma/timers/{timer_id}/cancel."
            )
        else:
            lines.append(
                "suggested_next_action: inspect the transition and adjust /hub/pma/subscriptions or /hub/pma/timers as needed."
            )
        return "\n".join(lines)

    def drain_pma_automation_wakeups(self, *, limit: int = 100) -> int:
        take = max(0, int(limit))
        if take <= 0:
            return 0
        if not self.hub_config.pma.enabled:
            return 0

        store = self.ensure_pma_automation_store()
        list_pending_wakeups = getattr(store, "list_pending_wakeups", None)
        mark_wakeup_dispatched = getattr(store, "mark_wakeup_dispatched", None)
        if not callable(list_pending_wakeups) or not callable(mark_wakeup_dispatched):
            return 0
        wakeups = list_pending_wakeups(limit=take)
        if not wakeups:
            return 0

        queue = PmaQueue(self.hub_config.root)
        drained = 0
        for wakeup in wakeups:
            wakeup_id = str(wakeup.get("wakeup_id") or "").strip()
            if not wakeup_id:
                continue
            wake_payload = {
                "wakeup_id": wakeup_id,
                "repo_id": wakeup.get("repo_id"),
                "run_id": wakeup.get("run_id"),
                "thread_id": wakeup.get("thread_id"),
                "lane_id": (
                    str(wakeup.get("lane_id")).strip()
                    if isinstance(wakeup.get("lane_id"), str)
                    and str(wakeup.get("lane_id")).strip()
                    else "pma:default"
                ),
                "from_state": wakeup.get("from_state"),
                "to_state": wakeup.get("to_state"),
                "reason": wakeup.get("reason"),
                "timestamp": wakeup.get("timestamp"),
                "source": wakeup.get("source"),
                "event_type": wakeup.get("event_type"),
                "subscription_id": wakeup.get("subscription_id"),
                "timer_id": wakeup.get("timer_id"),
            }
            payload = {
                "message": self._build_pma_wakeup_message(wake_payload),
                "agent": None,
                "model": None,
                "reasoning": None,
                "client_turn_id": wakeup_id,
                "stream": False,
                "hub_root": str(self.hub_config.root),
                "wake_up": wake_payload,
            }
            event_id = wakeup.get("event_id")
            event_type = wakeup.get("event_type")
            if (
                isinstance(event_id, str)
                and event_id
                and isinstance(event_type, str)
                and event_type
            ):
                metadata = wakeup.get("metadata")
                origin = (
                    metadata.get("origin")
                    if isinstance(metadata, dict)
                    and isinstance(metadata.get("origin"), str)
                    else "system"
                )
                payload["lifecycle_event"] = {
                    "event_id": event_id,
                    "event_type": event_type,
                    "repo_id": wakeup.get("repo_id"),
                    "run_id": wakeup.get("run_id"),
                    "timestamp": wakeup.get("timestamp"),
                    "data": (
                        dict(wakeup.get("event_data"))
                        if isinstance(wakeup.get("event_data"), dict)
                        else {}
                    ),
                    "origin": origin,
                }
            idempotency_key = (
                str(wakeup.get("idempotency_key")).strip()
                if isinstance(wakeup.get("idempotency_key"), str)
                and str(wakeup.get("idempotency_key")).strip()
                else f"automation:{wakeup_id}"
            )
            try:
                lane_id = (
                    str(wakeup.get("lane_id")).strip()
                    if isinstance(wakeup.get("lane_id"), str)
                    and str(wakeup.get("lane_id")).strip()
                    else "pma:default"
                )
                _, dupe_reason = queue.enqueue_sync(lane_id, idempotency_key, payload)
                self._request_pma_lane_worker_start(lane_id)
            except Exception:
                logger.exception(
                    "Failed to drain PMA automation wake-up %s into PMA queue",
                    wakeup_id,
                )
                continue
            if dupe_reason:
                logger.info(
                    "Deduped PMA queue item for automation wake-up %s: %s",
                    wakeup_id,
                    dupe_reason,
                )
            if mark_wakeup_dispatched(wakeup_id):
                drained += 1
        return drained

    def _build_pma_lifecycle_message(
        self, event: LifecycleEvent, *, reason: str
    ) -> str:
        lines = [
            "Lifecycle event received.",
            f"type: {event.event_type.value}",
            f"repo_id: {event.repo_id}",
            f"run_id: {event.run_id}",
            f"event_id: {event.event_id}",
        ]
        if reason:
            lines.append(f"reason: {reason}")
        if event.data:
            try:
                payload = json.dumps(event.data, sort_keys=True, ensure_ascii=True)
            except Exception:
                payload = str(event.data)
            lines.append(f"data: {payload}")
        if event.event_type == LifecycleEventType.DISPATCH_CREATED:
            lines.append("Dispatch requires attention; check the repo inbox.")
        return "\n".join(lines)

    def _build_lifecycle_retry_policy(self) -> LifecycleRetryPolicy:
        raw = getattr(self.hub_config, "raw", {})
        pma_config = raw.get("pma", {}) if isinstance(raw, dict) else {}
        if not isinstance(pma_config, dict):
            pma_config = {}

        def _read_int(key: str, fallback: int, *, minimum: int = 0) -> int:
            raw_value = pma_config.get(key, fallback)
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                return fallback
            return value if value >= minimum else fallback

        def _read_float(key: str, fallback: float, *, minimum: float = 0.0) -> float:
            raw_value = pma_config.get(key, fallback)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return fallback
            return value if value >= minimum else fallback

        max_attempts = _read_int("lifecycle_retry_max_attempts", 3, minimum=1)
        initial_backoff_seconds = _read_float(
            "lifecycle_retry_initial_backoff_seconds",
            5.0,
            minimum=0.0,
        )
        max_backoff_seconds = _read_float(
            "lifecycle_retry_max_backoff_seconds",
            300.0,
            minimum=0.0,
        )
        if max_backoff_seconds < initial_backoff_seconds:
            max_backoff_seconds = initial_backoff_seconds

        return LifecycleRetryPolicy(
            max_attempts=max_attempts,
            initial_backoff_seconds=initial_backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )

    def ensure_pma_safety_checker(self) -> PmaSafetyChecker:
        if self._pma_safety_checker is not None:
            return self._pma_safety_checker

        raw = getattr(self.hub_config, "raw", {})
        pma_config = raw.get("pma", {}) if isinstance(raw, dict) else {}
        if not isinstance(pma_config, dict):
            pma_config = {}

        def _resolve_int(key: str, fallback: int) -> int:
            raw_value = pma_config.get(key, fallback)
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                return fallback
            return value if value >= 0 else fallback

        safety_config = PmaSafetyConfig(
            dedup_window_seconds=_resolve_int("dedup_window_seconds", 300),
            max_duplicate_actions=_resolve_int("max_duplicate_actions", 3),
            rate_limit_window_seconds=_resolve_int("rate_limit_window_seconds", 60),
            max_actions_per_window=_resolve_int("max_actions_per_window", 20),
            circuit_breaker_threshold=_resolve_int("circuit_breaker_threshold", 5),
            circuit_breaker_cooldown_seconds=_resolve_int(
                "circuit_breaker_cooldown_seconds", 600
            ),
            enable_dedup=bool(pma_config.get("enable_dedup", True)),
            enable_rate_limit=bool(pma_config.get("enable_rate_limit", True)),
            enable_circuit_breaker=bool(pma_config.get("enable_circuit_breaker", True)),
        )
        self._pma_safety_checker = PmaSafetyChecker(
            self.hub_config.root, config=safety_config
        )
        return self._pma_safety_checker

    def get_pma_safety_checker(self) -> PmaSafetyChecker:
        return self.ensure_pma_safety_checker()

    def _pma_reactive_gate(self, event: LifecycleEvent) -> tuple[bool, str]:
        pma = self.hub_config.pma
        reactive_enabled = getattr(pma, "reactive_enabled", True)
        if not reactive_enabled:
            return False, "reactive_disabled"

        origin = (event.origin or "").strip().lower()
        blocked_origins = getattr(pma, "reactive_origin_blocklist", [])
        if blocked_origins:
            blocked = {str(value).strip().lower() for value in blocked_origins}
            if origin and origin in blocked:
                logger.info(
                    "Skipping PMA reactive trigger for event %s due to origin=%s",
                    event.event_id,
                    origin,
                )
                return False, "reactive_origin_blocked"

        allowlist = getattr(pma, "reactive_event_types", None)
        if allowlist:
            if event.event_type.value not in set(allowlist):
                return False, "reactive_filtered"

        debounce_seconds = int(getattr(pma, "reactive_debounce_seconds", 0) or 0)
        if debounce_seconds > 0:
            key = f"{event.event_type.value}:{event.repo_id}:{event.run_id}"
            store = PmaReactiveStore(self.hub_config.root)
            if not store.check_and_update(key, debounce_seconds):
                return False, "reactive_debounced"

        safety_checker = self.ensure_pma_safety_checker()
        safety_check = safety_checker.check_reactive_turn()
        if not safety_check.allowed:
            logger.info(
                "Blocked PMA reactive trigger for event %s: %s",
                event.event_id,
                safety_check.reason,
            )
            return False, safety_check.reason or "reactive_blocked"

        return True, "reactive_allowed"

    def _enqueue_pma_for_lifecycle_event(
        self, event: LifecycleEvent, *, reason: str
    ) -> bool:
        if not self.hub_config.pma.enabled:
            return False

        async def _enqueue() -> tuple[object, Optional[str]]:
            queue = PmaQueue(self.hub_config.root)
            message = self._build_pma_lifecycle_message(event, reason=reason)
            payload = {
                "message": message,
                "agent": None,
                "model": None,
                "reasoning": None,
                "client_turn_id": event.event_id,
                "stream": False,
                "hub_root": str(self.hub_config.root),
                "lifecycle_event": {
                    "event_id": event.event_id,
                    "event_type": event.event_type.value,
                    "repo_id": event.repo_id,
                    "run_id": event.run_id,
                    "timestamp": event.timestamp,
                    "data": event.data,
                    "origin": event.origin,
                },
            }
            idempotency_key = f"lifecycle:{event.event_id}"
            return await queue.enqueue("pma:default", idempotency_key, payload)

        _, dupe_reason = self._run_coroutine(_enqueue())
        if dupe_reason:
            logger.info(
                "Deduped PMA queue item for lifecycle event %s: %s",
                event.event_id,
                dupe_reason,
            )
        return True

    def _process_lifecycle_event(self, event: LifecycleEvent) -> None:
        if event.processed:
            return
        event_id = event.event_id
        if not event_id:
            return

        decision = "skip"
        processed = False
        automation_wakeups = 0
        try:
            automation_wakeups = self._enqueue_automation_wakeups_for_lifecycle_event(
                event
            )
        except Exception:
            logger.exception(
                "Failed to enqueue lifecycle automation wake-ups for event %s",
                event.event_id,
            )
            automation_wakeups = 0

        if event.event_type == LifecycleEventType.DISPATCH_CREATED:
            if not self.hub_config.pma.enabled:
                decision = "pma_disabled"
                processed = True
            else:
                interceptor = self._ensure_dispatch_interceptor()
                repo_snapshot = None
                try:
                    snapshots = self.list_repos()
                    for snap in snapshots:
                        if snap.id == event.repo_id:
                            repo_snapshot = snap
                            break
                except Exception:
                    logger.exception(
                        "Failed to get repo snapshot for repo_id=%s", event.repo_id
                    )
                    repo_snapshot = None

                if repo_snapshot is None or not repo_snapshot.exists_on_disk:
                    decision = "repo_missing"
                    processed = True
                elif interceptor is not None:
                    result = self._run_coroutine(
                        interceptor.process_dispatch_event(event, repo_snapshot.path)
                    )
                    if result and result.action == "auto_resolved":
                        decision = "dispatch_auto_resolved"
                        processed = True
                    elif result and result.action == "ignore":
                        decision = "dispatch_ignored"
                        processed = True
                    else:
                        allowed, gate_reason = self._pma_reactive_gate(event)
                        if not allowed:
                            decision = gate_reason
                            processed = True
                        else:
                            decision = "dispatch_escalated"
                            processed = self._enqueue_pma_for_lifecycle_event(
                                event, reason="dispatch_escalated"
                            )
                else:
                    allowed, gate_reason = self._pma_reactive_gate(event)
                    if not allowed:
                        decision = gate_reason
                        processed = True
                    else:
                        decision = "dispatch_enqueued"
                        processed = self._enqueue_pma_for_lifecycle_event(
                            event, reason="dispatch_created"
                        )
        elif event.event_type in (
            LifecycleEventType.FLOW_PAUSED,
            LifecycleEventType.FLOW_COMPLETED,
            LifecycleEventType.FLOW_FAILED,
            LifecycleEventType.FLOW_STOPPED,
        ):
            if not self.hub_config.pma.enabled:
                decision = "pma_disabled"
                processed = True
            else:
                allowed, gate_reason = self._pma_reactive_gate(event)
                if not allowed:
                    decision = gate_reason
                    processed = True
                else:
                    decision = "flow_enqueued"
                    processed = self._enqueue_pma_for_lifecycle_event(
                        event, reason=event.event_type.value
                    )

        if processed:
            self.lifecycle_store.mark_processed(event_id)
            self.lifecycle_store.prune_processed(keep_last=50)

        logger.info(
            "Lifecycle event processed: event_id=%s type=%s repo_id=%s run_id=%s decision=%s processed=%s automation_wakeups=%s",
            event.event_id,
            event.event_type.value,
            event.repo_id,
            event.run_id,
            decision,
            processed,
            automation_wakeups,
        )

    def _snapshot_from_record(
        self,
        record: DiscoveryRecord,
        repos_by_id: Optional[Dict[str, ManifestRepo]] = None,
    ) -> RepoSnapshot:
        repo_path = record.absolute_path
        lock_path = repo_path / ".codex-autorunner" / "lock"
        lock_status = read_lock_status(lock_path)

        runner_state: Optional[RunnerState] = None
        if record.initialized:
            runner_state = load_state(repo_path / ".codex-autorunner" / "state.sqlite3")

        is_clean: Optional[bool] = None
        if record.exists_on_disk and git_available(repo_path):
            is_clean = git_is_clean(repo_path)

        status = self._derive_status(record, lock_status, runner_state)
        last_run_id = runner_state.last_run_id if runner_state else None
        repo_index = repos_by_id or {record.repo.id: record.repo}
        effective_destination = resolve_effective_repo_destination(
            record.repo, repo_index
        ).to_dict()
        return RepoSnapshot(
            id=record.repo.id,
            path=repo_path,
            display_name=record.repo.display_name or repo_path.name or record.repo.id,
            enabled=record.repo.enabled,
            auto_run=record.repo.auto_run,
            worktree_setup_commands=record.repo.worktree_setup_commands,
            kind=record.repo.kind,
            worktree_of=record.repo.worktree_of,
            branch=record.repo.branch,
            exists_on_disk=record.exists_on_disk,
            is_clean=is_clean,
            initialized=record.initialized,
            init_error=record.init_error,
            status=status,
            lock_status=lock_status,
            last_run_id=last_run_id,
            last_run_started_at=(
                runner_state.last_run_started_at if runner_state else None
            ),
            last_run_finished_at=(
                runner_state.last_run_finished_at if runner_state else None
            ),
            last_run_duration_seconds=None,
            last_exit_code=runner_state.last_exit_code if runner_state else None,
            runner_pid=runner_state.runner_pid if runner_state else None,
            effective_destination=effective_destination,
        )

    def _snapshot_from_agent_workspace(
        self, workspace: ManifestAgentWorkspace
    ) -> AgentWorkspaceSnapshot:
        workspace_path = (self.hub_config.root / workspace.path).resolve()
        effective_destination = resolve_effective_agent_workspace_destination(
            workspace
        ).to_dict()
        return AgentWorkspaceSnapshot(
            id=workspace.id,
            runtime=workspace.runtime,
            path=workspace_path,
            display_name=workspace.display_name or workspace_path.name or workspace.id,
            enabled=workspace.enabled,
            exists_on_disk=workspace_path.exists(),
            effective_destination=effective_destination,
        )

    def _run_worktree_setup_commands(
        self,
        worktree_path: Path,
        commands: Optional[List[str]],
        *,
        base_repo_id: str,
    ) -> None:
        normalized = [str(cmd).strip() for cmd in (commands or []) if str(cmd).strip()]
        if not normalized:
            return
        log_path = worktree_path / ".codex-autorunner" / "logs" / "worktree-setup.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"[{now_iso()}] base_repo={base_repo_id} commands={len(normalized)}\n"
            )
            for idx, command in enumerate(normalized, start=1):
                log_file.write(f"$ {command}\n")
                try:
                    proc = subprocess.run(
                        ["/bin/sh", "-lc", command],
                        cwd=str(worktree_path),
                        capture_output=True,
                        text=True,
                        timeout=600,
                        env=subprocess_env(),
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ValueError(
                        "Worktree setup command %d/%d timed out after 600s: %r"
                        % (idx, len(normalized), command)
                    ) from exc
                output = (proc.stdout or "") + (proc.stderr or "")
                if output:
                    log_file.write(output)
                    if not output.endswith("\n"):
                        log_file.write("\n")
                if proc.returncode != 0:
                    detail = output.strip() or f"exit {proc.returncode}"
                    raise ValueError(
                        "Worktree setup failed for command %d/%d (%r): %s"
                        % (idx, len(normalized), command, detail)
                    )
            log_file.write("\n")

    def _derive_status(
        self,
        record: DiscoveryRecord,
        lock_status: LockStatus,
        runner_state: Optional[RunnerState],
    ) -> RepoStatus:
        if not record.exists_on_disk:
            return RepoStatus.MISSING
        if record.init_error:
            return RepoStatus.INIT_ERROR
        if not record.initialized:
            return RepoStatus.UNINITIALIZED
        if runner_state and runner_state.status == "running":
            if lock_status == LockStatus.LOCKED_ALIVE:
                return RepoStatus.RUNNING
            return RepoStatus.IDLE
        if lock_status in (LockStatus.LOCKED_ALIVE, LockStatus.LOCKED_STALE):
            return RepoStatus.LOCKED
        if runner_state and runner_state.status == "error":
            return RepoStatus.ERROR
        return RepoStatus.IDLE


def _repo_id_from_url(url: str) -> str:
    name = (url or "").rstrip("/").split("/")[-1]
    if ":" in name:
        name = name.split(":")[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name.strip()
