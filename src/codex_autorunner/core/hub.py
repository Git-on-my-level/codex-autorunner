import asyncio
import importlib
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..discovery import DiscoveryRecord, discover_and_init
from ..manifest import (
    Manifest,
    ManifestAgentWorkspace,
    load_manifest,
    normalize_manifest_destination,
    sanitize_repo_id,
    save_manifest,
)
from ..tickets.outbox import set_lifecycle_emitter
from .config import (
    HubConfig,
    RepoConfig,
    derive_repo_config,
    load_hub_config,
)
from .hub_lifecycle import (
    HubLifecycleWorker,
    LifecycleEventProcessor,
    LifecycleRetryPolicy,
)
from .hub_lifecycle_routing import LifecycleEventRouter
from .hub_repo_manager import RepoManager
from .hub_runner_orchestrator import RunnerOrchestrator
from .hub_topology import (
    AgentWorkspaceSnapshot,
    HubState,
    LockStatus,  # noqa: F401  re-exported for consumers
    RepoSnapshot,
    RepoStatus,  # noqa: F401  re-exported for consumers
    build_agent_workspace_snapshot,
    build_agent_workspace_snapshots,
    build_full_topology,
    build_repo_snapshot,
    build_repo_snapshots,
    load_hub_state,
    normalize_pinned_parent_repo_ids,
    prune_pinned_parent_repo_ids,
    read_lock_status,  # noqa: F401  re-exported for consumers
    refresh_pma_threads_artifact,
    save_hub_state,
)
from .hub_worktree_manager import WorktreeManager
from .lifecycle_events import (
    LifecycleEvent,
    LifecycleEventEmitter,
    LifecycleEventStore,
)
from .pma_automation_store import DEFAULT_PMA_LANE_ID, PmaAutomationStore
from .pma_queue import PmaQueue
from .pma_safety import PmaSafetyChecker, PmaSafetyConfig
from .ports.backend_orchestrator import (
    BackendOrchestrator as BackendOrchestratorProtocol,
)
from .runner_controller import ProcessRunnerController, SpawnRunnerFn
from .runtime import RuntimeContext
from .state import RunnerState, now_iso  # noqa: F401
from .state_roots import resolve_hub_agent_workspace_root
from .types import AppServerSupervisorFactory, BackendFactory

logger = logging.getLogger("codex_autorunner.hub")

_LIST_REPOS_CACHE_TTL_SECONDS = 30.0

BackendFactoryBuilder = Callable[[Path, RepoConfig], BackendFactory]
AppServerSupervisorFactoryBuilder = Callable[[RepoConfig], AppServerSupervisorFactory]
BackendOrchestratorBuilder = Callable[[Path, RepoConfig], BackendOrchestratorProtocol]


class _HubWorktreeBridge:
    """Concrete WorktreeHubContext that delegates to HubSupervisor."""

    def __init__(self, supervisor: "HubSupervisor") -> None:
        self._supervisor = supervisor

    def invalidate_cache(self) -> None:
        self._supervisor._invalidate_list_cache()

    def snapshot_for_repo(self, repo_id: str) -> "RepoSnapshot":
        return self._supervisor._snapshot_for_repo(repo_id)

    def list_repos(self, *, use_cache: bool = True) -> list["RepoSnapshot"]:
        return self._supervisor.list_repos(use_cache=use_cache)

    def stop_runner(
        self,
        *,
        repo_id: str,
        repo_path: Path,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self._supervisor._stop_runner_and_wait_for_exit(
            repo_id=repo_id,
            repo_path=repo_path,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def archive_repo_state(
        self,
        *,
        repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        return self._supervisor.archive_repo_state(
            repo_id=repo_id,
            archive_note=archive_note,
            archive_profile=archive_profile,
        )

    def base_repo_paths(self, manifest: "Manifest") -> dict[str, Path]:
        return self._supervisor._base_repo_paths(manifest)

    def collect_unbound_repo_threads(
        self,
        *,
        manifest: Optional["Manifest"] = None,
    ) -> dict[str, list[str]]:
        return self._supervisor._collect_unbound_repo_threads(manifest=manifest)

    def archive_unbound_repo_threads(
        self,
        *,
        repo_id: str,
        unbound_threads_by_repo: Optional[dict[str, list[str]]] = None,
    ) -> list[str]:
        return self._supervisor._archive_unbound_repo_threads(
            repo_id=repo_id,
            unbound_threads_by_repo=unbound_threads_by_repo,
        )


def _load_managed_runtime_module() -> Any:
    try:
        return importlib.import_module("codex_autorunner.agents.managed_runtime")
    except (ImportError, ModuleNotFoundError):
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
            except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
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


HubStartupPhase = str
HUB_STARTUP_CONSTRUCTED: HubStartupPhase = "constructed"
HUB_STARTUP_RECONCILING: HubStartupPhase = "reconciling"
HUB_STARTUP_READY: HubStartupPhase = "ready"
HUB_STARTUP_STARTED: HubStartupPhase = "started"


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
        start_lifecycle_worker: bool = True,
    ):
        self._startup_phase: HubStartupPhase = HUB_STARTUP_CONSTRUCTED
        self.hub_config = hub_config
        self.state_path = hub_config.root / ".codex-autorunner" / "hub_state.json"
        self._runner_orchestrator = RunnerOrchestrator(
            hub_config,
            spawn_fn=spawn_fn,
            backend_factory_builder=backend_factory_builder,
            app_server_supervisor_factory_builder=(
                app_server_supervisor_factory_builder
            ),
            backend_orchestrator_builder=backend_orchestrator_builder,
            agent_id_validator=agent_id_validator,
        )
        self._spawn_fn = spawn_fn
        self._backend_factory_builder = backend_factory_builder
        self._app_server_supervisor_factory_builder = (
            app_server_supervisor_factory_builder
        )
        self._backend_orchestrator_builder = backend_orchestrator_builder
        self.state = load_hub_state(self.state_path, self.hub_config.root)
        self._list_cache_at: Optional[float] = None
        self._list_cache: Optional[List[RepoSnapshot]] = None
        self._startup_repo_state_pending = bool(self.state.repos)
        self._list_lock = threading.Lock()
        self._lifecycle_emitter = LifecycleEventEmitter(hub_config.root)
        self._lifecycle_router = LifecycleEventRouter(
            hub_config=hub_config,
            lifecycle_store=self.lifecycle_store,
            list_repos_fn=self.list_repos,
            ensure_pma_automation_store_fn=self.ensure_pma_automation_store,
            ensure_pma_safety_checker_fn=self.ensure_pma_safety_checker,
            run_coroutine_fn=self._run_coroutine,
            logger=logger,
        )
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
        self._pma_safety_checker: Optional[PmaSafetyChecker] = None
        self._pma_automation_store: Optional[PmaAutomationStore] = None
        self._pma_lane_worker_starter: Optional[Callable[[str], None]] = None
        self._scm_poll_processor = scm_poll_processor
        self._invalidation_callbacks: List[Callable[[], None]] = []
        self._repo_manager = RepoManager(
            hub_config,
            on_invalidate_cache=self._invalidate_list_cache,
            on_snapshot_for_repo=self._snapshot_for_repo,
            on_stop_runner=self._stop_runner_and_wait_for_exit,
            on_cleanup_worktree=self.cleanup_worktree,
            on_list_repos=self.list_repos,
            runners=self._runner_orchestrator.runners,
        )
        self._worktree_bridge = _HubWorktreeBridge(self)
        self._worktree_manager = WorktreeManager(
            hub_config,
            ctx=self._worktree_bridge,
        )
        self._wire_outbox_lifecycle()
        self._startup_phase = HUB_STARTUP_RECONCILING
        self._reconcile_startup()
        self._startup_phase = HUB_STARTUP_READY
        if start_lifecycle_worker:
            self.startup()

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
        snapshots, agent_workspaces, pinned_parent_repo_ids = build_full_topology(
            records,
            manifest.agent_workspaces,
            self.state.pinned_parent_repo_ids,
            self.hub_config.root,
        )
        self.state = HubState(
            last_scan_at=now_iso(),
            repos=snapshots,
            agent_workspaces=agent_workspaces,
            pinned_parent_repo_ids=pinned_parent_repo_ids,
        )
        save_hub_state(self.state_path, self.state, self.hub_config.root)
        refresh_pma_threads_artifact(self.hub_config.root)
        return snapshots

    def list_repos(self, *, use_cache: bool = True) -> List[RepoSnapshot]:
        with self._list_lock:
            if use_cache and self._list_cache and self._list_cache_at is not None:
                if (
                    time.monotonic() - self._list_cache_at
                    < _LIST_REPOS_CACHE_TTL_SECONDS
                ):
                    return self._list_cache
            if use_cache and self._startup_repo_state_pending and self.state.repos:
                self._startup_repo_state_pending = False
                self._list_cache = list(self.state.repos)
                self._list_cache_at = time.monotonic()
                return self._list_cache
            self._startup_repo_state_pending = False
            manifest, records = self._manifest_records(manifest_only=True)
            snapshots, agent_workspaces, pinned_parent_repo_ids = build_full_topology(
                records,
                manifest.agent_workspaces,
                self.state.pinned_parent_repo_ids,
                self.hub_config.root,
            )
            self.state = HubState(
                last_scan_at=self.state.last_scan_at,
                repos=snapshots,
                agent_workspaces=agent_workspaces,
                pinned_parent_repo_ids=pinned_parent_repo_ids,
            )
            save_hub_state(
                self.state_path,
                self.state,
                self.hub_config.root,
            )
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
                pinned_parent_repo_ids=normalize_pinned_parent_repo_ids(current),
            )
            save_hub_state(
                self.state_path,
                self.state,
                self.hub_config.root,
            )
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
        self.list_repos(use_cache=False)
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
        self.list_repos(use_cache=False)
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
        self.list_repos(use_cache=False)
        return self._snapshot_for_agent_workspace(workspace_id)

    def _reconcile_startup(self) -> None:
        try:
            _, records = self._manifest_records(manifest_only=True)
        except (OSError, ValueError, RuntimeError) as exc:
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
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
                logger.warning(
                    "Failed to reconcile runner state for %s: %s",
                    record.absolute_path,
                    exc,
                )

    def run_repo(self, repo_id: str, once: bool = False) -> RepoSnapshot:
        self._runner_orchestrator.run(repo_id, once=once)
        return self._snapshot_for_repo(repo_id)

    def stop_repo(self, repo_id: str) -> RepoSnapshot:
        self._runner_orchestrator.stop(repo_id)
        return self._snapshot_for_repo(repo_id)

    def _stop_runner_and_wait_for_exit(
        self,
        *,
        repo_id: str,
        repo_path: Path,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self._runner_orchestrator.stop_and_wait_for_exit(
            repo_id=repo_id,
            repo_path=repo_path,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def resume_repo(self, repo_id: str, once: bool = False) -> RepoSnapshot:
        self._runner_orchestrator.resume(repo_id, once=once)
        return self._snapshot_for_repo(repo_id)

    def kill_repo(self, repo_id: str) -> RepoSnapshot:
        self._runner_orchestrator.kill(repo_id)
        return self._snapshot_for_repo(repo_id)

    def init_repo(self, repo_id: str) -> RepoSnapshot:
        return self._repo_manager.init_repo(repo_id)

    def sync_main(self, repo_id: str) -> RepoSnapshot:
        return self._repo_manager.sync_main(repo_id)

    def create_repo(
        self,
        repo_id: str,
        repo_path: Optional[Path] = None,
        git_init: bool = True,
        force: bool = False,
    ) -> RepoSnapshot:
        return self._repo_manager.create_repo(
            repo_id, repo_path=repo_path, git_init=git_init, force=force
        )

    def clone_repo(
        self,
        *,
        git_url: str,
        repo_id: Optional[str] = None,
        repo_path: Optional[Path] = None,
        force: bool = False,
    ) -> RepoSnapshot:
        return self._repo_manager.clone_repo(
            git_url=git_url, repo_id=repo_id, repo_path=repo_path, force=force
        )

    def create_worktree(
        self,
        *,
        base_repo_id: str,
        branch: str,
        force: bool = False,
        start_point: Optional[str] = None,
    ) -> RepoSnapshot:
        return self._worktree_manager.create_worktree(
            base_repo_id=base_repo_id,
            branch=branch,
            force=force,
            start_point=start_point,
        )

    def set_worktree_setup_commands(
        self, repo_id: str, commands: List[str]
    ) -> RepoSnapshot:
        return self._worktree_manager.set_worktree_setup_commands(repo_id, commands)

    def run_setup_commands_for_workspace(
        self,
        workspace_path: Path,
        *,
        repo_id_hint: Optional[str] = None,
    ) -> int:
        return self._worktree_manager.run_setup_commands_for_workspace(
            workspace_path,
            repo_id_hint=repo_id_hint,
        )

    def _archive_bound_pma_threads(
        self,
        *,
        worktree_repo_id: str,
        worktree_path: Path,
    ) -> list[str]:
        return self._worktree_manager._archive_bound_pma_threads(
            worktree_repo_id=worktree_repo_id,
            worktree_path=worktree_path,
        )

    def _bound_thread_target_ids(self) -> set[str]:
        return self._repo_manager._bound_thread_target_ids()

    def _base_repo_paths(self, manifest: Manifest) -> dict[str, Path]:
        return self._repo_manager._base_repo_paths(manifest)

    def _collect_unbound_repo_threads(
        self,
        *,
        manifest: Optional[Manifest] = None,
    ) -> dict[str, list[str]]:
        return self._repo_manager._collect_unbound_repo_threads(manifest=manifest)

    def _archive_unbound_repo_threads(
        self,
        *,
        repo_id: str,
        unbound_threads_by_repo: Optional[dict[str, list[str]]] = None,
    ) -> list[str]:
        return self._repo_manager._archive_unbound_repo_threads(
            repo_id=repo_id,
            unbound_threads_by_repo=unbound_threads_by_repo,
        )

    def unbound_repo_thread_counts(self) -> dict[str, int]:
        return self._repo_manager.unbound_repo_thread_counts()

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
        return self._worktree_manager.cleanup_worktree(
            worktree_repo_id=worktree_repo_id,
            delete_branch=delete_branch,
            delete_remote=delete_remote,
            archive=archive,
            force_archive=force_archive,
            archive_note=archive_note,
            force=force,
            force_attestation=force_attestation,
            archive_profile=archive_profile,
        )

    def archive_worktree(
        self,
        *,
        worktree_repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        return self._worktree_manager.archive_worktree(
            worktree_repo_id=worktree_repo_id,
            archive_note=archive_note,
            archive_profile=archive_profile,
        )

    def archive_repo_state(
        self,
        *,
        repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        return self._repo_manager.archive_repo_state(
            repo_id=repo_id,
            archive_note=archive_note,
            archive_profile=archive_profile,
        )

    def archive_worktree_state(
        self,
        *,
        worktree_repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        return self._worktree_manager.archive_worktree_state(
            worktree_repo_id=worktree_repo_id,
            archive_note=archive_note,
            archive_profile=archive_profile,
        )

    def cleanup_repo_threads(self, *, repo_id: str) -> Dict[str, object]:
        return self._repo_manager.cleanup_repo_threads(repo_id=repo_id)

    def cleanup_all_repo_threads(self) -> Dict[str, object]:
        return self._repo_manager.cleanup_all_repo_threads()

    def cleanup_all(self, *, dry_run: bool = False) -> Dict[str, object]:
        return self._worktree_manager.cleanup_all(dry_run=dry_run)

    def check_repo_removal(self, repo_id: str) -> Dict[str, object]:
        return self._repo_manager.check_repo_removal(repo_id)

    def remove_repo(
        self,
        repo_id: str,
        *,
        force: bool = False,
        delete_dir: bool = True,
        delete_worktrees: bool = False,
        force_attestation: Optional[Mapping[str, object]] = None,
    ) -> None:
        self._repo_manager.remove_repo(
            repo_id,
            force=force,
            delete_dir=delete_dir,
            delete_worktrees=delete_worktrees,
            force_attestation=force_attestation,
        )

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
        return build_repo_snapshots(records)

    def _build_agent_workspace_snapshots(
        self, workspaces: List[ManifestAgentWorkspace]
    ) -> List[AgentWorkspaceSnapshot]:
        return build_agent_workspace_snapshots(workspaces, self.hub_config.root)

    def _prune_pinned_parent_repo_ids(self, snapshots: List[RepoSnapshot]) -> List[str]:
        return prune_pinned_parent_repo_ids(
            self.state.pinned_parent_repo_ids, snapshots
        )

    def _snapshot_for_repo(self, repo_id: str) -> RepoSnapshot:
        _, records = self._manifest_records(manifest_only=True)
        record = next((r for r in records if r.repo.id == repo_id), None)
        if not record:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repos_by_id = {entry.repo.id: entry.repo for entry in records}
        snapshot = build_repo_snapshot(record, repos_by_id)
        self.list_repos(use_cache=False)
        return snapshot

    def _snapshot_for_agent_workspace(
        self, workspace_id: str
    ) -> AgentWorkspaceSnapshot:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        workspace = manifest.get_agent_workspace(workspace_id)
        if not workspace:
            raise ValueError(f"Agent workspace {workspace_id} not found in manifest")
        return build_agent_workspace_snapshot(workspace, self.hub_config.root)

    def register_invalidation_callback(self, callback: Callable[[], None]) -> None:
        self._invalidation_callbacks.append(callback)

    def _invalidate_list_cache(self) -> None:
        with self._list_lock:
            self._list_cache = None
            self._list_cache_at = None
            self._startup_repo_state_pending = False
        for cb in self._invalidation_callbacks:
            try:
                cb()
            except (OSError, ValueError, TypeError, RuntimeError):
                logger.exception("Invalidation callback failed")

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
        except (RuntimeError, OSError, ValueError, TypeError):
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

    def _process_lifecycle_event_cycle(self) -> bool:
        productive = False
        if self.process_lifecycle_events() > 0:
            productive = True
        scm_counts = self.process_scm_automation_polls()
        if scm_counts.get("polled", 0) > 0 or scm_counts.get("events_emitted", 0) > 0:
            productive = True
        timer_count = self.process_pma_automation_timers()
        if timer_count > 0:
            productive = True
        wakeup_count = self.drain_pma_automation_wakeups()
        if wakeup_count > 0:
            productive = True
        return productive

    def process_lifecycle_events(self) -> int:
        processed = self._lifecycle_event_processor.process_events(limit=100)
        try:
            self.drain_pma_automation_wakeups()
        except Exception:
            logger.exception("Failed draining PMA automation wake-ups")
        return processed

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
                "candidate_workspaces_scanned": 0,
                "bindings_discovered": 0,
                "watches_armed": 0,
                "discovery_errors": 0,
                "invalid_bindings_skipped": 0,
                "rate_limited_skipped": 0,
            }
        try:
            return processor(limit)
        except (RuntimeError, OSError, ValueError, TypeError, sqlite3.Error):
            logger.exception("Failed processing SCM automation polling watches")
            return {
                "due": 0,
                "polled": 0,
                "events_emitted": 0,
                "expired": 0,
                "closed": 0,
                "errors": 1,
                "candidate_workspaces": 0,
                "candidate_workspaces_scanned": 0,
                "bindings_discovered": 0,
                "watches_armed": 0,
                "discovery_errors": 1,
                "invalid_bindings_skipped": 0,
                "rate_limited_skipped": 0,
            }

    def _start_lifecycle_event_processor(self) -> None:
        self._lifecycle_worker.start()

    def _stop_lifecycle_event_processor(self) -> None:
        self._lifecycle_worker.stop()

    @property
    def startup_phase(self) -> HubStartupPhase:
        return self._startup_phase

    def startup(self) -> None:
        self._start_lifecycle_event_processor()
        self._startup_phase = HUB_STARTUP_STARTED

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
                self._lifecycle_worker.wake()

        set_lifecycle_emitter(_emit_outbox_event)

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
            metadata = wakeup.get("metadata")
            if isinstance(metadata, dict):
                wake_payload["metadata"] = dict(metadata)
                delivery_target = metadata.get("delivery_target")
                if isinstance(delivery_target, dict):
                    wake_payload["delivery_target"] = dict(delivery_target)
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
            except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError):
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

    def _process_lifecycle_event(self, event: LifecycleEvent) -> None:
        self._lifecycle_router.route_event(event)
