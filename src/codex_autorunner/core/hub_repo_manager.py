from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional

from ..bootstrap import seed_repo_files
from ..manifest import (
    Manifest,
    ensure_unique_repo_id,
    load_manifest,
    sanitize_repo_id,
    save_manifest,
)
from .archive import (
    archive_workspace_for_fresh_start,
    dirty_car_state_paths,
)
from .archive_retention import resolve_worktree_archive_retention_policy
from .config import HubConfig
from .force_attestation import enforce_force_attestation
from .git_utils import (
    GitError,
    git_available,
    git_branch,
    git_default_branch,
    git_failure_detail,
    git_head_sha,
    git_is_clean,
    git_mutation_lock,
    git_upstream_status,
    resolve_ref_sha,
    run_git,
)
from .orchestration.sqlite import open_orchestration_sqlite
from .pma_thread_store import PmaThreadStore

if TYPE_CHECKING:
    from .hub import RepoSnapshot

logger = logging.getLogger("codex_autorunner.hub_repo_manager")

_GIT_CLONE_TIMEOUT_SECONDS = 300
_GIT_FETCH_TIMEOUT_SECONDS = 120
_GIT_PULL_TIMEOUT_SECONDS = 120


def _repo_id_from_url(url: str) -> str:
    name = (url or "").rstrip("/").split("/")[-1]
    if ":" in name:
        name = name.split(":")[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name.strip()


class RepoManager:
    def __init__(
        self,
        hub_config: HubConfig,
        *,
        on_invalidate_cache: Callable[[], None],
        on_snapshot_for_repo: Callable[[str], RepoSnapshot],
        on_stop_runner: Optional[Callable[..., None]] = None,
        on_cleanup_worktree: Optional[Callable[..., Dict[str, object]]] = None,
        on_list_repos: Optional[Callable[..., List[RepoSnapshot]]] = None,
        runners: Optional[Dict[str, Any]] = None,
    ):
        self._hub_config = hub_config
        self._on_invalidate_cache = on_invalidate_cache
        self._on_snapshot_for_repo = on_snapshot_for_repo
        self._on_stop_runner = on_stop_runner
        self._on_cleanup_worktree = on_cleanup_worktree
        self._on_list_repos = on_list_repos
        self._runners = runners

    def init_repo(self, repo_id: str) -> RepoSnapshot:
        self._on_invalidate_cache()
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repo_path = (self._hub_config.root / repo.path).resolve()
        if not repo_path.exists():
            raise ValueError(f"Repo {repo_id} missing on disk")
        seed_repo_files(repo_path, force=False, git_required=False)
        return self._on_snapshot_for_repo(repo_id)

    def create_repo(
        self,
        repo_id: str,
        repo_path: Optional[Path] = None,
        git_init: bool = True,
        force: bool = False,
    ) -> RepoSnapshot:
        safe_repo_id = sanitize_repo_id(repo_id)
        display_name = repo_id
        target = self._resolve_target_path(safe_repo_id, repo_path)

        def setup_fn(t: Path, f: bool) -> None:
            t.mkdir(parents=True, exist_ok=True)
            if git_init and not (t / ".git").exists():
                self._git_init(t)
            if git_init and not (t / ".git").exists():
                raise ValueError(f"git init failed for {t}")

        return self._provision_repo(
            safe_repo_id=safe_repo_id,
            display_name=display_name,
            target=target,
            force=force,
            setup_fn=setup_fn,
            seed_force=force,
            seed_git_required=True,
        )

    def clone_repo(
        self,
        *,
        git_url: str,
        repo_id: Optional[str] = None,
        repo_path: Optional[Path] = None,
        force: bool = False,
    ) -> RepoSnapshot:
        git_url = (git_url or "").strip()
        if not git_url:
            raise ValueError("git_url is required")
        inferred_name = (repo_id or "").strip() or _repo_id_from_url(git_url)
        if not inferred_name:
            raise ValueError("Unable to infer repo id from git_url")
        safe_repo_id = sanitize_repo_id(inferred_name)
        display_name = inferred_name
        target = self._resolve_target_path(safe_repo_id, repo_path)

        def setup_fn(t: Path, f: bool) -> None:
            t.parent.mkdir(parents=True, exist_ok=True)
            self._git_clone(git_url, t)

        return self._provision_repo(
            safe_repo_id=safe_repo_id,
            display_name=display_name,
            target=target,
            force=force,
            setup_fn=setup_fn,
            seed_force=False,
            seed_git_required=False,
        )

    def remove_repo(
        self,
        repo_id: str,
        *,
        force: bool = False,
        delete_dir: bool = True,
        delete_worktrees: bool = False,
        force_attestation: Optional[Mapping[str, object]] = None,
    ) -> None:
        self._on_invalidate_cache()
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
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
            self._delegate_cleanup_worktree(
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
                self._delegate_cleanup_worktree(
                    worktree_repo_id=worktree.id,
                    force=force,
                    force_attestation=force_attestation,
                )
            manifest = load_manifest(
                self._hub_config.manifest_path, self._hub_config.root
            )
            repo = manifest.get(repo_id)
            if not repo:
                raise ValueError(f"Repo {repo_id} missing after worktree cleanup")

        repo_root = (self._hub_config.root / repo.path).resolve()
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

        if self._on_stop_runner is not None:
            self._on_stop_runner(
                repo_id=repo_id,
                repo_path=repo_root,
            )
        if self._runners is not None:
            self._runners.pop(repo_id, None)

        if delete_dir and repo_root.exists():
            shutil.rmtree(repo_root)

        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        manifest.repos = [r for r in manifest.repos if r.id != repo_id]
        save_manifest(self._hub_config.manifest_path, manifest, self._hub_config.root)
        if self._on_list_repos is not None:
            self._on_list_repos(use_cache=False)

    def sync_main(self, repo_id: str) -> RepoSnapshot:
        self._on_invalidate_cache()
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repo_root = (self._hub_config.root / repo.path).resolve()
        if not repo_root.exists():
            raise ValueError(f"Repo {repo_id} missing on disk")
        if not git_available(repo_root):
            raise ValueError(f"Repo {repo_id} is not a git repository")
        if not git_is_clean(repo_root):
            raise ValueError("Repo has uncommitted changes; commit or stash first")

        with git_mutation_lock(repo_root):
            try:
                proc = run_git(
                    ["fetch", "--prune", "origin"],
                    repo_root,
                    check=False,
                    timeout_seconds=_GIT_FETCH_TIMEOUT_SECONDS,
                )
            except GitError as exc:
                raise ValueError(f"git fetch failed: {exc}") from exc
            if proc.returncode != 0:
                raise ValueError(f"git fetch failed: {git_failure_detail(proc)}")

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
                    raise ValueError(f"git checkout failed: {git_failure_detail(proc)}")

            try:
                proc = run_git(
                    ["pull", "--ff-only", "origin", default_branch],
                    repo_root,
                    check=False,
                    timeout_seconds=_GIT_PULL_TIMEOUT_SECONDS,
                )
            except GitError as exc:
                raise ValueError(f"git pull failed: {exc}") from exc
            if proc.returncode != 0:
                raise ValueError(f"git pull failed: {git_failure_detail(proc)}")
            local_sha = git_head_sha(repo_root)
            if not local_sha:
                raise ValueError("Unable to resolve local HEAD after sync")
            origin_ref = f"refs/remotes/origin/{default_branch}"
            origin_sha = resolve_ref_sha(repo_root, origin_ref)
            if local_sha != origin_sha:
                raise ValueError(
                    "Sync main did not land on origin/%s: local=%s origin=%s. "
                    "Local branch may contain extra commits; resolve divergence first."
                    % (default_branch, local_sha[:12], origin_sha[:12])
                )
        return self._on_snapshot_for_repo(repo_id)

    def archive_repo_state(
        self,
        *,
        repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        entry = manifest.get(repo_id)
        if not entry:
            raise ValueError(f"Repo not found: {repo_id}")

        repo_path = (self._hub_config.root / entry.path).resolve()
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
            base_path = (self._hub_config.root / base.path).resolve()
            base_repo_id = base.id

        has_car_state = bool(dirty_car_state_paths(repo_path))
        if has_car_state:
            if self._on_stop_runner is not None:
                self._on_stop_runner(
                    repo_id=repo_id,
                    repo_path=repo_path,
                )

        branch_name = entry.branch or git_branch(repo_path) or "unknown"
        result = archive_workspace_for_fresh_start(
            hub_root=self._hub_config.root,
            base_repo_root=base_path,
            base_repo_id=base_repo_id,
            worktree_repo_root=repo_path,
            worktree_repo_id=repo_id,
            branch=branch_name,
            worktree_of=worktree_of,
            note=archive_note,
            source_path=entry.path,
            retention_policy=resolve_worktree_archive_retention_policy(
                self._hub_config.pma
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

    def cleanup_repo_threads(self, *, repo_id: str) -> Dict[str, object]:
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        entry = manifest.get(repo_id)
        if not entry or entry.kind != "base":
            raise ValueError(f"Base repo not found: {repo_id}")

        repo_path = (self._hub_config.root / entry.path).resolve()
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
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
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
                except (GitError, OSError):
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

    def check_repo_removal(self, repo_id: str) -> Dict[str, object]:
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repo_root = (self._hub_config.root / repo.path).resolve()
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

    def unbound_repo_thread_counts(self) -> dict[str, int]:
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        thread_ids_by_repo = self._collect_unbound_repo_threads(manifest=manifest)
        return {
            repo_id: len(thread_ids)
            for repo_id, thread_ids in thread_ids_by_repo.items()
            if thread_ids
        }

    def _base_repo_paths(self, manifest: Manifest) -> dict[str, Path]:
        base_repo_paths: dict[str, Path] = {}
        for entry in manifest.repos:
            if entry.kind != "base":
                continue
            base_repo_paths[entry.id] = (self._hub_config.root / entry.path).resolve()
        return base_repo_paths

    def _bound_thread_target_ids(self) -> set[str]:
        try:
            with open_orchestration_sqlite(self._hub_config.root) as conn:
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

    def _collect_unbound_repo_threads(
        self,
        *,
        manifest: Optional[Manifest] = None,
    ) -> dict[str, list[str]]:
        if manifest is None:
            manifest = load_manifest(
                self._hub_config.manifest_path,
                self._hub_config.root,
            )
        base_repo_paths = self._base_repo_paths(manifest)
        if not base_repo_paths:
            return {}

        store = PmaThreadStore(self._hub_config.root)
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
                except OSError:
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

        store = PmaThreadStore(self._hub_config.root)
        archived_thread_ids: list[str] = []
        for managed_thread_id in thread_ids:
            store.archive_thread(managed_thread_id)
            archived_thread_ids.append(managed_thread_id)
        return archived_thread_ids

    def _provision_repo(
        self,
        *,
        safe_repo_id: str,
        display_name: str,
        target: Path,
        force: bool,
        setup_fn: Callable[[Path, bool], None],
        seed_force: bool,
        seed_git_required: bool,
    ) -> RepoSnapshot:
        self._on_invalidate_cache()
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        self._validate_no_id_conflict(manifest, safe_repo_id, target)
        if target.exists() and not force:
            raise ValueError(f"Repo path already exists: {target}")
        setup_fn(target, force)
        seed_repo_files(target, force=seed_force, git_required=seed_git_required)
        safe_repo_id = self._ensure_unique_id(manifest, safe_repo_id)
        self._register_repo(manifest, target, safe_repo_id, display_name)
        return self._on_snapshot_for_repo(safe_repo_id)

    def _resolve_target_path(
        self, safe_repo_id: str, repo_path: Optional[Path]
    ) -> Path:
        base_dir = self._hub_config.repos_root
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
        return target

    def _validate_no_id_conflict(
        self, manifest: Manifest, safe_repo_id: str, target: Path
    ) -> None:
        existing = manifest.get(safe_repo_id)
        if existing:
            existing_path = (self._hub_config.root / existing.path).resolve()
            if existing_path != target:
                raise ValueError(
                    f"Repo id {safe_repo_id} already exists at {existing.path}; choose a different id"
                )

    def _ensure_unique_id(self, manifest: Manifest, safe_repo_id: str) -> str:
        existing_ids = {repo.id for repo in manifest.repos}
        existing = manifest.get(safe_repo_id)
        if safe_repo_id in existing_ids and not existing:
            return ensure_unique_repo_id(safe_repo_id, existing_ids)
        return safe_repo_id

    def _register_repo(
        self,
        manifest: Manifest,
        target: Path,
        repo_id: str,
        display_name: str,
    ) -> None:
        manifest.ensure_repo(
            self._hub_config.root,
            target,
            repo_id=repo_id,
            display_name=display_name,
            kind="base",
        )
        save_manifest(self._hub_config.manifest_path, manifest, self._hub_config.root)

    def _delegate_cleanup_worktree(
        self,
        *,
        worktree_repo_id: str,
        force: bool,
        force_attestation: Optional[Mapping[str, object]],
    ) -> None:
        if self._on_cleanup_worktree is None:
            raise RuntimeError("cleanup_worktree callback not configured")
        self._on_cleanup_worktree(
            worktree_repo_id=worktree_repo_id,
            force=force,
            force_attestation=force_attestation,
        )

    def _git_init(self, target: Path) -> None:
        try:
            proc = run_git(["init"], target, check=False)
        except GitError as exc:
            raise ValueError(f"git init failed: {exc}") from exc
        if proc.returncode != 0:
            raise ValueError(f"git init failed: {git_failure_detail(proc)}")

    def _git_clone(self, git_url: str, target: Path) -> None:
        try:
            proc = run_git(
                ["clone", git_url, str(target)],
                target.parent,
                check=False,
                timeout_seconds=_GIT_CLONE_TIMEOUT_SECONDS,
            )
        except GitError as exc:
            raise ValueError(f"git clone failed: {exc}") from exc
        if proc.returncode != 0:
            raise ValueError(f"git clone failed: {git_failure_detail(proc)}")
