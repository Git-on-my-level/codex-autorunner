from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Optional

from ..manifest import load_manifest
from ..workspace import workspace_id_for_path
from .archive_retention import (
    WorktreeArchiveRetentionPolicy,
    prune_worktree_archive_root,
)
from .git_utils import git_branch, git_head_sha
from .state import load_state, now_iso
from .utils import atomic_write

ArchiveStatus = Literal["complete", "partial", "failed"]
ArchiveMode = Literal["copy", "move"]
ArchiveProfile = Literal["portable", "full"]

logger = logging.getLogger(__name__)

DEFAULT_CONTEXTSPACE_DOCS = frozenset({"active_context.md", "decisions.md", "spec.md"})
DEFAULT_TICKETS_FILES = frozenset({"AGENTS.md"})
SQLITE_SIDE_SUFFIXES = ("-wal", "-shm")
CAR_STATE_PATHS = (
    "tickets",
    "contextspace",
    "runs",
    "flows",
    "flows.db",
    "state.sqlite3",
    "app_server_threads.json",
    "app_server_workspaces",
    "github_context",
    "filebox",
    "codex-autorunner.log",
    "codex-server.log",
    "lock",
    "workspace",
)
PORTABLE_ARCHIVE_PATHS = frozenset(
    {
        "tickets",
        "contextspace",
        "workspace",
        "runs",
        "flows",
        "github_context",
    }
)
PORTABLE_RESET_ARCHIVE_EXTRA_PATHS = frozenset(
    {
        "state.sqlite3",
        "app_server_threads.json",
        "app_server_workspaces",
        "filebox",
        "codex-autorunner.log",
        "codex-server.log",
        "lock",
    }
)


@dataclass(frozen=True)
class ArchiveResult:
    snapshot_id: str
    snapshot_path: Path
    meta_path: Path
    status: ArchiveStatus
    file_count: int
    total_bytes: int
    flow_run_count: int
    latest_flow_run_id: Optional[str]
    missing_paths: tuple[str, ...]
    skipped_symlinks: tuple[str, ...]


@dataclass(frozen=True)
class ArchivedCarStateResult:
    snapshot_id: str
    snapshot_path: Path
    meta_path: Path
    status: ArchiveStatus
    file_count: int
    total_bytes: int
    flow_run_count: int
    latest_flow_run_id: Optional[str]
    archived_paths: tuple[str, ...]
    reset_paths: tuple[str, ...]
    missing_paths: tuple[str, ...]
    skipped_symlinks: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveEntrySpec:
    label: str
    source: Path
    dest: Path
    mode: ArchiveMode = "copy"
    required: bool = True


@dataclass(frozen=True)
class ArchiveExecutionSummary:
    file_count: int
    total_bytes: int
    copied_paths: tuple[str, ...]
    moved_paths: tuple[str, ...]
    missing_paths: tuple[str, ...]
    skipped_symlinks: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceArchiveTarget:
    base_repo_root: Path
    base_repo_id: str
    workspace_repo_id: str
    worktree_of: str
    source_path: Path | str


def _snapshot_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_BRANCH_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _sanitize_branch(branch: Optional[str]) -> str:
    if not branch:
        return "unknown"
    cleaned = _BRANCH_SANITIZE_RE.sub("-", branch.strip())
    cleaned = cleaned.strip("-")
    return cleaned or "unknown"


def _is_within(*, root: Path, target: Path) -> bool:
    try:
        return target.resolve().is_relative_to(root.resolve())
    except FileNotFoundError:
        return False


def _copy_file(src: Path, dest: Path, stats: dict[str, int]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    stats["file_count"] += 1
    stats["total_bytes"] += dest.stat().st_size


def _copy_tree(
    src_dir: Path,
    dest_dir: Path,
    worktree_root: Path,
    stats: dict[str, int],
    *,
    visited: set[Path],
    skipped_symlinks: list[str],
) -> None:
    real_dir = src_dir.resolve()
    if real_dir in visited:
        return
    visited.add(real_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for child in sorted(src_dir.iterdir(), key=lambda p: p.name):
            _copy_entry(
                child,
                dest_dir / child.name,
                worktree_root,
                stats,
                visited=visited,
                skipped_symlinks=skipped_symlinks,
            )
        try:
            shutil.copystat(src_dir, dest_dir, follow_symlinks=False)
        except OSError:
            pass
    finally:
        visited.remove(real_dir)


def _copy_entry(
    src: Path,
    dest: Path,
    worktree_root: Path,
    stats: dict[str, int],
    *,
    visited: set[Path],
    skipped_symlinks: list[str],
) -> bool:
    if src.is_symlink():
        try:
            resolved = src.resolve()
        except FileNotFoundError:
            skipped_symlinks.append(str(src))
            return False
        if not _is_within(root=worktree_root, target=resolved):
            skipped_symlinks.append(str(src))
            return False
        if resolved.is_dir():
            _copy_tree(
                resolved,
                dest,
                worktree_root,
                stats,
                visited=visited,
                skipped_symlinks=skipped_symlinks,
            )
            return True
        if resolved.is_file():
            _copy_file(resolved, dest, stats)
            return True
        return False

    if src.is_dir():
        _copy_tree(
            src,
            dest,
            worktree_root,
            stats,
            visited=visited,
            skipped_symlinks=skipped_symlinks,
        )
        return True

    if src.is_file():
        _copy_file(src, dest, stats)
        return True

    return False


def _remove_source_entry(src: Path) -> None:
    if src.is_symlink() or src.is_file():
        src.unlink()
        return
    if src.is_dir():
        shutil.rmtree(src)


def _directory_has_any_entries(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


def _tree_has_payload(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    for child in path.rglob("*"):
        if child.is_file() or child.is_symlink():
            return True
    return False


def _contextspace_is_dirty(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    for child in sorted(path.rglob("*")):
        if child.is_dir():
            continue
        rel_path = child.relative_to(path)
        try:
            text = child.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return True
        if (
            len(rel_path.parts) == 1
            and rel_path.name in DEFAULT_CONTEXTSPACE_DOCS
            and text == ""
        ):
            continue
        return True
    return False


def _tickets_are_dirty(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    for child in sorted(path.rglob("*")):
        if child.is_dir():
            continue
        rel_path = child.relative_to(path)
        if len(rel_path.parts) == 1 and rel_path.name in DEFAULT_TICKETS_FILES:
            continue
        return True
    return False


def _runner_state_is_dirty(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        state = load_state(path)
    except Exception:
        return True
    if state.status != "idle":
        return True
    if state.last_run_id is not None or state.last_exit_code is not None:
        return True
    if state.last_run_started_at is not None or state.last_run_finished_at is not None:
        return True
    if state.runner_pid is not None:
        return True
    if state.sessions or state.repo_to_session:
        return True
    override_values = (
        state.autorunner_agent_override,
        state.autorunner_model_override,
        state.autorunner_effort_override,
        state.autorunner_approval_policy,
        state.autorunner_sandbox_mode,
        state.autorunner_workspace_write_network,
        state.runner_stop_after_runs,
    )
    return any(value is not None for value in override_values)


def _json_state_file_is_dirty(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    if not raw:
        return False
    return raw not in {"{}", "[]", "null"}


def _log_file_is_dirty(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _car_state_path_is_dirty(car_root: Path, rel_path: str) -> bool:
    path = car_root / rel_path
    if rel_path == "tickets":
        return _tickets_are_dirty(path)
    if rel_path == "contextspace":
        return _contextspace_is_dirty(path)
    if rel_path in {"runs", "flows"}:
        return _directory_has_any_entries(path)
    if rel_path in {"github_context", "filebox", "app_server_workspaces", "workspace"}:
        return _tree_has_payload(path)
    if rel_path == "state.sqlite3":
        return _runner_state_is_dirty(path)
    if rel_path == "app_server_threads.json":
        return _json_state_file_is_dirty(path)
    if rel_path in {"codex-autorunner.log", "codex-server.log"}:
        return _log_file_is_dirty(path)
    if rel_path == "lock":
        return path.exists()
    if rel_path == "flows.db":
        return path.exists()
    return path.exists()


def dirty_car_state_paths(worktree_root: Path) -> tuple[str, ...]:
    car_root = worktree_root / ".codex-autorunner"
    if not car_root.exists():
        return ()
    return tuple(
        rel_path
        for rel_path in CAR_STATE_PATHS
        if _car_state_path_is_dirty(car_root, rel_path)
    )


def has_car_state(worktree_root: Path) -> bool:
    return bool(dirty_car_state_paths(worktree_root))


def _car_state_archive_entries(
    source_root: Path,
    snapshot_root: Path,
    dirty_paths: Iterable[str],
    *,
    profile: ArchiveProfile = "portable",
    include_reset_extras: bool = False,
) -> list[ArchiveEntrySpec]:
    if profile not in {"portable", "full"}:
        raise ValueError(f"Unsupported archive profile: {profile}")
    entries: list[ArchiveEntrySpec] = []
    dirty_set = set(dirty_paths)
    selected = set(dirty_set)
    if profile == "portable":
        selected &= PORTABLE_ARCHIVE_PATHS
        if include_reset_extras:
            selected |= dirty_set & PORTABLE_RESET_ARCHIVE_EXTRA_PATHS
    if "tickets" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="tickets",
                source=source_root / "tickets",
                dest=snapshot_root / "tickets",
            )
        )
    if "contextspace" in selected or "workspace" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="contextspace",
                source=_contextspace_source(source_root),
                dest=snapshot_root / "contextspace",
            )
        )
    if "runs" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="runs",
                source=source_root / "runs",
                dest=snapshot_root / "runs",
            )
        )
    if "flows" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="flows",
                source=source_root / "flows",
                dest=snapshot_root / "flows",
            )
        )
    if "flows.db" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="flows.db",
                source=source_root / "flows.db",
                dest=snapshot_root / "flows.db",
            )
        )
    if "state.sqlite3" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="state.sqlite3",
                source=source_root / "state.sqlite3",
                dest=snapshot_root / "state" / "state.sqlite3",
            )
        )
    if "app_server_threads.json" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="app_server_threads.json",
                source=source_root / "app_server_threads.json",
                dest=snapshot_root / "state" / "app_server_threads.json",
            )
        )
    if "app_server_workspaces" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="app_server_workspaces",
                source=source_root / "app_server_workspaces",
                dest=snapshot_root / "app_server_workspaces",
            )
        )
    if "github_context" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="github_context",
                source=source_root / "github_context",
                dest=snapshot_root / "github_context",
            )
        )
    if "filebox" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="filebox",
                source=source_root / "filebox",
                dest=snapshot_root / "filebox",
            )
        )
    if "codex-autorunner.log" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="codex-autorunner.log",
                source=source_root / "codex-autorunner.log",
                dest=snapshot_root / "logs" / "codex-autorunner.log",
            )
        )
    if "codex-server.log" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="codex-server.log",
                source=source_root / "codex-server.log",
                dest=snapshot_root / "logs" / "codex-server.log",
            )
        )
    if "lock" in selected:
        entries.append(
            ArchiveEntrySpec(
                label="lock",
                source=source_root / "lock",
                dest=snapshot_root / "state" / "lock",
            )
        )
    entries.append(
        ArchiveEntrySpec(
            label="config.yml",
            source=source_root / "config.yml",
            dest=snapshot_root / "config" / "config.yml",
            required=False,
        )
    )
    return entries


def _remove_with_sidecars(path: Path) -> None:
    if path.exists() or path.is_symlink():
        _remove_source_entry(path)
    if path.name.endswith(".sqlite3") or path.name.endswith(".db"):
        for suffix in SQLITE_SIDE_SUFFIXES:
            sidecar = path.with_name(f"{path.name}{suffix}")
            if sidecar.exists():
                sidecar.unlink()


def _reset_car_state(worktree_root: Path) -> tuple[str, ...]:
    from ..bootstrap import seed_repo_files

    car_root = worktree_root / ".codex-autorunner"
    reset_paths: list[str] = []
    for rel_path in CAR_STATE_PATHS:
        target = car_root / rel_path
        if not target.exists() and not target.is_symlink():
            continue
        _remove_with_sidecars(target)
        reset_paths.append(rel_path)
    seed_repo_files(worktree_root, force=False, git_required=False)
    return tuple(reset_paths)


def resolve_workspace_archive_target(
    workspace_root: Path,
    *,
    hub_root: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
) -> WorkspaceArchiveTarget:
    workspace_root = workspace_root.resolve()
    resolved_hub_root = hub_root.resolve() if hub_root is not None else None
    if (
        manifest_path is not None
        and resolved_hub_root is not None
        and manifest_path.exists()
    ):
        try:
            manifest = load_manifest(manifest_path, resolved_hub_root)
        except Exception:
            manifest = None
        if manifest is not None:
            entry = manifest.get_by_path(resolved_hub_root, workspace_root)
            if entry is not None:
                base_repo_root = workspace_root
                base_repo_id = entry.id
                worktree_of = entry.worktree_of or entry.id
                if entry.kind == "worktree" and entry.worktree_of:
                    base = manifest.get(entry.worktree_of)
                    if base is not None:
                        base_repo_root = (resolved_hub_root / base.path).resolve()
                        base_repo_id = base.id
                return WorkspaceArchiveTarget(
                    base_repo_root=base_repo_root,
                    base_repo_id=base_repo_id,
                    workspace_repo_id=entry.id,
                    worktree_of=worktree_of,
                    source_path=entry.path,
                )
    repo_id = workspace_root.name.strip() or workspace_id_for_path(workspace_root)
    return WorkspaceArchiveTarget(
        base_repo_root=workspace_root,
        base_repo_id=repo_id,
        workspace_repo_id=repo_id,
        worktree_of=repo_id,
        source_path=workspace_root,
    )


def _move_entry(
    src: Path,
    dest: Path,
    worktree_root: Path,
    stats: dict[str, int],
    *,
    visited: set[Path],
    skipped_symlinks: list[str],
) -> bool:
    copied = _copy_entry(
        src,
        dest,
        worktree_root,
        stats,
        visited=visited,
        skipped_symlinks=skipped_symlinks,
    )
    if copied:
        _remove_source_entry(src)
    return copied


def _contextspace_source(source_root: Path) -> Path:
    contextspace = source_root / "contextspace"
    if contextspace.exists() or contextspace.is_symlink():
        return contextspace
    legacy_workspace = source_root / "workspace"
    if legacy_workspace.exists() or legacy_workspace.is_symlink():
        return legacy_workspace
    return contextspace


def build_common_car_archive_entries(
    source_root: Path,
    dest_root: Path,
    *,
    include_contextspace: bool = True,
    include_flow_store: bool = False,
    include_config: bool = False,
    include_runtime_state: bool = False,
    include_logs: bool = False,
    include_github_context: bool = False,
) -> list[ArchiveEntrySpec]:
    entries: list[ArchiveEntrySpec] = []
    if include_contextspace:
        entries.append(
            ArchiveEntrySpec(
                label="contextspace",
                source=_contextspace_source(source_root),
                dest=dest_root / "contextspace",
            )
        )
    if include_flow_store:
        entries.append(
            ArchiveEntrySpec(
                label="flows.db",
                source=source_root / "flows.db",
                dest=dest_root / "flows.db",
            )
        )
    if include_config:
        entries.append(
            ArchiveEntrySpec(
                label="config.yml",
                source=source_root / "config.yml",
                dest=dest_root / "config" / "config.yml",
            )
        )
    if include_runtime_state:
        entries.extend(
            [
                ArchiveEntrySpec(
                    label="state.sqlite3",
                    source=source_root / "state.sqlite3",
                    dest=dest_root / "state" / "state.sqlite3",
                ),
                ArchiveEntrySpec(
                    label="app_server_threads.json",
                    source=source_root / "app_server_threads.json",
                    dest=dest_root / "state" / "app_server_threads.json",
                    required=False,
                ),
            ]
        )
    if include_logs:
        entries.extend(
            [
                ArchiveEntrySpec(
                    label="codex-autorunner.log",
                    source=source_root / "codex-autorunner.log",
                    dest=dest_root / "logs" / "codex-autorunner.log",
                ),
                ArchiveEntrySpec(
                    label="codex-server.log",
                    source=source_root / "codex-server.log",
                    dest=dest_root / "logs" / "codex-server.log",
                ),
            ]
        )
    if include_github_context:
        entries.append(
            ArchiveEntrySpec(
                label="github_context",
                source=source_root / "github_context",
                dest=dest_root / "github_context",
                required=False,
            )
        )
    return entries


def execute_archive_entries(
    entries: Iterable[ArchiveEntrySpec],
    *,
    worktree_root: Path,
) -> ArchiveExecutionSummary:
    stats = {"file_count": 0, "total_bytes": 0}
    copied_paths: list[str] = []
    moved_paths: list[str] = []
    missing_paths: list[str] = []
    skipped_symlinks: list[str] = []
    visited: set[Path] = set()

    for entry in entries:
        src = entry.source
        if not src.exists() and not src.is_symlink():
            if not entry.required:
                continue
            missing_paths.append(entry.label)
            continue
        if entry.mode == "move":
            copied = _move_entry(
                src,
                entry.dest,
                worktree_root,
                stats,
                visited=visited,
                skipped_symlinks=skipped_symlinks,
            )
            if copied:
                moved_paths.append(entry.label)
            continue
        copied = _copy_entry(
            src,
            entry.dest,
            worktree_root,
            stats,
            visited=visited,
            skipped_symlinks=skipped_symlinks,
        )
        if copied:
            copied_paths.append(entry.label)

    return ArchiveExecutionSummary(
        file_count=stats["file_count"],
        total_bytes=stats["total_bytes"],
        copied_paths=tuple(copied_paths),
        moved_paths=tuple(moved_paths),
        missing_paths=tuple(missing_paths),
        skipped_symlinks=tuple(skipped_symlinks),
    )


def _flow_summary(flows_dir: Path) -> tuple[int, Optional[str]]:
    if not flows_dir.exists() or not flows_dir.is_dir():
        return 0, None
    runs: list[Path] = [
        path
        for path in sorted(flows_dir.iterdir(), key=lambda p: p.name)
        if path.is_dir()
    ]
    if not runs:
        return 0, None
    latest = max(
        runs,
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    return len(runs), latest.name


def _build_meta(
    *,
    snapshot_id: str,
    created_at: str,
    status: ArchiveStatus,
    base_repo_id: str,
    worktree_repo_id: str,
    worktree_of: str,
    branch: str,
    head_sha: str,
    source_path: Path,
    copied_paths: Iterable[str],
    missing_paths: Iterable[str],
    skipped_symlinks: Iterable[str],
    summary: dict[str, object],
    note: Optional[str] = None,
    error: Optional[str] = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "status": status,
        "base_repo_id": base_repo_id,
        "worktree_repo_id": worktree_repo_id,
        "worktree_of": worktree_of,
        "branch": branch,
        "head_sha": head_sha,
        "source": {
            "path": str(source_path),
            "copied_paths": list(copied_paths),
            "missing_paths": list(missing_paths),
            "skipped_symlinks": list(skipped_symlinks),
        },
        "summary": summary,
    }
    if note:
        payload["note"] = note
    if error:
        payload["error"] = error
    return payload


def build_snapshot_id(branch: Optional[str], head_sha: str) -> str:
    head_short = head_sha[:7] if head_sha and head_sha != "unknown" else "unknown"
    return f"{_snapshot_timestamp()}--{_sanitize_branch(branch)}--{head_short}"


def archive_worktree_snapshot(
    *,
    base_repo_root: Path,
    base_repo_id: str,
    worktree_repo_root: Path,
    worktree_repo_id: str,
    branch: Optional[str],
    worktree_of: str,
    note: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    head_sha: Optional[str] = None,
    source_path: Optional[Path | str] = None,
    profile: ArchiveProfile = "portable",
    include_flow_store_in_portable: bool = False,
    retention_policy: Optional[WorktreeArchiveRetentionPolicy] = None,
) -> ArchiveResult:
    base_repo_root = base_repo_root.resolve()
    worktree_repo_root = worktree_repo_root.resolve()
    branch_name = branch or git_branch(worktree_repo_root) or "unknown"
    resolved_head_sha = head_sha or git_head_sha(worktree_repo_root) or "unknown"
    snapshot_id = snapshot_id or build_snapshot_id(branch_name, resolved_head_sha)
    snapshot_root = (
        base_repo_root
        / ".codex-autorunner"
        / "archive"
        / "worktrees"
        / worktree_repo_id
        / snapshot_id
    )
    snapshot_root.mkdir(parents=True, exist_ok=False)

    source_root = worktree_repo_root / ".codex-autorunner"
    created_at = now_iso()
    meta_path = snapshot_root / "META.json"
    summary: dict[str, object] = {}
    if profile not in {"portable", "full"}:
        raise ValueError(f"Unsupported archive profile: {profile}")
    entries = build_common_car_archive_entries(
        source_root,
        snapshot_root,
        include_flow_store=profile == "full" or include_flow_store_in_portable,
        include_config=True,
        include_runtime_state=profile == "full",
        include_logs=profile == "full",
        include_github_context=True,
    )
    entries.extend(
        [
            ArchiveEntrySpec(
                label="tickets",
                source=source_root / "tickets",
                dest=snapshot_root / "tickets",
            ),
            ArchiveEntrySpec(
                label="runs",
                source=source_root / "runs",
                dest=snapshot_root / "runs",
            ),
            ArchiveEntrySpec(
                label="flows",
                source=source_root / "flows",
                dest=snapshot_root / "flows",
            ),
        ]
    )

    try:
        execution = execute_archive_entries(entries, worktree_root=worktree_repo_root)

        flow_run_count, latest_flow_run_id = _flow_summary(snapshot_root / "flows")
        status: ArchiveStatus = "complete" if not execution.missing_paths else "partial"
        summary = {
            "file_count": execution.file_count,
            "total_bytes": execution.total_bytes,
            "flow_run_count": flow_run_count,
            "latest_flow_run_id": latest_flow_run_id,
        }
        meta = _build_meta(
            snapshot_id=snapshot_id,
            created_at=created_at,
            status=status,
            base_repo_id=base_repo_id,
            worktree_repo_id=worktree_repo_id,
            worktree_of=worktree_of,
            branch=branch_name,
            head_sha=resolved_head_sha,
            source_path=(
                Path(source_path) if source_path is not None else worktree_repo_root
            ),
            copied_paths=execution.copied_paths,
            missing_paths=execution.missing_paths,
            skipped_symlinks=execution.skipped_symlinks,
            summary=summary,
            note=note,
        )
        atomic_write(meta_path, json.dumps(meta, indent=2) + "\n")
        if retention_policy is not None:
            try:
                prune_worktree_archive_root(
                    base_repo_root / ".codex-autorunner" / "archive" / "worktrees",
                    policy=retention_policy,
                    preserve_paths=(snapshot_root,),
                )
            except Exception:
                logger.warning(
                    "Failed to prune worktree archives under %s",
                    base_repo_root / ".codex-autorunner" / "archive" / "worktrees",
                    exc_info=True,
                )
    except Exception as exc:
        summary = {
            "file_count": 0,
            "total_bytes": 0,
            "flow_run_count": 0,
            "latest_flow_run_id": None,
        }
        meta = _build_meta(
            snapshot_id=snapshot_id,
            created_at=created_at,
            status="failed",
            base_repo_id=base_repo_id,
            worktree_repo_id=worktree_repo_id,
            worktree_of=worktree_of,
            branch=branch_name,
            head_sha=resolved_head_sha,
            source_path=(
                Path(source_path) if source_path is not None else worktree_repo_root
            ),
            copied_paths=(),
            missing_paths=(),
            skipped_symlinks=(),
            summary=summary,
            note=note,
            error=str(exc),
        )
        atomic_write(meta_path, json.dumps(meta, indent=2) + "\n")
        raise

    return ArchiveResult(
        snapshot_id=snapshot_id,
        snapshot_path=snapshot_root,
        meta_path=meta_path,
        status=status,
        file_count=execution.file_count,
        total_bytes=execution.total_bytes,
        flow_run_count=flow_run_count,
        latest_flow_run_id=latest_flow_run_id,
        missing_paths=execution.missing_paths,
        skipped_symlinks=execution.skipped_symlinks,
    )


def archive_workspace_car_state(
    *,
    base_repo_root: Path,
    base_repo_id: str,
    worktree_repo_root: Path,
    worktree_repo_id: str,
    branch: Optional[str],
    worktree_of: str,
    note: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    head_sha: Optional[str] = None,
    source_path: Optional[Path | str] = None,
    profile: ArchiveProfile = "portable",
    retention_policy: Optional[WorktreeArchiveRetentionPolicy] = None,
) -> ArchivedCarStateResult:
    base_repo_root = base_repo_root.resolve()
    worktree_repo_root = worktree_repo_root.resolve()
    if profile not in {"portable", "full"}:
        raise ValueError(f"Unsupported archive profile: {profile}")
    dirty_paths = dirty_car_state_paths(worktree_repo_root)
    if not dirty_paths:
        raise ValueError("No CAR state to archive. Workspace is already clean.")

    branch_name = branch or git_branch(worktree_repo_root) or "unknown"
    resolved_head_sha = head_sha or git_head_sha(worktree_repo_root) or "unknown"
    snapshot_id = snapshot_id or build_snapshot_id(branch_name, resolved_head_sha)
    snapshot_root = (
        base_repo_root
        / ".codex-autorunner"
        / "archive"
        / "worktrees"
        / worktree_repo_id
        / snapshot_id
    )
    snapshot_root.mkdir(parents=True, exist_ok=False)

    source_root = worktree_repo_root / ".codex-autorunner"
    created_at = now_iso()
    meta_path = snapshot_root / "META.json"
    entries = _car_state_archive_entries(
        source_root,
        snapshot_root,
        dirty_paths,
        profile=profile,
        include_reset_extras=True,
    )

    try:
        execution = execute_archive_entries(entries, worktree_root=worktree_repo_root)
        reset_paths = _reset_car_state(worktree_repo_root)
        flow_run_count, latest_flow_run_id = _flow_summary(snapshot_root / "flows")
        status: ArchiveStatus = "complete" if not execution.missing_paths else "partial"
        execution_summary: dict[str, object] = {
            "file_count": execution.file_count,
            "total_bytes": execution.total_bytes,
            "flow_run_count": flow_run_count,
            "latest_flow_run_id": latest_flow_run_id,
            "archived_paths": list(execution.copied_paths),
            "reset_paths": list(reset_paths),
        }
        meta = _build_meta(
            snapshot_id=snapshot_id,
            created_at=created_at,
            status=status,
            base_repo_id=base_repo_id,
            worktree_repo_id=worktree_repo_id,
            worktree_of=worktree_of,
            branch=branch_name,
            head_sha=resolved_head_sha,
            source_path=(
                Path(source_path) if source_path is not None else worktree_repo_root
            ),
            copied_paths=execution.copied_paths,
            missing_paths=execution.missing_paths,
            skipped_symlinks=execution.skipped_symlinks,
            summary=execution_summary,
            note=note,
        )
        atomic_write(meta_path, json.dumps(meta, indent=2) + "\n")
        if retention_policy is not None:
            try:
                prune_worktree_archive_root(
                    base_repo_root / ".codex-autorunner" / "archive" / "worktrees",
                    policy=retention_policy,
                    preserve_paths=(snapshot_root,),
                )
            except Exception:
                logger.warning(
                    "Failed to prune worktree archives under %s",
                    base_repo_root / ".codex-autorunner" / "archive" / "worktrees",
                    exc_info=True,
                )
    except Exception as exc:
        failed_summary: dict[str, object] = {
            "file_count": 0,
            "total_bytes": 0,
            "flow_run_count": 0,
            "latest_flow_run_id": None,
            "archived_paths": [],
            "reset_paths": [],
        }
        meta = _build_meta(
            snapshot_id=snapshot_id,
            created_at=created_at,
            status="failed",
            base_repo_id=base_repo_id,
            worktree_repo_id=worktree_repo_id,
            worktree_of=worktree_of,
            branch=branch_name,
            head_sha=resolved_head_sha,
            source_path=(
                Path(source_path) if source_path is not None else worktree_repo_root
            ),
            copied_paths=(),
            missing_paths=(),
            skipped_symlinks=(),
            summary=failed_summary,
            note=note,
            error=str(exc),
        )
        atomic_write(meta_path, json.dumps(meta, indent=2) + "\n")
        raise

    return ArchivedCarStateResult(
        snapshot_id=snapshot_id,
        snapshot_path=snapshot_root,
        meta_path=meta_path,
        status=status,
        file_count=execution.file_count,
        total_bytes=execution.total_bytes,
        flow_run_count=flow_run_count,
        latest_flow_run_id=latest_flow_run_id,
        archived_paths=execution.copied_paths,
        reset_paths=reset_paths,
        missing_paths=execution.missing_paths,
        skipped_symlinks=execution.skipped_symlinks,
    )
