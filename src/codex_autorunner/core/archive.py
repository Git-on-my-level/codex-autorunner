from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Optional

from .git_utils import git_branch, git_head_sha
from .state import now_iso
from .utils import atomic_write

ArchiveStatus = Literal["complete", "partial", "failed"]


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


def _snapshot_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_BRANCH_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _sanitize_branch(branch: Optional[str]) -> str:
    if not branch:
        return "unknown"
    cleaned = _BRANCH_SANITIZE_RE.sub("-", branch.strip())
    cleaned = cleaned.strip("-")
    return cleaned or "unknown"


def _is_within(root: Path, target: Path) -> bool:
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
        if not _is_within(worktree_root, resolved):
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
    curated: list[tuple[Path, Path]] = [
        (source_root / "workspace", snapshot_root / "workspace"),
        (source_root / "tickets", snapshot_root / "tickets"),
        (source_root / "runs", snapshot_root / "runs"),
        (source_root / "flows", snapshot_root / "flows"),
        (source_root / "flows.db", snapshot_root / "flows.db"),
        (source_root / "config.yml", snapshot_root / "config" / "config.yml"),
        (source_root / "state.sqlite3", snapshot_root / "state" / "state.sqlite3"),
        (
            source_root / "codex-autorunner.log",
            snapshot_root / "logs" / "codex-autorunner.log",
        ),
        (
            source_root / "codex-server.log",
            snapshot_root / "logs" / "codex-server.log",
        ),
    ]

    stats = {"file_count": 0, "total_bytes": 0}
    copied_paths: list[str] = []
    missing_paths: list[str] = []
    skipped_symlinks: list[str] = []
    visited: set[Path] = set()
    created_at = now_iso()
    meta_path = snapshot_root / "META.json"
    summary: dict[str, object] = {}

    try:
        for src, dest in curated:
            rel = src.relative_to(source_root)
            if not src.exists() and not src.is_symlink():
                missing_paths.append(str(rel))
                continue
            copied = _copy_entry(
                src,
                dest,
                worktree_repo_root,
                stats,
                visited=visited,
                skipped_symlinks=skipped_symlinks,
            )
            if copied:
                copied_paths.append(str(rel))

        flow_run_count, latest_flow_run_id = _flow_summary(snapshot_root / "flows")
        status: ArchiveStatus = "complete" if not missing_paths else "partial"
        summary = {
            "file_count": stats["file_count"],
            "total_bytes": stats["total_bytes"],
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
            copied_paths=copied_paths,
            missing_paths=missing_paths,
            skipped_symlinks=skipped_symlinks,
            summary=summary,
            note=note,
        )
        atomic_write(meta_path, json.dumps(meta, indent=2) + "\n")
    except Exception as exc:
        summary = {
            "file_count": stats["file_count"],
            "total_bytes": stats["total_bytes"],
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
            copied_paths=copied_paths,
            missing_paths=missing_paths,
            skipped_symlinks=skipped_symlinks,
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
        file_count=stats["file_count"],
        total_bytes=stats["total_bytes"],
        flow_run_count=flow_run_count,
        latest_flow_run_id=latest_flow_run_id,
        missing_paths=tuple(missing_paths),
        skipped_symlinks=tuple(skipped_symlinks),
    )
