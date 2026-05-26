from __future__ import annotations

import dataclasses
import enum
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from ..manifest import (
    Manifest,
    ManifestRepo,
    load_manifest,
    normalize_manifest_destination,
    save_manifest,
)
from .destinations import (
    default_local_destination,
    resolve_effective_repo_destination,
)
from .git_utils import git_available, git_branch, git_is_clean
from .locks import DEFAULT_RUNNER_CMD_HINTS, assess_lock, process_alive
from .state import RunnerState, load_state, now_iso
from .state_roots import ORCHESTRATION_DB_FILENAME, resolve_repo_runner_state_db_path
from .utils import atomic_write

logger = logging.getLogger("codex_autorunner.hub_topology")


class RepoTopologyRecord(Protocol):
    repo: ManifestRepo
    absolute_path: Path
    added_to_manifest: bool
    exists_on_disk: bool
    initialized: bool
    init_error: Optional[str]


@dataclasses.dataclass
class ManifestRepoRecord:
    repo: ManifestRepo
    absolute_path: Path
    added_to_manifest: bool
    exists_on_disk: bool
    initialized: bool
    init_error: Optional[str] = None


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


class ControlPlaneRole(str, enum.Enum):
    HUB_OWNED = "hub_owned"
    REPO_LOCAL_DATA = "repo_local_data"
    STANDALONE_HUB = "standalone_hub"
    FOREIGN = "foreign"
    ARCHIVED = "archived"


@dataclasses.dataclass(frozen=True)
class NonAuthoritativeArtifact:
    kind: str
    path: Path
    reason: str

    def to_dict(self, hub_root: Path) -> Dict[str, object]:
        try:
            path: Path | str = self.path.relative_to(hub_root)
        except ValueError:
            path = self.path
        return {
            "kind": self.kind,
            "path": str(path),
            "reason": self.reason,
        }


@dataclasses.dataclass(frozen=True)
class WorkspaceRegistryEntry:
    hub_root: Path
    control_plane_id: str
    authoritative_hub_root: Optional[Path]
    repo_id: Optional[str]
    workspace_root: Path
    worktree_root: Optional[Path]
    repo_slug: Optional[str]
    kind: str
    worktree_of: Optional[str]
    resource_kind: str
    enabled: bool
    has_car_state: bool
    default_branch: Optional[str]
    current_branch_hint: Optional[str]
    thread_target_ids: Tuple[str, ...]
    control_plane_role: ControlPlaneRole
    authority_reason: str
    manifest_source: Optional[Path]
    non_authoritative_artifacts: Tuple[NonAuthoritativeArtifact, ...]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "hub_root": str(self.hub_root),
            "control_plane_id": self.control_plane_id,
            "authoritative_hub_root": (
                str(self.authoritative_hub_root)
                if self.authoritative_hub_root is not None
                else None
            ),
            "repo_id": self.repo_id,
            "workspace_root": str(self.workspace_root),
            "worktree_root": str(self.worktree_root) if self.worktree_root else None,
            "repo_slug": self.repo_slug,
            "kind": self.kind,
            "worktree_of": self.worktree_of,
            "resource_kind": self.resource_kind,
            "enabled": self.enabled,
            "has_car_state": self.has_car_state,
            "default_branch": self.default_branch,
            "current_branch_hint": self.current_branch_hint,
            "thread_target_ids": list(self.thread_target_ids),
            "control_plane_role": self.control_plane_role.value,
            "authority_reason": self.authority_reason,
            "manifest_source": (
                str(self.manifest_source) if self.manifest_source is not None else None
            ),
            "non_authoritative_artifacts": [
                artifact.to_dict(self.hub_root)
                for artifact in self.non_authoritative_artifacts
            ],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclasses.dataclass
class RepoSnapshot:
    id: str
    path: Path
    display_name: str
    enabled: bool
    auto_run: bool
    worktree_setup_commands: Optional[List[str]]
    kind: str
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
        except ValueError:
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


@dataclasses.dataclass(frozen=True)
class WorkspaceArchiveTarget:
    base_repo_root: Path
    base_repo_id: str
    workspace_repo_id: str
    worktree_of: str
    source_path: Path | str


@dataclasses.dataclass
class HubState:
    last_scan_at: Optional[str]
    repos: List[RepoSnapshot]
    pinned_parent_repo_ids: List[str] = dataclasses.field(default_factory=list)
    title: str = "Web Hub"

    def to_dict(self, hub_root: Path) -> Dict[str, object]:
        return {
            "last_scan_at": self.last_scan_at,
            "repos": [repo.to_dict(hub_root) for repo in self.repos],
            "pinned_parent_repo_ids": list(self.pinned_parent_repo_ids or []),
            "title": normalize_hub_title(self.title),
        }


class HubTopologyRepository:
    """Single manifest-backed authority for hub topology reads.

    Hub mutations should read manifest state, build refreshed repo snapshots,
    and resolve archive targets through this repository instead of rebuilding
    topology ad hoc in callers.
    """

    def __init__(self, *, hub_root: Path, manifest_path: Path) -> None:
        self._hub_root = hub_root
        self._manifest_path = manifest_path

    def load_manifest(self) -> Manifest:
        return load_manifest(self._manifest_path, self._hub_root)

    def save_manifest(self, manifest: Manifest) -> None:
        self.validate_manifest(manifest)
        save_manifest(self._manifest_path, manifest, self._hub_root)

    def validate_manifest(self, manifest: Manifest) -> None:
        seen_ids: set[str] = set()
        seen_paths: dict[Path, str] = {}
        for entry in manifest.repos:
            if entry.id in seen_ids:
                raise ValueError(f"Duplicate repo id in manifest: {entry.id}")
            seen_ids.add(entry.id)

            absolute_path = (self._hub_root / entry.path).resolve()
            try:
                absolute_path.relative_to(self._hub_root.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"Repo path for {entry.id} must live under hub root"
                ) from exc
            path_owner = seen_paths.get(absolute_path)
            if path_owner is not None:
                raise ValueError(
                    f"Repo path collision: {entry.id} and {path_owner} both use {entry.path}"
                )
            seen_paths[absolute_path] = entry.id

            if entry.kind == "worktree":
                if not entry.worktree_of:
                    raise ValueError(
                        f"Worktree {entry.id} is missing worktree_of metadata"
                    )
                parent = manifest.get(entry.worktree_of)
                if parent is None or parent.kind != "base":
                    raise ValueError(
                        f"Worktree {entry.id} references missing base repo {entry.worktree_of}"
                    )
            elif entry.kind != "base":
                raise ValueError(
                    f"Invalid repo kind for {entry.id}: {entry.kind} (expected base|worktree)"
                )

    def manifest_records(self) -> tuple[Manifest, Sequence[RepoTopologyRecord]]:
        manifest = self.load_manifest()
        records = [self._record_for_repo(entry) for entry in manifest.repos]
        return manifest, records

    def build_hub_state(
        self,
        *,
        existing_pinned_parent_repo_ids: list[str],
        last_scan_at: Optional[str],
        title: str = "Web Hub",
        manifest: Optional[Manifest] = None,
        records: Optional[Sequence[RepoTopologyRecord]] = None,
    ) -> HubState:
        resolved_manifest = manifest if manifest is not None else self.load_manifest()
        resolved_records = (
            list(records)
            if records is not None
            else [self._record_for_repo(entry) for entry in resolved_manifest.repos]
        )
        repos, pinned_parent_repo_ids = build_full_topology(
            resolved_records,
            existing_pinned_parent_repo_ids,
        )
        return HubState(
            last_scan_at=last_scan_at,
            repos=repos,
            pinned_parent_repo_ids=pinned_parent_repo_ids,
            title=normalize_hub_title(title),
        )

    def repo_snapshot(self, repo_id: str) -> RepoSnapshot:
        manifest, records = self.manifest_records()
        record = next((item for item in records if item.repo.id == repo_id), None)
        if record is None:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repos_by_id = {entry.id: entry for entry in manifest.repos}
        return build_repo_snapshot(record, repos_by_id)

    @property
    def hub_root(self) -> Path:
        return self._hub_root

    @property
    def control_plane_id(self) -> str:
        return self._hub_root.resolve().as_posix()

    def workspace_registry(self) -> List[WorkspaceRegistryEntry]:
        manifest = self.load_manifest()
        entries: List[WorkspaceRegistryEntry] = []
        for repo in manifest.repos:
            workspace_root = (self._hub_root / repo.path).resolve()
            entries.append(
                self._workspace_entry_for_manifest_repo(
                    repo,
                    workspace_root=workspace_root,
                    role=ControlPlaneRole.HUB_OWNED,
                    authority_reason="workspace is declared in the hub manifest",
                )
            )
        return entries

    def classify_workspace_path(self, path: Path) -> WorkspaceRegistryEntry:
        """Classify an observed path without letting that path choose authority."""
        observed = path.resolve()
        manifest = self.load_manifest()
        entry = manifest.get_by_path(self._hub_root, observed)
        if entry is not None:
            return self._workspace_entry_for_manifest_repo(
                entry,
                workspace_root=observed,
                role=ControlPlaneRole.HUB_OWNED,
                authority_reason="workspace is declared in the hub manifest",
            )

        role = ControlPlaneRole.FOREIGN
        reason = "path is not declared in the hub manifest"
        try:
            observed.relative_to(self._hub_root.resolve())
        except ValueError:
            pass
        else:
            if observed.name in {"archive", "archives", "retired"} or any(
                part in {"archive", "archives", "retired"} for part in observed.parts
            ):
                role = ControlPlaneRole.ARCHIVED
                reason = "path appears under an archived workspace area"
            elif observed != self._hub_root.resolve():
                role = ControlPlaneRole.REPO_LOCAL_DATA
                reason = (
                    "path is under this hub but is not a manifest workspace; "
                    "local CAR files are data only"
                )

        if observed == self._hub_root.resolve() and self._manifest_path.exists():
            role = ControlPlaneRole.STANDALONE_HUB
            reason = "path is the explicitly configured hub root"

        return WorkspaceRegistryEntry(
            hub_root=self._hub_root.resolve(),
            control_plane_id=self.control_plane_id,
            authoritative_hub_root=(
                self._hub_root.resolve()
                if role in {ControlPlaneRole.HUB_OWNED, ControlPlaneRole.STANDALONE_HUB}
                else None
            ),
            repo_id=None,
            workspace_root=observed,
            worktree_root=None,
            repo_slug=None,
            kind="unknown",
            worktree_of=None,
            resource_kind="workspace",
            enabled=False,
            has_car_state=(observed / ".codex-autorunner").exists(),
            default_branch=None,
            current_branch_hint=_safe_git_branch(observed),
            thread_target_ids=(),
            control_plane_role=role,
            authority_reason=reason,
            manifest_source=(
                self._manifest_path if self._manifest_path.exists() else None
            ),
            non_authoritative_artifacts=tuple(
                _non_authoritative_artifacts(
                    observed,
                    authoritative_hub_root=(
                        self._hub_root.resolve()
                        if role == ControlPlaneRole.STANDALONE_HUB
                        else None
                    ),
                )
            ),
        )

    def binding_context_for_workspace(
        self, workspace_root: Path
    ) -> tuple[Optional[Path], Optional[str]]:
        entry = self.classify_workspace_path(workspace_root)
        if entry.control_plane_role not in {
            ControlPlaneRole.HUB_OWNED,
            ControlPlaneRole.STANDALONE_HUB,
        }:
            return None, None
        return entry.authoritative_hub_root, entry.repo_id

    def resolve_workspace_archive_target(
        self,
        workspace_root: Path,
    ) -> Optional[WorkspaceArchiveTarget]:
        resolved_workspace_root = workspace_root.resolve()
        manifest = self.load_manifest()
        entry = manifest.get_by_path(self._hub_root, resolved_workspace_root)
        if entry is None:
            return None

        base_repo_root = resolved_workspace_root
        base_repo_id = entry.id
        worktree_of = entry.worktree_of or entry.id
        if entry.kind == "worktree" and entry.worktree_of:
            base = manifest.get(entry.worktree_of)
            if base is not None:
                base_repo_root = (self._hub_root / base.path).resolve()
                base_repo_id = base.id
        return WorkspaceArchiveTarget(
            base_repo_root=base_repo_root,
            base_repo_id=base_repo_id,
            workspace_repo_id=entry.id,
            worktree_of=worktree_of,
            source_path=entry.path,
        )

    def _record_for_repo(self, entry: ManifestRepo) -> ManifestRepoRecord:
        repo_path = (self._hub_root / entry.path).resolve()
        return ManifestRepoRecord(
            repo=entry,
            absolute_path=repo_path,
            added_to_manifest=False,
            exists_on_disk=repo_path.exists(),
            initialized=(repo_path / ".codex-autorunner" / "tickets").exists(),
            init_error=None,
        )

    def _workspace_entry_for_manifest_repo(
        self,
        repo: ManifestRepo,
        *,
        workspace_root: Path,
        role: ControlPlaneRole,
        authority_reason: str,
    ) -> WorkspaceRegistryEntry:
        return WorkspaceRegistryEntry(
            hub_root=self._hub_root.resolve(),
            control_plane_id=self.control_plane_id,
            authoritative_hub_root=self._hub_root.resolve(),
            repo_id=repo.id,
            workspace_root=workspace_root,
            worktree_root=workspace_root if repo.kind == "worktree" else None,
            repo_slug=None,
            kind=repo.kind,
            worktree_of=repo.worktree_of,
            resource_kind="worktree" if repo.kind == "worktree" else "repo",
            enabled=repo.enabled,
            has_car_state=(workspace_root / ".codex-autorunner").exists(),
            default_branch=repo.branch if repo.kind == "base" else None,
            current_branch_hint=_safe_git_branch(workspace_root),
            thread_target_ids=(),
            control_plane_role=role,
            authority_reason=authority_reason,
            manifest_source=self._manifest_path,
            non_authoritative_artifacts=tuple(
                _non_authoritative_artifacts(
                    workspace_root,
                    authoritative_hub_root=(
                        self._hub_root.resolve()
                        if (
                            role == ControlPlaneRole.STANDALONE_HUB
                            or workspace_root.resolve() == self._hub_root.resolve()
                        )
                        else None
                    ),
                )
            ),
        )


def _safe_git_branch(workspace_root: Path) -> Optional[str]:
    if not workspace_root.exists():
        return None
    try:
        return git_branch(workspace_root)
    except Exception:
        return None


def _non_authoritative_artifacts(
    workspace_root: Path,
    *,
    authoritative_hub_root: Optional[Path],
) -> List[NonAuthoritativeArtifact]:
    artifacts: List[NonAuthoritativeArtifact] = []
    state_root = workspace_root / ".codex-autorunner"
    candidates = [
        (
            "manifest",
            state_root / "manifest.yml",
            "workspace manifest is data unless this process was launched as that hub",
        ),
        (
            "orchestration_db",
            state_root / ORCHESTRATION_DB_FILENAME,
            "workspace orchestration database is not runtime authority for this hub",
        ),
    ]
    for kind, candidate, reason in candidates:
        if not candidate.exists():
            continue
        if (
            authoritative_hub_root is not None
            and workspace_root.resolve() == authoritative_hub_root.resolve()
        ):
            continue
        artifacts.append(
            NonAuthoritativeArtifact(kind=kind, path=candidate.resolve(), reason=reason)
        )
    return artifacts


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


def derive_repo_status(
    record: RepoTopologyRecord,
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


def build_repo_snapshot(
    record: RepoTopologyRecord,
    repos_by_id: Optional[Dict[str, ManifestRepo]] = None,
) -> RepoSnapshot:
    repo_path = record.absolute_path
    lock_path = repo_path / ".codex-autorunner" / "lock"
    lock_status = read_lock_status(lock_path)

    runner_state: Optional[RunnerState] = None
    if record.initialized:
        runner_state = load_state(resolve_repo_runner_state_db_path(repo_path))

    is_clean: Optional[bool] = None
    if record.exists_on_disk and git_available(repo_path):
        is_clean = git_is_clean(repo_path)

    status = derive_repo_status(record, lock_status, runner_state)
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


def build_repo_snapshots(records: Sequence[RepoTopologyRecord]) -> List[RepoSnapshot]:
    repos_by_id = {record.repo.id: record.repo for record in records}
    return [build_repo_snapshot(record, repos_by_id) for record in records]


def build_full_topology(
    records: Sequence[RepoTopologyRecord],
    existing_pinned_ids: List[str],
) -> Tuple[List[RepoSnapshot], List[str]]:
    snapshots = build_repo_snapshots(records)
    pinned = prune_pinned_parent_repo_ids(existing_pinned_ids, snapshots)
    return snapshots, pinned


def prune_pinned_parent_repo_ids(
    existing_pinned_ids: List[str],
    snapshots: List[RepoSnapshot],
) -> List[str]:
    base_repo_ids = {snap.id for snap in snapshots if snap.kind == "base"}
    pinned = normalize_pinned_parent_repo_ids(existing_pinned_ids)
    return [repo_id for repo_id in pinned if repo_id in base_repo_ids]


def normalize_pinned_parent_repo_ids(value: Any) -> List[str]:
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


def normalize_hub_title(value: Any) -> str:
    if not isinstance(value, str):
        return "Web Hub"
    normalized = " ".join(value.strip().split())
    if not normalized:
        return "Web Hub"
    return normalized[:80]


def load_hub_state(state_path: Path, hub_root: Path) -> HubState:
    if not state_path.exists():
        return HubState(
            last_scan_at=None,
            repos=[],
            pinned_parent_repo_ids=[],
            title="Web Hub",
        )
    data = state_path.read_text(encoding="utf-8")
    try:
        import json as _json

        payload = _json.loads(data)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse hub state from %s: %s", state_path, exc)
        return HubState(
            last_scan_at=None,
            repos=[],
            pinned_parent_repo_ids=[],
            title="Web Hub",
        )
    last_scan_at = payload.get("last_scan_at")
    pinned_parent_repo_ids = normalize_pinned_parent_repo_ids(
        payload.get("pinned_parent_repo_ids")
    )
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
        except (ValueError, TypeError, KeyError) as exc:
            repo_id = entry.get("id", "unknown")
            logger.warning(
                "Failed to load repo snapshot for id=%s from hub state: %s",
                repo_id,
                exc,
            )
            continue
    return HubState(
        last_scan_at=last_scan_at,
        repos=repos,
        pinned_parent_repo_ids=pinned_parent_repo_ids,
        title=normalize_hub_title(payload.get("title")),
    )


def save_hub_state(
    state_path: Path,
    state: HubState,
    hub_root: Path,
) -> None:
    payload = state.to_dict(hub_root)
    atomic_write(state_path, json.dumps(payload, indent=2) + "\n")


def refresh_managed_threads_artifact(hub_root: Path) -> None:
    try:
        from .pma_context import _snapshot_managed_threads

        payload = {
            "generated_at": now_iso(),
            "threads": _snapshot_managed_threads(hub_root),
        }
        artifact_path = hub_root / ".codex-autorunner" / "managed_threads.json"
        atomic_write(artifact_path, json.dumps(payload, indent=2) + "\n")
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Failed to write PMA thread snapshot artifact: %s", exc)
