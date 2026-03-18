from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal, Mapping, Optional
from uuid import uuid4

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
ArchiveIntent = Literal[
    "review_snapshot",
    "review_snapshot_full",
    "cleanup_snapshot",
    "cleanup_snapshot_full",
    "reset_car_state",
]
CarStatePayloadKind = Literal["review_relevant", "runtime_only", "both"]

logger = logging.getLogger(__name__)

DEFAULT_CONTEXTSPACE_DOCS = frozenset({"active_context.md", "decisions.md", "spec.md"})
DEFAULT_TICKETS_FILES = frozenset({"AGENTS.md"})
SQLITE_SIDE_SUFFIXES = ("-wal", "-shm")


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


@dataclass(frozen=True)
class CarStatePathSpec:
    key: str
    archive_dest: str
    dirty_check: Callable[[Path], bool]
    archive_intents: frozenset[ArchiveIntent]
    payload_kind: CarStatePayloadKind
    reset_paths: tuple[str, ...]
    source_resolver: Optional[Callable[[Path], Path]] = None
    required: bool = True


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


def _copy_sqlite_sidecars(src: Path, dest: Path, stats: dict[str, int]) -> None:
    if not (src.name.endswith(".sqlite3") or src.name.endswith(".db")):
        return
    for suffix in SQLITE_SIDE_SUFFIXES:
        sidecar_src = src.with_name(f"{src.name}{suffix}")
        if not sidecar_src.exists() or not sidecar_src.is_file():
            continue
        sidecar_dest = dest.with_name(f"{dest.name}{suffix}")
        _copy_file(sidecar_src, sidecar_dest, stats)


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
        _copy_sqlite_sidecars(src, dest, stats)
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


CAR_STATE_PATH_SPECS = (
    CarStatePathSpec(
        key="tickets",
        archive_dest="tickets",
        dirty_check=_tickets_are_dirty,
        archive_intents=frozenset(
            {
                "review_snapshot",
                "review_snapshot_full",
                "cleanup_snapshot",
                "cleanup_snapshot_full",
                "reset_car_state",
            }
        ),
        payload_kind="review_relevant",
        reset_paths=("tickets",),
    ),
    CarStatePathSpec(
        key="contextspace",
        archive_dest="contextspace",
        dirty_check=_contextspace_is_dirty,
        archive_intents=frozenset(
            {
                "review_snapshot",
                "review_snapshot_full",
                "cleanup_snapshot",
                "cleanup_snapshot_full",
                "reset_car_state",
            }
        ),
        payload_kind="review_relevant",
        reset_paths=("contextspace", "workspace"),
        source_resolver=lambda source_root: _contextspace_source(source_root),
    ),
    CarStatePathSpec(
        key="runs",
        archive_dest="runs",
        dirty_check=_directory_has_any_entries,
        archive_intents=frozenset(
            {
                "review_snapshot",
                "review_snapshot_full",
                "cleanup_snapshot",
                "cleanup_snapshot_full",
                "reset_car_state",
            }
        ),
        payload_kind="review_relevant",
        reset_paths=("runs",),
    ),
    CarStatePathSpec(
        key="flows",
        archive_dest="flows",
        dirty_check=_directory_has_any_entries,
        archive_intents=frozenset(
            {
                "review_snapshot",
                "review_snapshot_full",
                "cleanup_snapshot",
                "cleanup_snapshot_full",
                "reset_car_state",
            }
        ),
        payload_kind="review_relevant",
        reset_paths=("flows",),
    ),
    CarStatePathSpec(
        key="flows.db",
        archive_dest="flows.db",
        dirty_check=lambda path: path.exists(),
        archive_intents=frozenset(
            {
                "review_snapshot_full",
                "cleanup_snapshot",
                "cleanup_snapshot_full",
                "reset_car_state",
            }
        ),
        payload_kind="both",
        reset_paths=("flows.db",),
    ),
    CarStatePathSpec(
        key="state.sqlite3",
        archive_dest="state/state.sqlite3",
        dirty_check=_runner_state_is_dirty,
        archive_intents=frozenset(
            {
                "review_snapshot_full",
                "cleanup_snapshot_full",
                "reset_car_state",
            }
        ),
        payload_kind="runtime_only",
        reset_paths=("state.sqlite3",),
    ),
    CarStatePathSpec(
        key="app_server_threads.json",
        archive_dest="state/app_server_threads.json",
        dirty_check=_json_state_file_is_dirty,
        archive_intents=frozenset(
            {
                "review_snapshot_full",
                "cleanup_snapshot_full",
                "reset_car_state",
            }
        ),
        payload_kind="runtime_only",
        reset_paths=("app_server_threads.json",),
        required=False,
    ),
    CarStatePathSpec(
        key="app_server_workspaces",
        archive_dest="app_server_workspaces",
        dirty_check=_tree_has_payload,
        archive_intents=frozenset({"reset_car_state"}),
        payload_kind="runtime_only",
        reset_paths=("app_server_workspaces",),
    ),
    CarStatePathSpec(
        key="github_context",
        archive_dest="github_context",
        dirty_check=_tree_has_payload,
        archive_intents=frozenset(
            {
                "review_snapshot",
                "review_snapshot_full",
                "cleanup_snapshot",
                "cleanup_snapshot_full",
                "reset_car_state",
            }
        ),
        payload_kind="review_relevant",
        reset_paths=("github_context",),
        required=False,
    ),
    CarStatePathSpec(
        key="filebox",
        archive_dest="filebox",
        dirty_check=_tree_has_payload,
        archive_intents=frozenset({"reset_car_state"}),
        payload_kind="both",
        reset_paths=("filebox",),
    ),
    CarStatePathSpec(
        key="codex-autorunner.log",
        archive_dest="logs/codex-autorunner.log",
        dirty_check=_log_file_is_dirty,
        archive_intents=frozenset(
            {
                "review_snapshot_full",
                "cleanup_snapshot_full",
                "reset_car_state",
            }
        ),
        payload_kind="runtime_only",
        reset_paths=("codex-autorunner.log",),
    ),
    CarStatePathSpec(
        key="codex-server.log",
        archive_dest="logs/codex-server.log",
        dirty_check=_log_file_is_dirty,
        archive_intents=frozenset(
            {
                "review_snapshot_full",
                "cleanup_snapshot_full",
                "reset_car_state",
            }
        ),
        payload_kind="runtime_only",
        reset_paths=("codex-server.log",),
    ),
    CarStatePathSpec(
        key="lock",
        archive_dest="state/lock",
        dirty_check=lambda path: path.exists() or path.is_symlink(),
        archive_intents=frozenset({"reset_car_state"}),
        payload_kind="runtime_only",
        reset_paths=("lock",),
    ),
)


def _resolve_car_state_source(spec: CarStatePathSpec, source_root: Path) -> Path:
    if spec.source_resolver is not None:
        return spec.source_resolver(source_root)
    return source_root / spec.key


def resolve_worktree_archive_intent(
    *,
    profile: ArchiveProfile = "portable",
    cleanup: bool = False,
) -> ArchiveIntent:
    if profile not in {"portable", "full"}:
        raise ValueError(f"Unsupported archive profile: {profile}")
    if cleanup:
        return "cleanup_snapshot_full" if profile == "full" else "cleanup_snapshot"
    return "review_snapshot_full" if profile == "full" else "review_snapshot"


def dirty_car_state_paths(worktree_root: Path) -> tuple[str, ...]:
    car_root = worktree_root / ".codex-autorunner"
    if not car_root.exists():
        return ()
    return tuple(
        spec.key
        for spec in CAR_STATE_PATH_SPECS
        if spec.dirty_check(_resolve_car_state_source(spec, car_root))
    )


def has_car_state(worktree_root: Path) -> bool:
    return bool(dirty_car_state_paths(worktree_root))


def _build_car_state_archive_entries(
    source_root: Path,
    snapshot_root: Path,
    *,
    intent: ArchiveIntent,
    path_filter: Optional[Iterable[str]] = None,
    include_config: bool = False,
) -> list[ArchiveEntrySpec]:
    entries: list[ArchiveEntrySpec] = []
    selected_paths = set(path_filter) if path_filter is not None else None
    for spec in CAR_STATE_PATH_SPECS:
        if intent not in spec.archive_intents:
            continue
        if selected_paths is not None and spec.key not in selected_paths:
            continue
        entries.append(
            ArchiveEntrySpec(
                label=spec.key,
                source=_resolve_car_state_source(spec, source_root),
                dest=snapshot_root / spec.archive_dest,
                required=spec.required,
            )
        )
    if include_config:
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


def _planned_reset_car_state_paths(worktree_root: Path) -> tuple[str, ...]:
    car_root = worktree_root / ".codex-autorunner"
    reset_paths: list[str] = []
    seen: set[str] = set()
    for spec in CAR_STATE_PATH_SPECS:
        for rel_path in spec.reset_paths:
            if rel_path in seen:
                continue
            target = car_root / rel_path
            if not target.exists() and not target.is_symlink():
                continue
            reset_paths.append(rel_path)
            seen.add(rel_path)
    return tuple(reset_paths)


def _reset_car_state(worktree_root: Path) -> tuple[str, ...]:
    from ..bootstrap import seed_repo_files

    car_root = worktree_root / ".codex-autorunner"
    reset_paths = list(_planned_reset_car_state_paths(worktree_root))
    for rel_path in reset_paths:
        target = car_root / rel_path
        _remove_with_sidecars(target)
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
    archive_intent: ArchiveIntent,
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
    summary: Mapping[str, object],
    note: Optional[str] = None,
    error: Optional[str] = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "archive_intent": archive_intent,
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


def _prepare_snapshot_roots(final_snapshot_root: Path) -> tuple[Path, Path]:
    final_snapshot_root.parent.mkdir(parents=True, exist_ok=True)
    if final_snapshot_root.exists():
        raise FileExistsError(f"Snapshot already exists: {final_snapshot_root}")
    staging_root = final_snapshot_root.parent / (
        f".{final_snapshot_root.name}.tmp-{uuid4().hex}"
    )
    staging_root.mkdir(parents=False, exist_ok=False)
    return final_snapshot_root, staging_root


def _finalize_snapshot_root(staging_root: Path, final_snapshot_root: Path) -> None:
    staging_root.rename(final_snapshot_root)


def _cleanup_staging_snapshot_root(staging_root: Path) -> None:
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)


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
    intent: Optional[ArchiveIntent] = None,
    profile: ArchiveProfile = "portable",
    retention_policy: Optional[WorktreeArchiveRetentionPolicy] = None,
) -> ArchiveResult:
    base_repo_root = base_repo_root.resolve()
    worktree_repo_root = worktree_repo_root.resolve()
    branch_name = branch or git_branch(worktree_repo_root) or "unknown"
    resolved_head_sha = head_sha or git_head_sha(worktree_repo_root) or "unknown"
    snapshot_id = snapshot_id or build_snapshot_id(branch_name, resolved_head_sha)
    final_snapshot_root = (
        base_repo_root
        / ".codex-autorunner"
        / "archive"
        / "worktrees"
        / worktree_repo_id
        / snapshot_id
    )
    resolved_intent = intent or resolve_worktree_archive_intent(profile=profile)
    if resolved_intent not in {
        "review_snapshot",
        "review_snapshot_full",
        "cleanup_snapshot",
        "cleanup_snapshot_full",
    }:
        raise ValueError(f"Unsupported worktree archive intent: {resolved_intent}")
    _, staging_root = _prepare_snapshot_roots(final_snapshot_root)

    source_root = worktree_repo_root / ".codex-autorunner"
    created_at = now_iso()
    meta_path = staging_root / "META.json"

    try:
        entries = _build_car_state_archive_entries(
            source_root,
            staging_root,
            intent=resolved_intent,
            include_config=True,
        )
        execution = execute_archive_entries(entries, worktree_root=worktree_repo_root)

        flow_run_count, latest_flow_run_id = _flow_summary(staging_root / "flows")
        status: ArchiveStatus = "complete" if not execution.missing_paths else "partial"
        summary = {
            "file_count": execution.file_count,
            "total_bytes": execution.total_bytes,
            "flow_run_count": flow_run_count,
            "latest_flow_run_id": latest_flow_run_id,
        }
        meta = _build_meta(
            snapshot_id=snapshot_id,
            archive_intent=resolved_intent,
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
        _finalize_snapshot_root(staging_root, final_snapshot_root)
        if retention_policy is not None:
            try:
                prune_worktree_archive_root(
                    base_repo_root / ".codex-autorunner" / "archive" / "worktrees",
                    policy=retention_policy,
                    preserve_paths=(final_snapshot_root,),
                )
            except Exception:
                logger.warning(
                    "Failed to prune worktree archives under %s",
                    base_repo_root / ".codex-autorunner" / "archive" / "worktrees",
                    exc_info=True,
                )
    except Exception as exc:
        logger.warning(
            "Failed to finalize worktree archive snapshot %s intent=%s: %s",
            snapshot_id,
            resolved_intent,
            exc,
        )
        _cleanup_staging_snapshot_root(staging_root)
        raise

    return ArchiveResult(
        snapshot_id=snapshot_id,
        snapshot_path=final_snapshot_root,
        meta_path=final_snapshot_root / "META.json",
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
    intent: ArchiveIntent = "reset_car_state",
    retention_policy: Optional[WorktreeArchiveRetentionPolicy] = None,
) -> ArchivedCarStateResult:
    base_repo_root = base_repo_root.resolve()
    worktree_repo_root = worktree_repo_root.resolve()
    if intent != "reset_car_state":
        raise ValueError(f"Unsupported workspace CAR-state archive intent: {intent}")
    dirty_paths = dirty_car_state_paths(worktree_repo_root)
    if not dirty_paths:
        raise ValueError("No CAR state to archive. Workspace is already clean.")

    branch_name = branch or git_branch(worktree_repo_root) or "unknown"
    resolved_head_sha = head_sha or git_head_sha(worktree_repo_root) or "unknown"
    snapshot_id = snapshot_id or build_snapshot_id(branch_name, resolved_head_sha)
    final_snapshot_root = (
        base_repo_root
        / ".codex-autorunner"
        / "archive"
        / "worktrees"
        / worktree_repo_id
        / snapshot_id
    )
    _, staging_root = _prepare_snapshot_roots(final_snapshot_root)

    source_root = worktree_repo_root / ".codex-autorunner"
    created_at = now_iso()
    meta_path = staging_root / "META.json"
    planned_reset_paths = _planned_reset_car_state_paths(worktree_repo_root)

    try:
        entries = _build_car_state_archive_entries(
            source_root,
            staging_root,
            intent=intent,
            path_filter=dirty_paths,
            include_config=True,
        )
        execution = execute_archive_entries(entries, worktree_root=worktree_repo_root)
        flow_run_count, latest_flow_run_id = _flow_summary(staging_root / "flows")
        status: ArchiveStatus = "complete" if not execution.missing_paths else "partial"
        execution_summary: dict[str, object] = {
            "file_count": execution.file_count,
            "total_bytes": execution.total_bytes,
            "flow_run_count": flow_run_count,
            "latest_flow_run_id": latest_flow_run_id,
            "archived_paths": list(execution.copied_paths),
            "reset_paths": list(planned_reset_paths),
        }
        meta = _build_meta(
            snapshot_id=snapshot_id,
            archive_intent=intent,
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
        _finalize_snapshot_root(staging_root, final_snapshot_root)
        reset_paths = _reset_car_state(worktree_repo_root)
        if retention_policy is not None:
            try:
                prune_worktree_archive_root(
                    base_repo_root / ".codex-autorunner" / "archive" / "worktrees",
                    policy=retention_policy,
                    preserve_paths=(final_snapshot_root,),
                )
            except Exception:
                logger.warning(
                    "Failed to prune worktree archives under %s",
                    base_repo_root / ".codex-autorunner" / "archive" / "worktrees",
                    exc_info=True,
                )
        if reset_paths != planned_reset_paths:
            final_meta = dict(meta)
            final_summary = dict(execution_summary)
            final_summary["reset_paths"] = list(reset_paths)
            final_meta["summary"] = final_summary
            try:
                atomic_write(
                    final_snapshot_root / "META.json",
                    json.dumps(final_meta, indent=2) + "\n",
                )
            except Exception:
                logger.warning(
                    "Failed to refresh reset_paths in archive metadata for %s",
                    final_snapshot_root,
                    exc_info=True,
                )
    except Exception as exc:
        logger.warning(
            "Failed to finalize CAR state archive snapshot %s intent=%s: %s",
            snapshot_id,
            intent,
            exc,
        )
        _cleanup_staging_snapshot_root(staging_root)
        raise

    return ArchivedCarStateResult(
        snapshot_id=snapshot_id,
        snapshot_path=final_snapshot_root,
        meta_path=final_snapshot_root / "META.json",
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
