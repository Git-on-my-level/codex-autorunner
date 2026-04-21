from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    cast,
)

from ..bootstrap import seed_repo_files
from ..manifest import (
    Manifest,
    load_manifest,
    match_base_repo_id,
    save_manifest,
)
from .archive import (
    ArchiveProfile,
    archive_workspace_managed_threads,
    archive_worktree_snapshot,
    build_snapshot_id,
    resolve_worktree_archive_intent,
)
from .archive_retention import resolve_worktree_archive_retention_policy
from .chat_bindings import repo_has_active_non_pma_chat_binding
from .config import HubConfig
from .destinations import (
    DockerDestination,
    default_car_docker_container_name,
    resolve_effective_repo_destination,
)
from .flows import FlowStore
from .flows.archive_helpers import archive_terminal_flow_runs
from .force_attestation import enforce_force_attestation
from .git_utils import (
    GitError,
    git_available,
    git_branch,
    git_default_branch,
    git_failure_detail,
    git_head_sha,
    git_is_clean,
    git_linked_worktree_base_root,
    git_mutation_lock,
    resolve_ref_sha,
    run_git,
)
from .hub_worktree_lifecycle import (
    ResolvedWorktreeEntry,
    WorktreeCleanupReport,
    WorktreeHubContext,
)
from .state import now_iso
from .utils import is_within, subprocess_env

if TYPE_CHECKING:
    from .hub import RepoSnapshot

logger = logging.getLogger("codex_autorunner.hub_worktree_manager")

_GIT_FETCH_TIMEOUT_SECONDS = 120
_GIT_WORKTREE_TIMEOUT_SECONDS = 120
_GIT_PUSH_DELETE_TIMEOUT_SECONDS = 120
_DOCKER_INSPECT_TIMEOUT_SECONDS = 15
_DOCKER_STOP_TIMEOUT_SECONDS = 15
_DOCKER_RM_TIMEOUT_SECONDS = 30
_WORKTREE_SETUP_COMMAND_TIMEOUT_SECONDS = 600


class WorktreeManager:
    def __init__(
        self,
        hub_config: HubConfig,
        *,
        ctx: WorktreeHubContext,
    ):
        self._hub_config = hub_config
        self._ctx = ctx

    def create_worktree(
        self,
        *,
        base_repo_id: str,
        branch: str,
        force: bool = False,
        start_point: Optional[str] = None,
    ) -> RepoSnapshot:
        self._ctx.invalidate_cache()
        branch = (branch or "").strip()
        if not branch:
            raise ValueError("branch is required")

        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        base = manifest.get(base_repo_id)
        if not base or base.kind != "base":
            raise ValueError(f"Base repo not found: {base_repo_id}")
        base_path = (self._hub_config.root / base.path).resolve()
        if not base_path.exists():
            raise ValueError(f"Base repo missing on disk: {base_repo_id}")

        self._hub_config.worktrees_root.mkdir(parents=True, exist_ok=True)
        worktrees_root = self._hub_config.worktrees_root.resolve()
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

        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        explicit_start_ref = (
            start_point.strip() if start_point and start_point.strip() else None
        )
        effective_start_ref = explicit_start_ref

        with git_mutation_lock(base_path):
            if explicit_start_ref is None or explicit_start_ref.startswith("origin/"):
                try:
                    fetch_proc = run_git(
                        ["fetch", "--prune", "origin"],
                        base_path,
                        check=False,
                        timeout_seconds=_GIT_FETCH_TIMEOUT_SECONDS,
                    )
                except GitError as exc:
                    raise ValueError(
                        "Unable to refresh origin before creating worktree: %s" % exc
                    ) from exc
                if fetch_proc.returncode != 0:
                    raise ValueError(
                        "Unable to refresh origin before creating worktree: %s"
                        % git_failure_detail(fetch_proc)
                    )

            if effective_start_ref is None:
                default_branch = git_default_branch(base_path)
                if not default_branch:
                    raise ValueError("Unable to resolve origin default branch")
                effective_start_ref = f"origin/{default_branch}"

            assert effective_start_ref is not None
            start_sha = resolve_ref_sha(base_path, effective_start_ref)
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
                    branch_sha = resolve_ref_sha(base_path, f"refs/heads/{branch}")
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
                        timeout_seconds=_GIT_WORKTREE_TIMEOUT_SECONDS,
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
                        timeout_seconds=_GIT_WORKTREE_TIMEOUT_SECONDS,
                    )
            except GitError as exc:
                raise ValueError(f"git worktree add failed: {exc}") from exc
            if proc.returncode != 0:
                raise ValueError(f"git worktree add failed: {git_failure_detail(proc)}")

        seed_repo_files(worktree_path, force=force, git_required=False)
        manifest.ensure_repo(
            self._hub_config.root,
            worktree_path,
            repo_id=repo_id,
            kind="worktree",
            worktree_of=base_repo_id,
            branch=branch,
        )
        save_manifest(self._hub_config.manifest_path, manifest, self._hub_config.root)
        self._run_worktree_setup_commands(
            worktree_path, base.worktree_setup_commands, base_repo_id=base_repo_id
        )
        return self._ctx.snapshot_for_repo(repo_id)

    def set_worktree_setup_commands(
        self, repo_id: str, commands: List[str]
    ) -> RepoSnapshot:
        self._ctx.invalidate_cache()
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        entry = manifest.get(repo_id)
        if not entry:
            raise ValueError(f"Repo not found: {repo_id}")
        if entry.kind != "base":
            raise ValueError(
                "Worktree setup commands can only be configured on base repos"
            )
        normalized = [str(cmd).strip() for cmd in commands if str(cmd).strip()]
        entry.worktree_setup_commands = normalized or None
        save_manifest(self._hub_config.manifest_path, manifest, self._hub_config.root)
        return self._ctx.snapshot_for_repo(repo_id)

    def run_setup_commands_for_workspace(
        self,
        workspace_path: Path,
        *,
        repo_id_hint: Optional[str] = None,
    ) -> int:
        workspace_root = workspace_path.expanduser().resolve()
        snapshots = self._ctx.list_repos(use_cache=False)
        snapshots_by_id = {snapshot.id: snapshot for snapshot in snapshots}
        target: Optional[RepoSnapshot] = None

        for snapshot in snapshots:
            try:
                if snapshot.path.expanduser().resolve() == workspace_root:
                    target = snapshot
                    break
            except OSError:
                continue

        if target is None:
            hint = (repo_id_hint or "").strip()
            if hint:
                target = snapshots_by_id.get(hint)

        if target is None:
            return 0

        try:
            execution_root = target.path.expanduser().resolve()
        except OSError:
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
        resolved: ResolvedWorktreeEntry,
        *,
        archive_note: Optional[str] = None,
        force: bool = False,
        archive_profile: Optional[str] = None,
        cleanup: bool = False,
    ):
        from .archive import ArchiveResult

        worktree_path = resolved.worktree_path
        if not worktree_path.exists():
            raise ValueError(f"Worktree path does not exist: {worktree_path}")

        branch_name = resolved.entry.branch or git_branch(worktree_path) or "unknown"
        head_sha = git_head_sha(worktree_path) or "unknown"
        snapshot_id = build_snapshot_id(branch_name, head_sha)
        logger.info(
            "Hub archive worktree start id=%s snapshot_id=%s",
            resolved.entry.id,
            snapshot_id,
        )
        profile = cast(
            ArchiveProfile,
            archive_profile or self._hub_config.pma.worktree_archive_profile,
        )
        intent = resolve_worktree_archive_intent(profile=profile, cleanup=cleanup)
        retention_policy = resolve_worktree_archive_retention_policy(
            self._hub_config.pma
        )
        try:
            result: ArchiveResult = archive_worktree_snapshot(
                base_repo_root=resolved.base_path,
                base_repo_id=resolved.base.id,
                worktree_repo_root=worktree_path,
                worktree_repo_id=resolved.entry.id,
                branch=branch_name,
                worktree_of=resolved.entry.worktree_of,
                note=archive_note,
                snapshot_id=snapshot_id,
                head_sha=head_sha,
                source_path=resolved.entry.path,
                intent=intent,
                retention_policy=retention_policy,
            )
        except (
            Exception
        ) as exc:  # intentional: archive_worktree_snapshot spans file I/O, git, and compression with unpredictable failure modes
            logger.exception(
                "Hub archive worktree failed id=%s snapshot_id=%s",
                resolved.entry.id,
                snapshot_id,
            )
            if not force:
                raise ValueError(f"Worktree archive failed: {exc}") from exc
            return None
        else:
            logger.info(
                "Hub archive worktree complete id=%s snapshot_id=%s status=%s",
                resolved.entry.id,
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
                hub_root=self._hub_config.root,
                worktree_repo_id=worktree_repo_id,
                worktree_path=worktree_path,
            )
        )

    def _ensure_worktree_clean_for_archive(
        self, *, worktree_repo_id: str, worktree_path: Path
    ) -> None:
        if not worktree_path.exists():
            return
        if git_available(worktree_path) and not git_is_clean(worktree_path):
            raise ValueError(
                f"Worktree {worktree_repo_id} has uncommitted changes; commit or stash before archiving"
            )

    def _resolve_worktree_entry(self, worktree_repo_id: str) -> ResolvedWorktreeEntry:
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        entry = manifest.get(worktree_repo_id)
        if not entry or entry.kind != "worktree":
            raise ValueError(f"Worktree repo not found: {worktree_repo_id}")
        if not entry.worktree_of:
            raise ValueError("Worktree repo is missing worktree_of metadata")
        base = manifest.get(entry.worktree_of)
        if not base or base.kind != "base":
            raise ValueError(f"Base repo not found: {entry.worktree_of}")
        base_path = (self._hub_config.root / base.path).resolve()
        worktree_path = (self._hub_config.root / entry.path).resolve()
        return ResolvedWorktreeEntry(
            manifest=manifest,
            entry=entry,
            base=base,
            base_path=base_path,
            worktree_path=worktree_path,
        )

    def _run_telemetry_housekeeping(
        self,
        *,
        worktree_repo_id: str,
        worktree_path: Path,
        report: Optional[WorktreeCleanupReport] = None,
    ) -> None:
        try:
            from .flows.flow_telemetry_hooks import housekeep_on_worktree_cleanup

            housekeep_on_worktree_cleanup(worktree_path)
            if report is not None:
                report.add_step("telemetry_housekeep", "ok")
        except Exception as exc:
            logger.warning(
                "Worktree telemetry housekeeping failed for %s: %s",
                worktree_repo_id,
                exc,
            )
            if report is not None:
                report.add_step("telemetry_housekeep", "error", detail=str(exc))

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
                timeout_seconds=_DOCKER_INSPECT_TIMEOUT_SECONDS,
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
                    timeout_seconds=_DOCKER_STOP_TIMEOUT_SECONDS,
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
                timeout_seconds=_DOCKER_RM_TIMEOUT_SECONDS,
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

    def _has_active_chat_binding(self, repo_id: str) -> bool:
        return repo_has_active_non_pma_chat_binding(
            hub_root=self._hub_config.root,
            raw_config=self._hub_config.raw,
            repo_id=repo_id,
        )

    def _infer_worktree_metadata(
        self,
        *,
        manifest: Manifest,
        display_name: str,
        worktree_path: Path,
        existing_branch: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        base_path = git_linked_worktree_base_root(worktree_path)
        base_name: Optional[str] = None
        if base_path is not None:
            base_name = base_path.name
        elif "--" in display_name:
            base_name = display_name.split("--", 1)[0] or None

        worktree_of = None
        if base_name:
            worktree_of = match_base_repo_id(
                manifest,
                self._hub_config.root,
                base_name,
                base_path=base_path,
            )

        branch = existing_branch or git_branch(worktree_path)
        if not branch and "--" in display_name:
            _, branch_suffix = display_name.split("--", 1)
            branch = branch_suffix or None
        return worktree_of, branch

    def _normalize_manifest_worktree_entries(self, manifest: Manifest) -> bool:
        changed = False
        for entry in manifest.repos:
            repo_path = (self._hub_config.root / entry.path).resolve()
            if not repo_path.exists():
                continue
            base_path = git_linked_worktree_base_root(repo_path)
            if base_path is None and entry.kind != "worktree":
                continue

            display_name = entry.display_name or entry.id or repo_path.name
            worktree_of, branch = self._infer_worktree_metadata(
                manifest=manifest,
                display_name=display_name,
                worktree_path=repo_path,
                existing_branch=entry.branch,
            )
            if entry.kind != "worktree":
                entry.kind = "worktree"
                changed = True
            if worktree_of and entry.worktree_of != worktree_of:
                entry.worktree_of = worktree_of
                changed = True
            if branch and entry.branch != branch:
                entry.branch = branch
                changed = True
        return changed

    def _inspect_orphaned_worktree_dir(self, worktree_path: Path) -> dict[str, object]:
        if not worktree_path.exists():
            return {
                "status": "missing",
                "message": "worktree directory already missing",
            }
        if not worktree_path.is_dir():
            return {
                "status": "safe_to_remove",
                "message": "worktree path is not a directory",
            }

        unexpected_entries: list[str] = []
        for child in sorted(worktree_path.iterdir(), key=lambda item: item.name):
            if child.name in {".codex-autorunner", ".git"}:
                continue
            unexpected_entries.append(child.name)

        if unexpected_entries:
            rendered = ", ".join(unexpected_entries[:5])
            if len(unexpected_entries) > 5:
                rendered = f"{rendered}, ..."
            return {
                "status": "blocked_non_car_files",
                "message": (
                    "orphaned worktree directory still has non-CAR files: "
                    f"{rendered}"
                ),
            }
        return {
            "status": "safe_to_remove",
            "message": "orphaned worktree directory only contains CAR metadata",
        }

    def _remove_orphaned_worktree_dir(self, worktree_path: Path) -> dict[str, object]:
        inspection = self._inspect_orphaned_worktree_dir(worktree_path)
        if inspection["status"] != "safe_to_remove":
            return inspection
        if worktree_path.exists():
            if worktree_path.is_dir():
                shutil.rmtree(worktree_path)
            else:
                worktree_path.unlink()
        return {
            "status": "removed",
            "message": "removed orphaned worktree directory",
        }

    def _cleanup_orphaned_worktree_entry(
        self,
        *,
        entry,
        dry_run: bool,
    ) -> dict[str, str]:
        worktree_path = (self._hub_config.root / entry.path).resolve()
        branch_name = entry.branch
        if (
            branch_name is None
            and worktree_path.exists()
            and git_available(worktree_path)
        ):
            branch_name = git_branch(worktree_path)
        branch_name = branch_name or "unknown"

        inspection = self._inspect_orphaned_worktree_dir(worktree_path)
        if inspection["status"] == "blocked_non_car_files":
            raise ValueError(str(inspection["message"]))
        if dry_run:
            return {"id": entry.id, "branch": branch_name}

        if worktree_path.exists():
            self._ctx.stop_runner(repo_id=entry.id, repo_path=worktree_path)
        self._archive_bound_pma_threads(
            worktree_repo_id=entry.id,
            worktree_path=worktree_path,
        )
        self._remove_orphaned_worktree_dir(worktree_path)

        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        manifest.repos = [repo for repo in manifest.repos if repo.id != entry.id]
        save_manifest(self._hub_config.manifest_path, manifest, self._hub_config.root)
        return {"id": entry.id, "branch": branch_name}

    def _remove_worktree_git_refs(
        self,
        *,
        worktree_path: Path,
        base_path: Path,
        branch: Optional[str],
        delete_branch: bool = False,
        delete_remote: bool = False,
    ) -> None:
        try:
            proc = run_git(
                ["worktree", "remove", "--force", str(worktree_path)],
                base_path,
                check=False,
                timeout_seconds=_GIT_WORKTREE_TIMEOUT_SECONDS,
            )
        except GitError as exc:
            raise ValueError(f"git worktree remove failed: {exc}") from exc
        if proc.returncode != 0:
            detail = git_failure_detail(proc)
            detail_lower = detail.lower()
            if "not a working tree" not in detail_lower:
                raise ValueError(f"git worktree remove failed: {detail}")
        try:
            proc = run_git(["worktree", "prune"], base_path, check=False)
            if proc.returncode != 0:
                logger.warning(
                    "git worktree prune failed: %s", git_failure_detail(proc)
                )
        except GitError as exc:
            logger.warning("git worktree prune failed: %s", exc)

        if delete_branch and branch:
            try:
                proc = run_git(["branch", "-D", branch], base_path, check=False)
                if proc.returncode != 0:
                    logger.warning(
                        "git branch delete failed: %s", git_failure_detail(proc)
                    )
            except GitError as exc:
                logger.warning("git branch delete failed: %s", exc)
        if delete_remote and branch:
            try:
                proc = run_git(
                    ["push", "origin", "--delete", branch],
                    base_path,
                    check=False,
                    timeout_seconds=_GIT_PUSH_DELETE_TIMEOUT_SECONDS,
                )
                if proc.returncode != 0:
                    logger.warning(
                        "git push delete failed: %s", git_failure_detail(proc)
                    )
            except GitError as exc:
                logger.warning("git push delete failed: %s", exc)

    def _validate_cleanup_worktree(
        self,
        *,
        worktree_repo_id: str,
        archive: bool,
        force: bool,
        force_archive: bool,
        force_attestation: Optional[Mapping[str, object]],
    ) -> ResolvedWorktreeEntry:
        if self._hub_config.pma.cleanup_require_archive and not archive:
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
        self._ctx.invalidate_cache()
        resolved = self._resolve_worktree_entry(worktree_repo_id)
        branch_name = resolved.entry.branch or "unknown"
        try:
            has_active_chat_binding = self._has_active_chat_binding(worktree_repo_id)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
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
        return resolved

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
        report = WorktreeCleanupReport()
        resolved = self._validate_cleanup_worktree(
            worktree_repo_id=worktree_repo_id,
            archive=archive,
            force=force,
            force_archive=force_archive,
            force_attestation=force_attestation,
        )
        worktree_path = resolved.worktree_path
        report.add_step("validate", "ok")

        self._ctx.stop_runner(
            repo_id=worktree_repo_id,
            repo_path=worktree_path,
        )
        report.add_step("stop_runner", "ok")

        self._run_telemetry_housekeeping(
            worktree_repo_id=worktree_repo_id,
            worktree_path=worktree_path,
            report=report,
        )

        if archive:
            self._ensure_worktree_clean_for_archive(
                worktree_repo_id=worktree_repo_id,
                worktree_path=worktree_path,
            )
            self._archive_worktree_snapshot(
                resolved,
                archive_note=archive_note,
                force=force_archive,
                archive_profile=archive_profile,
                cleanup=True,
            )
            report.add_step("archive_snapshot", "ok")

        repos_by_id = {repo.id: repo for repo in resolved.manifest.repos}
        effective_destination = resolve_effective_repo_destination(
            resolved.entry, repos_by_id
        )
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
        docker_status = docker_cleanup.get("status", "unknown")
        report.add_step(
            "docker_cleanup",
            (
                "ok"
                if docker_status
                in ("removed", "not_found", "not_applicable", "skipped_explicit")
                else "error"
            ),
            detail=(
                str(docker_cleanup.get("message")) if docker_status == "error" else None
            ),
        )
        self._remove_worktree_git_refs(
            worktree_path=worktree_path,
            base_path=resolved.base_path,
            branch=resolved.entry.branch,
            delete_branch=delete_branch,
            delete_remote=delete_remote,
        )
        report.add_step("git_remove", "ok")

        archived_thread_ids = self._archive_bound_pma_threads(
            worktree_repo_id=worktree_repo_id,
            worktree_path=worktree_path,
        )
        report.add_step(
            "archive_pma_threads", "ok", detail=f"archived={len(archived_thread_ids)}"
        )

        orphan_dir_cleanup = self._remove_orphaned_worktree_dir(worktree_path)
        orphan_dir_status = str(orphan_dir_cleanup.get("status", "unknown"))
        report.add_step(
            "worktree_dir_cleanup",
            "ok" if orphan_dir_status in {"removed", "missing"} else "error",
            detail=(
                None
                if orphan_dir_status in {"removed", "missing"}
                else str(orphan_dir_cleanup.get("message") or "")
            ),
        )

        resolved.manifest.repos = [
            r for r in resolved.manifest.repos if r.id != worktree_repo_id
        ]
        save_manifest(
            self._hub_config.manifest_path,
            resolved.manifest,
            self._hub_config.root,
        )
        report.add_step("manifest_remove", "ok")

        return {
            "status": "ok",
            "docker_cleanup": docker_cleanup,
            "cleanup_steps": [
                {"step": s.step, "status": s.status, "detail": s.detail}
                for s in report.steps
            ],
        }

    def archive_worktree(
        self,
        *,
        worktree_repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        report = WorktreeCleanupReport()
        resolved = self._resolve_worktree_entry(worktree_repo_id)
        worktree_path = resolved.worktree_path

        if not worktree_path.exists():
            raise ValueError(f"Worktree path does not exist: {worktree_path}")
        report.add_step("validate", "ok")

        self._ctx.stop_runner(
            repo_id=worktree_repo_id,
            repo_path=worktree_path,
        )
        report.add_step("stop_runner", "ok")

        self._run_telemetry_housekeeping(
            worktree_repo_id=worktree_repo_id,
            worktree_path=worktree_path,
            report=report,
        )

        self._ensure_worktree_clean_for_archive(
            worktree_repo_id=worktree_repo_id,
            worktree_path=worktree_path,
        )

        result = self._archive_worktree_snapshot(
            resolved,
            archive_note=archive_note,
            force=False,
            archive_profile=archive_profile,
        )
        report.add_step("archive_snapshot", "ok")

        archived_thread_ids = self._archive_bound_pma_threads(
            worktree_repo_id=worktree_repo_id,
            worktree_path=worktree_path,
        )
        report.add_step(
            "archive_pma_threads",
            "ok",
            detail=f"archived={len(archived_thread_ids)}",
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

    def archive_worktree_state(
        self,
        *,
        worktree_repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]:
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        entry = manifest.get(worktree_repo_id)
        if not entry or entry.kind != "worktree":
            raise ValueError(f"Worktree repo not found: {worktree_repo_id}")
        return self._ctx.archive_repo_state(
            repo_id=worktree_repo_id,
            archive_note=archive_note,
            archive_profile=archive_profile,
        )

    def _collect_cleanup_worktrees(
        self,
        *,
        manifest: Manifest,
        dry_run: bool,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        worktree_items: list[dict[str, str]] = []
        worktree_errors: list[dict[str, str]] = []
        for entry in manifest.repos:
            worktree_path = (self._hub_config.root / entry.path).resolve()
            is_legacy_linked_worktree = (
                entry.kind != "worktree"
                and worktree_path.exists()
                and git_linked_worktree_base_root(worktree_path) is not None
            )
            if entry.kind != "worktree" and not is_legacy_linked_worktree:
                continue
            if not entry.worktree_of:
                if not is_legacy_linked_worktree:
                    continue
            try:
                if self._has_active_chat_binding(entry.id):
                    continue
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                logger.warning(
                    "cleanup_all: chat binding check failed for %s",
                    entry.id,
                    exc_info=exc,
                )
                continue

            if not worktree_path.exists() or not git_available(worktree_path):
                try:
                    worktree_items.append(
                        self._cleanup_orphaned_worktree_entry(
                            entry=entry,
                            dry_run=dry_run,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "cleanup_all: orphaned worktree cleanup failed for %s",
                        entry.id,
                        exc_info=exc,
                    )
                    worktree_errors.append({"id": entry.id, "error": str(exc)})
                continue

            if git_available(worktree_path):
                try:
                    if not git_is_clean(worktree_path):
                        continue
                except OSError:
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
            except (
                Exception
            ) as exc:  # intentional: cleanup_worktree orchestrates git, archive, docker, and state with diverse failure modes
                logger.warning(
                    "cleanup_all: worktree cleanup failed for %s",
                    entry.id,
                    exc_info=exc,
                )
                worktree_errors.append({"id": entry.id, "error": str(exc)})
        return worktree_items, worktree_errors

    def _cleanup_worktree_flows(
        self,
        *,
        manifest: Manifest,
        dry_run: bool,
    ) -> tuple[list[dict[str, object]], int]:
        flow_by_repo: list[dict[str, object]] = []
        total_flow_count = 0
        for entry in manifest.repos:
            repo_root = (self._hub_config.root / entry.path).resolve()
            db_path = repo_root / ".codex-autorunner" / "flows.db"
            if not db_path.exists():
                continue
            try:
                from .config import ConfigError, load_repo_config

                repo_config = load_repo_config(repo_root)
                durable = bool(getattr(repo_config, "durable_writes", False))
            except ConfigError:
                durable = False
            store = FlowStore(db_path, durable=durable)
            records: list[Any] = []
            try:
                store.initialize()
                records = list(store.list_flow_runs(flow_type="ticket_flow"))
            except (OSError, sqlite3.Error) as exc:
                logger.warning(
                    "cleanup_all: flow store failed for %s",
                    entry.id,
                    exc_info=exc,
                )
                continue
            finally:
                store.close()
            terminal = [r for r in records if r.status.is_terminal()]
            if not terminal:
                continue
            if dry_run:
                n = len(terminal)
                flow_by_repo.append({"repo_id": entry.id, "count": n})
                total_flow_count += n
                continue
            try:
                cleanup_summary = archive_terminal_flow_runs(
                    repo_root,
                    store=store,
                    delete_run=True,
                )
                archived_here = int(cleanup_summary.get("deleted_run_count") or 0)
            except (OSError, ValueError, sqlite3.Error) as exc:
                logger.warning(
                    "cleanup_all: archive terminal runs failed repo=%s",
                    entry.id,
                    exc_info=exc,
                )
                archived_here = 0
            if archived_here:
                flow_by_repo.append({"repo_id": entry.id, "count": archived_here})
            total_flow_count += archived_here
        return flow_by_repo, total_flow_count

    def cleanup_all(self, *, dry_run: bool = False) -> Dict[str, object]:
        manifest = load_manifest(self._hub_config.manifest_path, self._hub_config.root)
        manifest_changed = self._normalize_manifest_worktree_entries(manifest)
        if manifest_changed and not dry_run:
            save_manifest(
                self._hub_config.manifest_path, manifest, self._hub_config.root
            )
        unbound_threads_by_repo = self._ctx.collect_unbound_repo_threads(
            manifest=manifest
        )

        threads_by_repo: list[dict[str, object]] = []
        total_thread_count = 0
        for repo_id, thread_ids in sorted(unbound_threads_by_repo.items()):
            count = len(thread_ids)
            if count == 0:
                continue
            total_thread_count += count
            threads_by_repo.append({"repo_id": repo_id, "count": count})

        if not dry_run:
            for repo_id in self._ctx.base_repo_paths(manifest).keys():
                self._ctx.archive_unbound_repo_threads(
                    repo_id=repo_id,
                    unbound_threads_by_repo=unbound_threads_by_repo,
                )

        worktree_items, worktree_errors = self._collect_cleanup_worktrees(
            manifest=manifest,
            dry_run=dry_run,
        )

        flow_by_repo, total_flow_count = self._cleanup_worktree_flows(
            manifest=manifest,
            dry_run=dry_run,
        )

        if not dry_run and (
            manifest_changed
            or total_thread_count
            or len(worktree_items)
            or total_flow_count
        ):
            self._ctx.invalidate_cache()

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
                        timeout=_WORKTREE_SETUP_COMMAND_TIMEOUT_SECONDS,
                        env=subprocess_env(),
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ValueError(
                        "Worktree setup command %d/%d timed out after %ds: %r"
                        % (
                            idx,
                            len(normalized),
                            _WORKTREE_SETUP_COMMAND_TIMEOUT_SECONDS,
                            command,
                        )
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
