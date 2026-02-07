import asyncio
import dataclasses
import enum
import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..bootstrap import seed_repo_files
from ..discovery import DiscoveryRecord, discover_and_init
from ..manifest import (
    Manifest,
    ensure_unique_repo_id,
    load_manifest,
    sanitize_repo_id,
    save_manifest,
)
from ..tickets.outbox import set_lifecycle_emitter
from .archive import archive_worktree_snapshot, build_snapshot_id
from .config import HubConfig, RepoConfig, derive_repo_config, load_hub_config
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
from .lifecycle_events import (
    LifecycleEvent,
    LifecycleEventEmitter,
    LifecycleEventStore,
    LifecycleEventType,
)
from .locks import DEFAULT_RUNNER_CMD_HINTS, assess_lock, process_alive
from .pma_dispatch_interceptor import PmaDispatchInterceptor
from .pma_queue import PmaQueue
from .pma_reactive import PmaReactiveStore
from .pma_safety import PmaSafetyChecker, PmaSafetyConfig
from .ports.backend_orchestrator import (
    BackendOrchestrator as BackendOrchestratorProtocol,
)
from .runner_controller import ProcessRunnerController, SpawnRunnerFn
from .runtime import RuntimeContext
from .state import RunnerState, load_state, now_iso
from .types import AppServerSupervisorFactory, BackendFactory
from .utils import atomic_write, is_within

logger = logging.getLogger("codex_autorunner.hub")

BackendFactoryBuilder = Callable[[Path, RepoConfig], BackendFactory]
AppServerSupervisorFactoryBuilder = Callable[[RepoConfig], AppServerSupervisorFactory]
BackendOrchestratorBuilder = Callable[[Path, RepoConfig], BackendOrchestratorProtocol]


def _git_failure_detail(proc) -> str:
    return (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"


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
            "last_exit_code": self.last_exit_code,
            "runner_pid": self.runner_pid,
        }


@dataclasses.dataclass
class HubState:
    last_scan_at: Optional[str]
    repos: List[RepoSnapshot]

    def to_dict(self, hub_root: Path) -> Dict[str, object]:
        return {
            "last_scan_at": self.last_scan_at,
            "repos": [repo.to_dict(hub_root) for repo in self.repos],
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
        return HubState(last_scan_at=None, repos=[])
    data = state_path.read_text(encoding="utf-8")
    try:
        import json

        payload = json.loads(data)
    except Exception as exc:
        logger.warning("Failed to parse hub state from %s: %s", state_path, exc)
        return HubState(last_scan_at=None, repos=[])
    last_scan_at = payload.get("last_scan_at")
    repos_payload = payload.get("repos") or []
    repos: List[RepoSnapshot] = []
    for entry in repos_payload:
        try:
            repo = RepoSnapshot(
                id=str(entry.get("id")),
                path=hub_root / entry.get("path", ""),
                display_name=str(entry.get("display_name", "")),
                enabled=bool(entry.get("enabled", True)),
                auto_run=bool(entry.get("auto_run", False)),
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
                last_exit_code=entry.get("last_exit_code"),
                runner_pid=entry.get("runner_pid"),
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
    return HubState(last_scan_at=last_scan_at, repos=repos)


def save_hub_state(state_path: Path, state: HubState, hub_root: Path) -> None:
    payload = state.to_dict(hub_root)
    import json

    atomic_write(state_path, json.dumps(payload, indent=2) + "\n")


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
        self._lifecycle_task_lock = threading.Lock()
        self._lifecycle_stop_event = threading.Event()
        self._lifecycle_thread: Optional[threading.Thread] = None
        self._dispatch_interceptor_task: Optional[asyncio.Task] = None
        self._dispatch_interceptor_stop_event: Optional[threading.Event] = None
        self._dispatch_interceptor_thread: Optional[threading.Thread] = None
        self._dispatch_interceptor: Optional[PmaDispatchInterceptor] = None
        self._pma_safety_checker: Optional[PmaSafetyChecker] = None
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
    ) -> "HubSupervisor":
        config = load_hub_config(path)
        return cls(
            config,
            backend_factory_builder=backend_factory_builder,
            app_server_supervisor_factory_builder=app_server_supervisor_factory_builder,
            backend_orchestrator_builder=backend_orchestrator_builder,
        )

    def scan(self) -> List[RepoSnapshot]:
        self._invalidate_list_cache()
        manifest, records = discover_and_init(self.hub_config)
        snapshots = self._build_snapshots(records)
        self.state = HubState(last_scan_at=now_iso(), repos=snapshots)
        save_hub_state(self.state_path, self.state, self.hub_config.root)
        return snapshots

    def list_repos(self, *, use_cache: bool = True) -> List[RepoSnapshot]:
        with self._list_lock:
            if use_cache and self._list_cache and self._list_cache_at is not None:
                if time.monotonic() - self._list_cache_at < 2.0:
                    return self._list_cache
            manifest, records = self._manifest_records(manifest_only=True)
            snapshots = self._build_snapshots(records)
            self.state = HubState(last_scan_at=self.state.last_scan_at, repos=snapshots)
            save_hub_state(self.state_path, self.state, self.hub_config.root)
            self._list_cache = snapshots
            self._list_cache_at = time.monotonic()
            return snapshots

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
        if not is_within(worktrees_root, worktree_path):
            raise ValueError(
                "Worktree path escapes worktrees_root: "
                f"{worktree_path} (root={worktrees_root})"
            )
        if worktree_path.exists() and not force:
            raise ValueError(f"Worktree path already exists: {worktree_path}")

        # Create the worktree (branch may or may not exist locally).
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
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
                proc = run_git(
                    ["worktree", "add", str(worktree_path), branch],
                    base_path,
                    check=False,
                    timeout_seconds=120,
                )
            else:
                cmd = ["worktree", "add", "-b", branch, str(worktree_path)]
                if start_point:
                    cmd.append(start_point)
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
        return self._snapshot_for_repo(repo_id)

    def cleanup_worktree(
        self,
        *,
        worktree_repo_id: str,
        delete_branch: bool = False,
        delete_remote: bool = False,
        archive: bool = True,
        force_archive: bool = False,
        archive_note: Optional[str] = None,
    ) -> None:
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

        # Stop any runner first.
        runner = self._ensure_runner(worktree_repo_id, allow_uninitialized=True)
        if runner:
            runner.stop()

        if archive:
            branch_name = entry.branch or git_branch(worktree_path) or "unknown"
            head_sha = git_head_sha(worktree_path) or "unknown"
            snapshot_id = build_snapshot_id(branch_name, head_sha)
            logger.info(
                "Hub archive worktree start id=%s snapshot_id=%s",
                worktree_repo_id,
                snapshot_id,
            )
            try:
                result = archive_worktree_snapshot(
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
                )
            except Exception as exc:
                logger.exception(
                    "Hub archive worktree failed id=%s snapshot_id=%s",
                    worktree_repo_id,
                    snapshot_id,
                )
                if not force_archive:
                    raise ValueError(f"Worktree archive failed: {exc}") from exc
            else:
                logger.info(
                    "Hub archive worktree complete id=%s snapshot_id=%s status=%s",
                    worktree_repo_id,
                    result.snapshot_id,
                    result.status,
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
    ) -> None:
        self._invalidate_list_cache()
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")

        if repo.kind == "worktree":
            self.cleanup_worktree(worktree_repo_id=repo_id)
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
                self.cleanup_worktree(worktree_repo_id=worktree.id)
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

        runner = self._ensure_runner(repo_id, allow_uninitialized=True)
        if runner:
            runner.stop()
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
        snapshots: List[RepoSnapshot] = []
        for record in records:
            snapshots.append(self._snapshot_from_record(record))
        return snapshots

    def _snapshot_for_repo(self, repo_id: str) -> RepoSnapshot:
        _, records = self._manifest_records(manifest_only=True)
        record = next((r for r in records if r.repo.id == repo_id), None)
        if not record:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        snapshot = self._snapshot_from_record(record)
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

    def trigger_pma_from_lifecycle_event(self, event: LifecycleEvent) -> None:
        self._process_lifecycle_event(event)

    def process_lifecycle_events(self) -> None:
        events = self.lifecycle_store.get_unprocessed(limit=100)
        if not events:
            return
        for event in events:
            try:
                self._process_lifecycle_event(event)
            except Exception as exc:
                logger.exception(
                    "Failed to process lifecycle event %s: %s", event.event_id, exc
                )

    def _start_lifecycle_event_processor(self) -> None:
        if self._lifecycle_thread is not None:
            return

        def _process_loop():
            while not self._lifecycle_stop_event.wait(5.0):
                try:
                    self.process_lifecycle_events()
                except Exception:
                    logger.exception("Error in lifecycle event processor")

        self._lifecycle_thread = threading.Thread(
            target=_process_loop, daemon=True, name="lifecycle-event-processor"
        )
        self._lifecycle_thread.start()

    def _stop_lifecycle_event_processor(self) -> None:
        if self._lifecycle_thread is None:
            return
        self._lifecycle_stop_event.set()
        self._lifecycle_thread.join(timeout=2.0)
        self._lifecycle_thread = None

    def shutdown(self) -> None:
        self._stop_lifecycle_event_processor()
        self._stop_dispatch_interceptor()
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

    def _start_dispatch_interceptor(self) -> None:
        if not self.hub_config.pma.enabled:
            return
        if not self.hub_config.pma.dispatch_interception_enabled:
            return
        if self._dispatch_interceptor_thread is not None:
            return

        import asyncio
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            pass

        def _run_interceptor():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            from .pma_dispatch_interceptor import run_dispatch_interceptor

            stop_event = threading.Event()
            self._dispatch_interceptor_stop_event = stop_event

            async def run_until_stop():
                task = None
                try:
                    task = await run_dispatch_interceptor(
                        hub_root=self.hub_config.root,
                        supervisor=self,
                        interval_seconds=5.0,
                        on_intercept=self._on_dispatch_intercept,
                    )
                    while not stop_event.is_set():
                        await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    pass
                finally:
                    if task is not None and not task.done():
                        task.cancel()
                    if task is not None:
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass

            loop.run_until_complete(run_until_stop())
            loop.close()

        self._dispatch_interceptor_thread = threading.Thread(
            target=_run_interceptor, daemon=True, name="pma-dispatch-interceptor"
        )
        self._dispatch_interceptor_thread.start()

    def _stop_dispatch_interceptor(self) -> None:
        if self._dispatch_interceptor_stop_event is not None:
            self._dispatch_interceptor_stop_event.set()
        if self._dispatch_interceptor_thread is not None:
            self._dispatch_interceptor_thread.join(timeout=2.0)
            self._dispatch_interceptor_thread = None
            self._dispatch_interceptor_stop_event = None

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
            return asyncio.run(coro)
        except RuntimeError as exc:
            if "asyncio.run() cannot be called" not in str(exc):
                raise
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

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

    def get_pma_safety_checker(self) -> PmaSafetyChecker:
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

        safety_checker = self.get_pma_safety_checker()
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
            "Lifecycle event processed: event_id=%s type=%s repo_id=%s run_id=%s decision=%s processed=%s",
            event.event_id,
            event.event_type.value,
            event.repo_id,
            event.run_id,
            decision,
            processed,
        )

    def _snapshot_from_record(self, record: DiscoveryRecord) -> RepoSnapshot:
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
        return RepoSnapshot(
            id=record.repo.id,
            path=repo_path,
            display_name=record.repo.display_name or repo_path.name or record.repo.id,
            enabled=record.repo.enabled,
            auto_run=record.repo.auto_run,
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
            last_exit_code=runner_state.last_exit_code if runner_state else None,
            runner_pid=runner_state.runner_pid if runner_state else None,
        )

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
