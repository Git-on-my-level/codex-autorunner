import dataclasses
import enum
import shutil
import threading
import subprocess
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .bootstrap import seed_repo_files
from .config import HubConfig, load_config
from .discovery import DiscoveryRecord, discover_and_init
from .engine import Engine, LockError, clear_stale_lock
from .git_utils import git_available, git_is_clean, git_upstream_status
from .lock_utils import process_alive, read_lock_info
from .manifest import Manifest, ManifestRepo, load_manifest, save_manifest
from .state import RunnerState, load_state, now_iso
from .utils import atomic_write


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
    info = read_lock_info(lock_path)
    pid = info.pid
    if pid and process_alive(pid):
        return LockStatus.LOCKED_ALIVE
    return LockStatus.LOCKED_STALE


def load_hub_state(state_path: Path, hub_root: Path) -> HubState:
    if not state_path.exists():
        return HubState(last_scan_at=None, repos=[])
    data = state_path.read_text(encoding="utf-8")
    try:
        import json

        payload = json.loads(data)
    except Exception:
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
        except Exception:
            continue
    return HubState(last_scan_at=last_scan_at, repos=repos)


def save_hub_state(state_path: Path, state: HubState, hub_root: Path) -> None:
    payload = state.to_dict(hub_root)
    import json

    atomic_write(state_path, json.dumps(payload, indent=2) + "\n")


class RepoRunner:
    def __init__(self, repo_id: str, repo_root: Path):
        self.repo_id = repo_id
        self.engine = Engine(repo_root)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()

    @property
    def running(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        return self.engine.runner_pid() is not None

    def _start_thread(self, *, once: bool) -> None:
        stop_after_runs = 1 if once else None

        def _run() -> None:
            try:
                self.engine.run_loop(
                    stop_after_runs=stop_after_runs,
                    external_stop_flag=self._stop_flag,
                )
            finally:
                self.engine.release_lock()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def start(self, once: bool = False) -> None:
        with self._lock:
            lock_status = read_lock_status(self.engine.lock_path)
            if lock_status == LockStatus.LOCKED_ALIVE:
                raise LockError(
                    f"Repo {self.repo_id} is locked; use resume to clear stale locks"
                )
            clear_stale_lock(self.engine.lock_path)
            self.engine.clear_stop_request()
            self._stop_flag.clear()
            self.engine.acquire_lock(force=False)
            self._start_thread(once=once)

    def stop(self) -> None:
        with self._lock:
            self._stop_flag.set()
            self.engine.request_stop()

    def kill(self) -> Optional[int]:
        with self._lock:
            self._stop_flag.set()
            pid = self.engine.kill_running_process()
            return pid

    def resume(self, once: bool = False) -> None:
        with self._lock:
            clear_stale_lock(self.engine.lock_path)
            lock_status = read_lock_status(self.engine.lock_path)
            if lock_status == LockStatus.LOCKED_ALIVE:
                raise LockError(f"Repo {self.repo_id} is locked by a live process")
            self.engine.clear_stop_request()
            self._stop_flag.clear()
            self.engine.acquire_lock(force=False)
            self._start_thread(once=once)


class HubSupervisor:
    def __init__(self, hub_config: HubConfig):
        self.hub_config = hub_config
        self.state_path = hub_config.root / ".codex-autorunner" / "hub_state.json"
        self._runners: Dict[str, RepoRunner] = {}
        self.state = load_hub_state(self.state_path, self.hub_config.root)
        self._list_cache_at: Optional[float] = None
        self._list_cache: Optional[List[RepoSnapshot]] = None

    @classmethod
    def from_path(cls, path: Path) -> "HubSupervisor":
        config = load_config(path)
        if not isinstance(config, HubConfig):
            raise ValueError("HubSupervisor requires hub mode configuration")
        return cls(config)

    def scan(self) -> List[RepoSnapshot]:
        self._invalidate_list_cache()
        manifest, records = discover_and_init(self.hub_config)
        snapshots = self._build_snapshots(records)
        self.state = HubState(last_scan_at=now_iso(), repos=snapshots)
        save_hub_state(self.state_path, self.state, self.hub_config.root)
        return snapshots

    def list_repos(self, *, use_cache: bool = True) -> List[RepoSnapshot]:
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

    def run_repo(self, repo_id: str, once: bool = False) -> RepoSnapshot:
        runner = self._ensure_runner(repo_id)
        runner.start(once=once)
        return self._snapshot_for_repo(repo_id)

    def stop_repo(self, repo_id: str) -> RepoSnapshot:
        runner = self._ensure_runner(repo_id, allow_uninitialized=True)
        if runner:
            runner.stop()
        return self._snapshot_for_repo(repo_id)

    def resume_repo(self, repo_id: str, once: bool = False) -> RepoSnapshot:
        runner = self._ensure_runner(repo_id)
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

    def create_repo(
        self,
        repo_id: str,
        repo_path: Optional[Path] = None,
        git_init: bool = True,
        force: bool = False,
    ) -> RepoSnapshot:
        self._invalidate_list_cache()
        base_dir = self.hub_config.repos_root
        target = repo_path if repo_path is not None else Path(repo_id)
        if not target.is_absolute():
            target = (base_dir / target).resolve()
        else:
            target = target.resolve()

        try:
            target.relative_to(base_dir)
        except ValueError:
            raise ValueError(f"Repo path must live under repos_root ({base_dir})")

        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        existing = manifest.get(repo_id)
        if existing:
            existing_path = (self.hub_config.root / existing.path).resolve()
            if existing_path != target:
                raise ValueError(
                    f"Repo id {repo_id} already exists at {existing.path}; choose a different id"
                )

        if target.exists() and not force:
            raise ValueError(f"Repo path already exists: {target}")

        target.mkdir(parents=True, exist_ok=True)

        if git_init and not (target / ".git").exists():
            subprocess.run(["git", "init"], cwd=target, check=False)
        if git_init and not (target / ".git").exists():
            raise ValueError(f"git init failed for {target}")

        seed_repo_files(target, force=force)
        manifest.ensure_repo(
            self.hub_config.root, target, repo_id=repo_id, kind="base"
        )
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)

        return self._snapshot_for_repo(repo_id)

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
        inferred_id = (repo_id or "").strip() or _repo_id_from_url(git_url)
        if not inferred_id:
            raise ValueError("Unable to infer repo id from git_url")
        base_dir = self.hub_config.repos_root
        target = repo_path if repo_path is not None else Path(inferred_id)
        if not target.is_absolute():
            target = (base_dir / target).resolve()
        else:
            target = target.resolve()

        try:
            target.relative_to(base_dir)
        except ValueError:
            raise ValueError(f"Repo path must live under repos_root ({base_dir})")

        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        existing = manifest.get(inferred_id)
        if existing:
            existing_path = (self.hub_config.root / existing.path).resolve()
            if existing_path != target:
                raise ValueError(
                    f"Repo id {inferred_id} already exists at {existing.path}; choose a different id"
                )

        if target.exists() and not force:
            raise ValueError(f"Repo path already exists: {target}")

        proc = subprocess.run(
            ["git", "clone", git_url, str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = (
                proc.stderr or proc.stdout or ""
            ).strip() or f"exit {proc.returncode}"
            raise ValueError(f"git clone failed: {detail}")

        seed_repo_files(target, force=False, git_required=False)
        manifest.ensure_repo(
            self.hub_config.root, target, repo_id=inferred_id, kind="base"
        )
        save_manifest(self.hub_config.manifest_path, manifest, self.hub_config.root)
        return self._snapshot_for_repo(inferred_id)

    def create_worktree(
        self,
        *,
        base_repo_id: str,
        branch: str,
        force: bool = False,
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
        safe_branch = re.sub(r"[^a-zA-Z0-9._/-]+", "-", branch).strip("-") or "work"
        repo_id = f"{base_repo_id}--{safe_branch.replace('/', '-')}"
        if manifest.get(repo_id) and not force:
            raise ValueError(f"Worktree repo already exists: {repo_id}")
        worktree_path = (self.hub_config.worktrees_root / repo_id).resolve()
        if worktree_path.exists() and not force:
            raise ValueError(f"Worktree path already exists: {worktree_path}")

        # Create the worktree (branch may or may not exist locally).
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=base_path,
            check=False,
            capture_output=True,
            text=True,
        )
        if exists.returncode == 0:
            proc = subprocess.run(
                ["git", "worktree", "add", str(worktree_path), branch],
                cwd=base_path,
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            proc = subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(worktree_path)],
                cwd=base_path,
                check=False,
                capture_output=True,
                text=True,
            )
        if proc.returncode != 0:
            detail = (
                proc.stderr or proc.stdout or ""
            ).strip() or f"exit {proc.returncode}"
            raise ValueError(f"git worktree add failed: {detail}")

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

        # Remove worktree from base repo.
        proc = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=base_path,
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = (
                proc.stderr or proc.stdout or ""
            ).strip() or f"exit {proc.returncode}"
            detail_lower = detail.lower()
            # If the worktree is already gone (deleted via UI/Hub), continue cleanup.
            if "not a working tree" not in detail_lower:
                raise ValueError(f"git worktree remove failed: {detail}")
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=base_path,
            check=False,
            capture_output=True,
            text=True,
        )

        if delete_branch and entry.branch:
            subprocess.run(
                ["git", "branch", "-D", entry.branch],
                cwd=base_path,
                check=False,
                capture_output=True,
                text=True,
            )
        if delete_remote and entry.branch:
            subprocess.run(
                ["git", "push", "origin", "--delete", entry.branch],
                cwd=base_path,
                check=False,
                capture_output=True,
                text=True,
            )

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
            manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
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
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        if not allow_uninitialized and not config_path.exists():
            raise ValueError(f"Repo {repo_id} is not initialized")
        if not config_path.exists():
            return None
        runner = RepoRunner(repo_id, repo_root)
        self._runners[repo_id] = runner
        return runner

    def _manifest_records(
        self, manifest_only: bool = False
    ) -> Tuple[Manifest, List[DiscoveryRecord]]:
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        records: List[DiscoveryRecord] = []
        for entry in manifest.repos:
            repo_path = (self.hub_config.root / entry.path).resolve()
            initialized = (repo_path / ".codex-autorunner" / "config.yml").exists()
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
        self._list_cache = None
        self._list_cache_at = None

    def _snapshot_from_record(self, record: DiscoveryRecord) -> RepoSnapshot:
        repo_path = record.absolute_path
        lock_path = repo_path / ".codex-autorunner" / "lock"
        lock_status = read_lock_status(lock_path)

        runner_state: Optional[RunnerState] = None
        state_path = repo_path / ".codex-autorunner" / "state.json"
        if record.initialized and state_path.exists():
            runner_state = load_state(state_path)

        status = self._derive_status(record, lock_status, runner_state)
        last_run_id = runner_state.last_run_id if runner_state else None
        return RepoSnapshot(
            id=record.repo.id,
            path=repo_path,
            display_name=repo_path.name,
            enabled=record.repo.enabled,
            auto_run=record.repo.auto_run,
            kind=record.repo.kind,
            worktree_of=record.repo.worktree_of,
            branch=record.repo.branch,
            exists_on_disk=record.exists_on_disk,
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
