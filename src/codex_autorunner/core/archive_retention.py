from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class WorktreeArchiveRetentionPolicy:
    max_snapshots_per_repo: int
    max_age_days: int
    max_total_bytes: int


@dataclass(frozen=True)
class RunArchiveRetentionPolicy:
    max_entries: int
    max_age_days: int
    max_total_bytes: int


@dataclass(frozen=True)
class ArchivePruneSummary:
    kept: int
    pruned: int
    bytes_before: int
    bytes_after: int
    pruned_paths: tuple[str, ...]


@dataclass(frozen=True)
class _ArchiveEntry:
    key: str
    path: Path
    created_at: datetime
    size_bytes: int
    preserve: bool


def _coerce_created_at(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _path_size_bytes(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        try:
            total += child.stat().st_size
        except OSError:
            continue
    return total


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path)


def _prune_empty_dirs(base: Path) -> None:
    if not base.exists():
        return
    for candidate in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if candidate.is_dir():
            try:
                next(candidate.iterdir())
            except StopIteration:
                candidate.rmdir()
            except OSError:
                continue


def _load_meta_created_at(snapshot_dir: Path) -> Optional[datetime]:
    meta_path = snapshot_dir / "META.json"
    if not meta_path.exists():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return _coerce_created_at(payload.get("created_at"))


def _has_snapshot_meta(snapshot_dir: Path) -> bool:
    return (snapshot_dir / "META.json").is_file()


def _fallback_created_at(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _prune_entries(
    entries: list[_ArchiveEntry],
    *,
    max_keep: int,
    max_age_days: int,
    max_total_bytes: int,
    dry_run: bool,
) -> ArchivePruneSummary:
    if not entries:
        return ArchivePruneSummary(
            kept=0,
            pruned=0,
            bytes_before=0,
            bytes_after=0,
            pruned_paths=(),
        )

    bytes_before = sum(entry.size_bytes for entry in entries)
    entries_sorted = sorted(
        entries,
        key=lambda entry: (entry.created_at, entry.key),
        reverse=True,
    )
    age_kept: list[_ArchiveEntry] = []
    pruned: list[_ArchiveEntry] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, max_age_days))

    for entry in entries_sorted:
        if not entry.preserve and max_age_days >= 0 and entry.created_at < cutoff:
            pruned.append(entry)
            continue
        age_kept.append(entry)

    kept: list[_ArchiveEntry] = []
    if max_keep < 0:
        kept = list(age_kept)
    else:
        for entry in age_kept:
            if len(kept) < max_keep:
                kept.append(entry)
                continue
            if not entry.preserve:
                pruned.append(entry)
                continue
            kept.append(entry)
            pruned_candidate = next(
                (candidate for candidate in reversed(kept) if not candidate.preserve),
                None,
            )
            if pruned_candidate is not None:
                kept.remove(pruned_candidate)
                pruned.append(pruned_candidate)

    if max_total_bytes >= 0:
        kept_bytes = sum(entry.size_bytes for entry in kept)
        for entry in sorted(kept, key=lambda item: (item.created_at, item.key)):
            if kept_bytes <= max_total_bytes:
                break
            if entry.preserve:
                continue
            kept.remove(entry)
            pruned.append(entry)
            kept_bytes -= entry.size_bytes

    pruned_paths = tuple(
        str(entry.path) for entry in sorted(pruned, key=lambda e: e.key)
    )
    if not dry_run:
        for entry in pruned:
            _remove_tree(entry.path)
        if pruned:
            roots = {entry.path.parent for entry in pruned}
            for root in roots:
                _prune_empty_dirs(root)

    bytes_after = sum(entry.size_bytes for entry in kept)
    return ArchivePruneSummary(
        kept=len(kept),
        pruned=len(pruned),
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        pruned_paths=pruned_paths,
    )


def prune_worktree_archive_root(
    archive_root: Path,
    *,
    policy: WorktreeArchiveRetentionPolicy,
    preserve_paths: Iterable[Path] = (),
    dry_run: bool = False,
) -> ArchivePruneSummary:
    if not archive_root.exists() or not archive_root.is_dir():
        return ArchivePruneSummary(
            kept=0,
            pruned=0,
            bytes_before=0,
            bytes_after=0,
            pruned_paths=(),
        )

    preserved = {path.resolve() for path in preserve_paths}
    entries: list[_ArchiveEntry] = []
    for worktree_dir in sorted(archive_root.iterdir(), key=lambda path: path.name):
        if not worktree_dir.is_dir():
            continue
        snapshots = [
            path
            for path in sorted(worktree_dir.iterdir(), key=lambda item: item.name)
            if path.is_dir()
        ]
        for snapshot_dir in snapshots:
            if not _has_snapshot_meta(snapshot_dir):
                continue
            created_at = _load_meta_created_at(snapshot_dir) or _fallback_created_at(
                snapshot_dir
            )
            entries.append(
                _ArchiveEntry(
                    key=f"{worktree_dir.name}/{snapshot_dir.name}",
                    path=snapshot_dir,
                    created_at=created_at,
                    size_bytes=_path_size_bytes(snapshot_dir),
                    preserve=snapshot_dir.resolve() in preserved,
                )
            )

    grouped: dict[str, list[_ArchiveEntry]] = {}
    for entry in entries:
        worktree_id, _, _snapshot_id = entry.key.partition("/")
        grouped.setdefault(worktree_id, []).append(entry)

    kept_after_group_prune: list[_ArchiveEntry] = []
    grouped_pruned: list[_ArchiveEntry] = []
    for worktree_id in sorted(grouped):
        summary = _prune_entries(
            grouped[worktree_id],
            max_keep=policy.max_snapshots_per_repo,
            max_age_days=policy.max_age_days,
            max_total_bytes=-1,
            dry_run=True,
        )
        pruned_paths = set(summary.pruned_paths)
        for entry in grouped[worktree_id]:
            if str(entry.path) in pruned_paths:
                grouped_pruned.append(entry)
            else:
                kept_after_group_prune.append(entry)

    final_summary = _prune_entries(
        kept_after_group_prune,
        max_keep=-1,
        max_age_days=-1,
        max_total_bytes=policy.max_total_bytes,
        dry_run=True,
    )
    final_pruned_paths = set(final_summary.pruned_paths)
    all_pruned = grouped_pruned + [
        entry
        for entry in kept_after_group_prune
        if str(entry.path) in final_pruned_paths
    ]
    final_kept = [
        entry
        for entry in kept_after_group_prune
        if str(entry.path) not in final_pruned_paths
    ]

    if not dry_run:
        for entry in all_pruned:
            _remove_tree(entry.path)
        _prune_empty_dirs(archive_root)

    return ArchivePruneSummary(
        kept=len(final_kept),
        pruned=len(all_pruned),
        bytes_before=sum(entry.size_bytes for entry in entries),
        bytes_after=sum(entry.size_bytes for entry in final_kept),
        pruned_paths=tuple(
            str(entry.path) for entry in sorted(all_pruned, key=lambda e: e.key)
        ),
    )


def prune_run_archive_root(
    archive_root: Path,
    *,
    policy: RunArchiveRetentionPolicy,
    preserve_paths: Iterable[Path] = (),
    dry_run: bool = False,
) -> ArchivePruneSummary:
    if not archive_root.exists() or not archive_root.is_dir():
        return ArchivePruneSummary(
            kept=0,
            pruned=0,
            bytes_before=0,
            bytes_after=0,
            pruned_paths=(),
        )

    preserved = {path.resolve() for path in preserve_paths}
    entries: list[_ArchiveEntry] = []
    for run_dir in sorted(archive_root.iterdir(), key=lambda path: path.name):
        if not run_dir.is_dir():
            continue
        entries.append(
            _ArchiveEntry(
                key=run_dir.name,
                path=run_dir,
                created_at=_fallback_created_at(run_dir),
                size_bytes=_path_size_bytes(run_dir),
                preserve=run_dir.resolve() in preserved,
            )
        )

    return _prune_entries(
        entries,
        max_keep=policy.max_entries,
        max_age_days=policy.max_age_days,
        max_total_bytes=policy.max_total_bytes,
        dry_run=dry_run,
    )
