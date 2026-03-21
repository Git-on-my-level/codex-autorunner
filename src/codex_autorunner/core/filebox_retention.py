from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Optional

from .filebox import inbox_dir, outbox_dir, outbox_pending_dir, outbox_sent_dir

DEFAULT_FILEBOX_INBOX_MAX_AGE_DAYS = 7
DEFAULT_FILEBOX_OUTBOX_MAX_AGE_DAYS = 7


@dataclass(frozen=True)
class FileBoxRetentionPolicy:
    inbox_max_age_days: int
    outbox_max_age_days: int


@dataclass(frozen=True)
class FileBoxPruneSummary:
    inbox_kept: int
    inbox_pruned: int
    outbox_kept: int
    outbox_pruned: int
    bytes_before: int
    bytes_after: int
    pruned_paths: tuple[str, ...]


@dataclass(frozen=True)
class _FileBoxEntry:
    path: Path
    size_bytes: int
    mtime: datetime


def _coerce_nonnegative_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _policy_config_value(config: object, name: str, default: int) -> int:
    if isinstance(config, Mapping):
        value = config.get(name, default)
    else:
        value = getattr(config, name, default)
    return _coerce_nonnegative_int(value, default)


def resolve_filebox_retention_policy(config: object) -> FileBoxRetentionPolicy:
    return FileBoxRetentionPolicy(
        inbox_max_age_days=_policy_config_value(
            config,
            "filebox_inbox_max_age_days",
            DEFAULT_FILEBOX_INBOX_MAX_AGE_DAYS,
        ),
        outbox_max_age_days=_policy_config_value(
            config,
            "filebox_outbox_max_age_days",
            DEFAULT_FILEBOX_OUTBOX_MAX_AGE_DAYS,
        ),
    )


def prune_filebox_root(
    repo_root: Path,
    *,
    policy: FileBoxRetentionPolicy,
    scope: str = "both",
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> FileBoxPruneSummary:
    scope_value = scope.strip().lower()
    if scope_value not in {"inbox", "outbox", "both"}:
        raise ValueError("scope must be one of: inbox, outbox, both")

    current_time = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    inbox_entries = (
        _collect_entries((inbox_dir(repo_root),))
        if scope_value in {"inbox", "both"}
        else []
    )
    outbox_entries = (
        _collect_entries(
            (
                outbox_dir(repo_root),
                outbox_pending_dir(repo_root),
                outbox_sent_dir(repo_root),
            )
        )
        if scope_value in {"outbox", "both"}
        else []
    )

    inbox_pruned = _prune_entries(
        inbox_entries,
        max_age_days=policy.inbox_max_age_days,
        dry_run=dry_run,
        now=current_time,
    )
    outbox_pruned = _prune_entries(
        outbox_entries,
        max_age_days=policy.outbox_max_age_days,
        dry_run=dry_run,
        now=current_time,
    )

    bytes_before = sum(entry.size_bytes for entry in inbox_entries + outbox_entries)
    bytes_after = bytes_before - sum(
        entry.size_bytes for entry in inbox_pruned + outbox_pruned
    )
    return FileBoxPruneSummary(
        inbox_kept=len(inbox_entries) - len(inbox_pruned),
        inbox_pruned=len(inbox_pruned),
        outbox_kept=len(outbox_entries) - len(outbox_pruned),
        outbox_pruned=len(outbox_pruned),
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        pruned_paths=tuple(
            str(entry.path)
            for entry in sorted(
                inbox_pruned + outbox_pruned,
                key=lambda item: str(item.path),
            )
        ),
    )


def _collect_entries(folders: tuple[Path, ...]) -> list[_FileBoxEntry]:
    collected: list[_FileBoxEntry] = []
    for folder in folders:
        if not folder.exists():
            continue
        try:
            iterator = folder.iterdir()
        except OSError:
            continue
        for path in iterator:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            collected.append(
                _FileBoxEntry(
                    path=path,
                    size_bytes=stat.st_size,
                    mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
    return collected


def _prune_entries(
    entries: list[_FileBoxEntry],
    *,
    max_age_days: int,
    dry_run: bool,
    now: datetime,
) -> list[_FileBoxEntry]:
    cutoff = now - timedelta(days=max(0, max_age_days))
    pruned: list[_FileBoxEntry] = []
    for entry in sorted(entries, key=lambda item: (item.mtime, str(item.path))):
        if entry.mtime >= cutoff:
            continue
        if not dry_run:
            try:
                entry.path.unlink()
            except OSError:
                continue
        pruned.append(entry)
    return pruned


__all__ = [
    "DEFAULT_FILEBOX_INBOX_MAX_AGE_DAYS",
    "DEFAULT_FILEBOX_OUTBOX_MAX_AGE_DAYS",
    "FileBoxPruneSummary",
    "FileBoxRetentionPolicy",
    "prune_filebox_root",
    "resolve_filebox_retention_policy",
]
