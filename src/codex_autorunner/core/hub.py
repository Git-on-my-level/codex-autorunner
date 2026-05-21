import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..discovery import discover_and_init
from ..manifest import Manifest
from .automation import (
    EXECUTOR_AGENT_TASK_TURN,
    EXECUTOR_GITHUB_COMMENT,
    EXECUTOR_GITHUB_REACTION,
    EXECUTOR_MANAGED_THREAD_TURN,
    EXECUTOR_PUBLISH_CHAT_NOTIFICATION,
    EXECUTOR_PUBLISH_OPERATION,
    EXECUTOR_TICKET_FLOW,
    AgentTaskTurnAutomationExecutor,
    AutomationExecutorRegistry,
    ManagedThreadTurnAutomationExecutor,
    PublishOperationAutomationExecutor,
)
from .automation.store import AutomationStore
from .automation.ticket_flow_executor import TicketFlowAutomationExecutor
from .config import (
    HubConfig,
    RepoConfig,
    derive_repo_config,
    load_hub_config,
)
from .hub_lifecycle import HubLifecycleOrchestrator
from .hub_repo_manager import RepoManager
from .hub_runner_orchestrator import RunnerOrchestrator
from .hub_topology import (
    HubState,
    HubTopologyRepository,
    RepoSnapshot,
    RepoTopologyRecord,
    load_hub_state,
    normalize_hub_title,
    normalize_pinned_parent_repo_ids,
    refresh_managed_threads_artifact,
    save_hub_state,
)
from .hub_worktree_manager import WorktreeManager
from .lifecycle_events import (
    LifecycleEvent,
    LifecycleEventEmitter,
    LifecycleEventStore,
)
from .pma_automation_types import DEFAULT_PMA_LANE_ID
from .pma_safety import PmaSafetyChecker, PmaSafetyConfig
from .ports.backend_orchestrator import (
    BackendOrchestrator as BackendOrchestratorProtocol,
)
from .publish_executor import PublishExecutorRegistry
from .publish_operation_executors import (
    build_enqueue_managed_turn_executor,
    build_notify_chat_executor,
)
from .runner_controller import ProcessRunnerController, SpawnRunnerFn
from .runtime import RuntimeContext
from .state import now_iso
from .types import AppServerSupervisorFactory, BackendFactory

logger = logging.getLogger("codex_autorunner.hub")

_LIST_REPOS_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class PmaLaneWorkerStartResult:
    accepted: bool
    reason: str
    lane_id: str


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
        self._topology_repository = HubTopologyRepository(
            hub_root=hub_config.root,
            manifest_path=hub_config.manifest_path,
        )
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
        self._pma_safety_checker: Optional[PmaSafetyChecker] = None
        self._pma_lane_worker_starter: Optional[Callable[[str], None]] = None
        self._managed_thread_queue_worker_starter: Optional[Callable[[str], None]] = (
            None
        )
        self._scm_poll_processor = scm_poll_processor
        self._invalidation_callbacks: List[Callable[[], None]] = []
        self._worktree_bridge = _HubWorktreeBridge(self)
        self._worktree_manager = WorktreeManager(
            hub_config,
            topology_repository=self._topology_repository,
            ctx=self._worktree_bridge,
        )
        automation_executor_registry = AutomationExecutorRegistry()
        automation_executor_registry.register(
            EXECUTOR_TICKET_FLOW,
            TicketFlowAutomationExecutor(
                hub_root=hub_config.root,
                topology_repository=self._topology_repository,
                worktree_manager=self._worktree_manager,
                run_coroutine_fn=self._run_coroutine,
            ),
        )
        automation_executor_registry.register(
            EXECUTOR_AGENT_TASK_TURN,
            AgentTaskTurnAutomationExecutor(
                hub_root=hub_config.root,
                automation_store=AutomationStore(hub_config.root),
                safety_checker_fn=lambda: self.ensure_pma_safety_checker(),
                queue_worker_starter_fn=self._request_managed_thread_queue_worker_start,
                queue_worker_available_fn=(self._managed_thread_queue_worker_available),
            ),
        )
        automation_executor_registry.register(
            EXECUTOR_MANAGED_THREAD_TURN,
            ManagedThreadTurnAutomationExecutor(
                hub_root=hub_config.root,
                automation_store=AutomationStore(hub_config.root),
                safety_checker_fn=lambda: self.ensure_pma_safety_checker(),
                queue_worker_starter_fn=self._request_managed_thread_queue_worker_start,
                queue_worker_available_fn=(self._managed_thread_queue_worker_available),
            ),
        )
        publish_registry = PublishExecutorRegistry(
            {
                "enqueue_managed_turn": build_enqueue_managed_turn_executor(
                    hub_root=hub_config.root
                ),
                "notify_chat": build_notify_chat_executor(hub_root=hub_config.root),
            },
            mutation_policy_config=hub_config.raw,
        )
        publish_executor = PublishOperationAutomationExecutor(
            hub_root=hub_config.root,
            executor_registry=publish_registry,
        )
        for executor_kind in (
            EXECUTOR_PUBLISH_OPERATION,
            EXECUTOR_PUBLISH_CHAT_NOTIFICATION,
            EXECUTOR_GITHUB_REACTION,
            EXECUTOR_GITHUB_COMMENT,
        ):
            automation_executor_registry.register(executor_kind, publish_executor)
        self._lifecycle_orchestrator = HubLifecycleOrchestrator(
            hub_config,
            list_repos_fn=lambda: self.list_repos(),
            run_coroutine_fn=self._run_coroutine,
            process_scm_polls_fn=lambda: self.process_scm_automation_polls(),
            process_pma_timers_fn=lambda: self.process_automation_timers(),
            automation_executor_registry=automation_executor_registry,
            logger=logger,
        )
        self._lifecycle_orchestrator._process_event_fn = lambda event: (
            self._process_lifecycle_event(event)
        )
        self._repo_manager = RepoManager(
            hub_config,
            topology_repository=self._topology_repository,
            on_invalidate_cache=self._invalidate_list_cache,
            on_snapshot_for_repo=self._snapshot_for_repo,
            on_stop_runner=self._stop_runner_and_wait_for_exit,
            on_retire_worktree=self.retire_worktree,
            on_list_repos=self.list_repos,
            runners=self._runner_orchestrator.runners,
        )
        self._lifecycle_orchestrator.wire_outbox_lifecycle()
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
        self.state = self._topology_repository.build_hub_state(
            existing_pinned_parent_repo_ids=self.state.pinned_parent_repo_ids,
            last_scan_at=now_iso(),
            title=self.state.title,
            manifest=manifest,
            records=records,
        )
        save_hub_state(self.state_path, self.state, self.hub_config.root)
        refresh_managed_threads_artifact(self.hub_config.root)
        return list(self.state.repos)

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
            self.state = self._topology_repository.build_hub_state(
                existing_pinned_parent_repo_ids=self.state.pinned_parent_repo_ids,
                last_scan_at=self.state.last_scan_at,
                title=self.state.title,
                manifest=manifest,
                records=records,
            )
            save_hub_state(
                self.state_path,
                self.state,
                self.hub_config.root,
            )
            self._list_cache = list(self.state.repos)
            self._list_cache_at = time.monotonic()
            return self._list_cache

    def set_parent_repo_pinned(self, repo_id: str, pinned: bool) -> List[str]:
        manifest = self._topology_repository.load_manifest()
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
                pinned_parent_repo_ids=normalize_pinned_parent_repo_ids(current),
                title=self.state.title,
            )
            save_hub_state(
                self.state_path,
                self.state,
                self.hub_config.root,
            )
            return list(self.state.pinned_parent_repo_ids)

    def get_hub_title(self) -> str:
        return normalize_hub_title(self.state.title)

    def set_hub_title(self, title: str) -> str:
        with self._list_lock:
            normalized = normalize_hub_title(title)
            self.state = HubState(
                last_scan_at=self.state.last_scan_at,
                repos=self.state.repos,
                pinned_parent_repo_ids=self.state.pinned_parent_repo_ids,
                title=normalized,
            )
            save_hub_state(self.state_path, self.state, self.hub_config.root)
            return normalized

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

    def _archive_bound_managed_threads(
        self,
        *,
        worktree_repo_id: str,
        worktree_path: Path,
    ) -> list[str]:
        return self._worktree_manager._archive_bound_managed_threads(
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

    def retire_worktree(
        self,
        *,
        worktree_repo_id: str,
        delete_branch: bool = False,
        delete_remote: bool = False,
        force_archive: bool = False,
        archive_note: Optional[str] = None,
        force: bool = False,
        force_attestation: Optional[Mapping[str, object]] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        return self._worktree_manager.retire_worktree(
            worktree_repo_id=worktree_repo_id,
            delete_branch=delete_branch,
            delete_remote=delete_remote,
            force_archive=force_archive,
            archive_note=archive_note,
            force=force,
            force_attestation=force_attestation,
            archive_profile=archive_profile,
        )

    def delete_worktree(
        self,
        *,
        worktree_repo_id: str,
        delete_branch: bool = False,
        delete_remote: bool = False,
        force: bool = False,
        force_attestation: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        return self._worktree_manager.delete_worktree(
            worktree_repo_id=worktree_repo_id,
            delete_branch=delete_branch,
            delete_remote=delete_remote,
            force=force,
            force_attestation=force_attestation,
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
    ) -> Tuple[Manifest, Sequence[RepoTopologyRecord]]:
        manifest, records = self._topology_repository.manifest_records()
        if manifest_only:
            return manifest, records
        return manifest, records

    def _snapshot_for_repo(self, repo_id: str) -> RepoSnapshot:
        self.list_repos(use_cache=False)
        snapshot = next((item for item in self.state.repos if item.id == repo_id), None)
        if snapshot is None:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        return snapshot

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
        return self._lifecycle_orchestrator.lifecycle_emitter

    @property
    def lifecycle_store(self) -> LifecycleEventStore:
        return self._lifecycle_orchestrator.lifecycle_store

    def set_pma_lane_worker_starter(
        self, starter: Optional[Callable[[str], None]]
    ) -> None:
        self._pma_lane_worker_starter = starter

    def set_managed_thread_queue_worker_starter(
        self, starter: Optional[Callable[[str], None]]
    ) -> None:
        self._managed_thread_queue_worker_starter = starter

    def _request_managed_thread_queue_worker_start(self, thread_id: str) -> None:
        starter = self._managed_thread_queue_worker_starter
        if starter is None:
            return
        try:
            starter(thread_id)
        except (RuntimeError, OSError, ValueError, TypeError):
            logger.exception(
                "Failed requesting managed-thread queue worker startup for thread_id=%s",
                thread_id,
            )

    def _managed_thread_queue_worker_available(self) -> bool:
        return self._managed_thread_queue_worker_starter is not None

    def _request_pma_lane_worker_start(
        self, lane_id: Optional[str]
    ) -> PmaLaneWorkerStartResult:
        starter = self._pma_lane_worker_starter
        normalized_lane_id = (
            lane_id.strip()
            if isinstance(lane_id, str) and lane_id.strip()
            else DEFAULT_PMA_LANE_ID
        )
        if starter is None:
            return PmaLaneWorkerStartResult(
                accepted=True,
                reason="no_lane_worker_starter_configured",
                lane_id=normalized_lane_id,
            )
        try:
            starter(normalized_lane_id)
            return PmaLaneWorkerStartResult(
                accepted=True,
                reason="accepted",
                lane_id=normalized_lane_id,
            )
        except (RuntimeError, OSError, ValueError, TypeError):
            logger.exception(
                "Failed requesting PMA lane worker startup for lane_id=%s",
                normalized_lane_id,
            )
            return PmaLaneWorkerStartResult(
                accepted=False,
                reason="starter_failed",
                lane_id=normalized_lane_id,
            )

    def process_automation_now(
        self, *, include_timers: bool = True, limit: int = 100
    ) -> dict[str, int]:
        timer_wakeups = (
            self.process_automation_timers(limit=limit) if include_timers else 0
        )
        automation_processed = self._lifecycle_orchestrator.process_automation()
        return {
            "timers_processed": timer_wakeups,
            "automation_processed": automation_processed,
            "wakeups_dispatched": 0,
        }

    def trigger_pma_from_lifecycle_event(self, event: LifecycleEvent) -> None:
        self._lifecycle_orchestrator.trigger_pma_from_lifecycle_event(event)

    def process_lifecycle_events(self) -> int:
        return self._lifecycle_orchestrator.process_lifecycle_events()

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

    @property
    def startup_phase(self) -> HubStartupPhase:
        return self._startup_phase

    def startup(self) -> None:
        self._lifecycle_orchestrator.startup()
        self._startup_phase = HUB_STARTUP_STARTED

    def _build_lifecycle_retry_policy(self):
        return self._lifecycle_orchestrator._build_lifecycle_retry_policy()

    @property
    def _lifecycle_router(self):
        return self._lifecycle_orchestrator._lifecycle_router

    def _process_lifecycle_event(self, event: LifecycleEvent) -> None:
        self._lifecycle_orchestrator._lifecycle_router.route_event(event)

    def shutdown(self) -> None:
        self._lifecycle_orchestrator.shutdown()

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

    def process_automation_timers(self, *, limit: int = 100) -> int:
        take = max(0, int(limit))
        if take <= 0:
            return 0

        scheduler = self._lifecycle_orchestrator._automation_scheduler
        worker = self._lifecycle_orchestrator._automation_job_worker
        scheduler.process_due(limit=take)
        worker_result = worker.process_once(limit=take)
        return worker_result.running + worker_result.succeeded + worker_result.skipped

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
