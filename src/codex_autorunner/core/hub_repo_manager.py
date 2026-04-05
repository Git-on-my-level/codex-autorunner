from __future__ import annotations

import logging
import shutil
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
from .config import HubConfig
from .force_attestation import enforce_force_attestation
from .git_utils import (
    GitError,
    git_available,
    git_is_clean,
    git_upstream_status,
    run_git,
)

if TYPE_CHECKING:
    from .hub import RepoSnapshot

logger = logging.getLogger("codex_autorunner.hub_repo_manager")

_GIT_CLONE_TIMEOUT_SECONDS = 300


def _git_failure_detail(proc: Any) -> str:
    return (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"


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
            raise ValueError(f"git init failed: {_git_failure_detail(proc)}")

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
            raise ValueError(f"git clone failed: {_git_failure_detail(proc)}")
