from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from ...core.state_retention import (
    CleanupAction,
    CleanupCandidate,
    CleanupPlan,
    CleanupReason,
    RetentionBucket,
    RetentionClass,
    RetentionScope,
    adapt_workspace_summary_to_result,
    make_cleanup_plan,
)
from ...core.state_roots import (
    is_within_allowed_root,
    resolve_global_state_root,
    validate_path_within_roots,
)

DEFAULT_WORKSPACE_MAX_AGE_DAYS = 7


@dataclass(frozen=True)
class WorkspaceRetentionPolicy:
    max_age_days: int


@dataclass(frozen=True)
class WorkspacePruneSummary:
    kept: int
    pruned: int
    bytes_before: int
    bytes_after: int
    pruned_paths: tuple[str, ...]
    blocked_paths: tuple[str, ...]
    blocked_reasons: tuple[str, ...]


@dataclass(frozen=True)
class _WorkspaceEntry:
    workspace_id: str
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


def resolve_workspace_retention_policy(config: object) -> WorkspaceRetentionPolicy:
    return WorkspaceRetentionPolicy(
        max_age_days=_policy_config_value(
            config,
            "app_server_workspace_max_age_days",
            DEFAULT_WORKSPACE_MAX_AGE_DAYS,
        ),
    )


def resolve_global_workspace_root(
    *, config: object | None = None, repo_root: Optional[Path] = None
) -> Path:
    return resolve_global_state_root(config=config, repo_root=repo_root) / "workspaces"


def resolve_repo_workspace_root(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "app_server_workspaces"


def _walk_tree_metadata(path: Path) -> tuple[datetime | None, int]:
    latest_timestamp: float | None = None
    total_size = 0

    try:
        root_stat = path.stat()
        if latest_timestamp is None or root_stat.st_mtime > latest_timestamp:
            latest_timestamp = root_stat.st_mtime
        if path.is_file():
            total_size += root_stat.st_size
    except OSError:
        pass

    if path.is_dir():
        try:
            for child in path.rglob("*"):
                try:
                    child_stat = child.stat()
                except OSError:
                    continue
                if latest_timestamp is None or child_stat.st_mtime > latest_timestamp:
                    latest_timestamp = child_stat.st_mtime
                if child.is_file():
                    total_size += child_stat.st_size
        except OSError:
            pass

    latest_mtime = (
        datetime.fromtimestamp(latest_timestamp, tz=timezone.utc)
        if latest_timestamp is not None
        else None
    )
    return latest_mtime, total_size


def _collect_workspace_entries(root: Path) -> list[_WorkspaceEntry]:
    if not root.exists() or not root.is_dir():
        return []
    entries: list[_WorkspaceEntry] = []
    try:
        iterator = root.iterdir()
    except OSError:
        return []
    for path in iterator:
        if not path.is_dir():
            continue
        latest_activity, size_bytes = _walk_tree_metadata(path)
        if latest_activity is None:
            continue
        entries.append(
            _WorkspaceEntry(
                workspace_id=path.name,
                path=path,
                size_bytes=size_bytes,
                mtime=latest_activity,
            )
        )
    return entries


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path)


def _build_cleanup_candidates(
    entries: list[_WorkspaceEntry],
    *,
    policy: WorkspaceRetentionPolicy,
    active_workspace_ids: set[str],
    locked_workspace_ids: set[str],
    current_workspace_ids: set[str],
    now: datetime,
    allowed_root: Path,
    scope: RetentionScope,
) -> list[CleanupCandidate]:
    bucket = RetentionBucket(
        family="workspaces",
        scope=scope,
        retention_class=RetentionClass.EPHEMERAL,
    )
    cutoff = now - timedelta(days=max(0, policy.max_age_days))
    candidates: list[CleanupCandidate] = []

    for entry in sorted(entries, key=lambda e: (e.mtime, e.workspace_id)):
        path = entry.path
        if not is_within_allowed_root(path, allowed_roots=[allowed_root], resolve=True):
            candidates.append(
                CleanupCandidate(
                    path=path,
                    size_bytes=entry.size_bytes,
                    bucket=bucket,
                    action=CleanupAction.SKIP_BLOCKED,
                    reason=CleanupReason.CANONICAL_STORE_GUARD,
                )
            )
            continue

        if entry.workspace_id in active_workspace_ids:
            candidates.append(
                CleanupCandidate(
                    path=path,
                    size_bytes=entry.size_bytes,
                    bucket=bucket,
                    action=CleanupAction.SKIP_BLOCKED,
                    reason=CleanupReason.LIVE_WORKSPACE_GUARD,
                )
            )
            continue

        if entry.workspace_id in locked_workspace_ids:
            candidates.append(
                CleanupCandidate(
                    path=path,
                    size_bytes=entry.size_bytes,
                    bucket=bucket,
                    action=CleanupAction.SKIP_BLOCKED,
                    reason=CleanupReason.LOCK_GUARD,
                )
            )
            continue

        if entry.workspace_id in current_workspace_ids:
            candidates.append(
                CleanupCandidate(
                    path=path,
                    size_bytes=entry.size_bytes,
                    bucket=bucket,
                    action=CleanupAction.SKIP_BLOCKED,
                    reason=CleanupReason.ACTIVE_RUN_GUARD,
                )
            )
            continue

        if entry.mtime >= cutoff:
            candidates.append(
                CleanupCandidate(
                    path=path,
                    size_bytes=entry.size_bytes,
                    bucket=bucket,
                    action=CleanupAction.KEEP,
                    reason=CleanupReason.PRESERVE_REQUESTED,
                )
            )
            continue

        candidates.append(
            CleanupCandidate(
                path=path,
                size_bytes=entry.size_bytes,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.STALE_WORKSPACE,
            )
        )

    return candidates


WorkspaceIdProvider = Callable[[], set[str]]


def plan_workspace_retention(
    workspace_root: Path,
    *,
    policy: WorkspaceRetentionPolicy,
    active_workspace_ids: Iterable[str],
    locked_workspace_ids: Iterable[str],
    current_workspace_ids: Iterable[str],
    now: Optional[datetime] = None,
    scope: RetentionScope = RetentionScope.GLOBAL,
) -> CleanupPlan:
    current_time = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    entries = _collect_workspace_entries(workspace_root)
    active_set = set(active_workspace_ids)
    locked_set = set(locked_workspace_ids)
    current_set = set(current_workspace_ids)

    candidates = _build_cleanup_candidates(
        entries,
        policy=policy,
        active_workspace_ids=active_set,
        locked_workspace_ids=locked_set,
        current_workspace_ids=current_set,
        now=current_time,
        allowed_root=workspace_root,
        scope=scope,
    )

    bucket = RetentionBucket(
        family="workspaces",
        scope=scope,
        retention_class=RetentionClass.EPHEMERAL,
    )
    return make_cleanup_plan(bucket, candidates)


def execute_workspace_retention(
    plan: CleanupPlan,
    *,
    workspace_root: Path,
    dry_run: bool = False,
) -> WorkspacePruneSummary:
    validate_path_within_roots(
        workspace_root, allowed_roots=[workspace_root], resolve=False
    )

    pruned_paths: list[str] = []
    blocked_paths: list[str] = []
    blocked_reasons: list[str] = []
    deleted_bytes = 0
    failed_prune_count = 0

    for candidate in plan.prune_candidates:
        path = candidate.path
        try:
            validate_path_within_roots(path, allowed_roots=[workspace_root])
        except (ValueError, OSError):
            blocked_paths.append(str(path))
            blocked_reasons.append("path_outside_root")
            failed_prune_count += 1
            continue

        if not dry_run:
            try:
                _remove_tree(path)
            except OSError:
                blocked_paths.append(str(path))
                blocked_reasons.append("deletion_failed")
                failed_prune_count += 1
                continue
            deleted_bytes += candidate.size_bytes

        pruned_paths.append(str(path))

    for candidate in plan.blocked_candidates:
        blocked_paths.append(str(candidate.path))
        reason = (
            candidate.reason.value
            if hasattr(candidate.reason, "value")
            else str(candidate.reason)
        )
        blocked_reasons.append(reason)

    kept_count = plan.kept_count + len(plan.blocked_candidates) + failed_prune_count
    bytes_after = (
        plan.total_bytes - plan.reclaimable_bytes
        if dry_run
        else plan.total_bytes - deleted_bytes
    )

    return WorkspacePruneSummary(
        kept=kept_count,
        pruned=len(pruned_paths),
        bytes_before=plan.total_bytes,
        bytes_after=bytes_after,
        pruned_paths=tuple(pruned_paths),
        blocked_paths=tuple(blocked_paths),
        blocked_reasons=tuple(blocked_reasons),
    )


def prune_workspace_root(
    workspace_root: Path,
    *,
    policy: WorkspaceRetentionPolicy,
    active_workspace_ids: Iterable[str],
    locked_workspace_ids: Iterable[str],
    current_workspace_ids: Iterable[str],
    dry_run: bool = False,
    now: Optional[datetime] = None,
    scope: RetentionScope = RetentionScope.GLOBAL,
) -> WorkspacePruneSummary:
    plan = plan_workspace_retention(
        workspace_root,
        policy=policy,
        active_workspace_ids=active_workspace_ids,
        locked_workspace_ids=locked_workspace_ids,
        current_workspace_ids=current_workspace_ids,
        now=now,
        scope=scope,
    )
    return execute_workspace_retention(
        plan, workspace_root=workspace_root, dry_run=dry_run
    )


__all__ = [
    "DEFAULT_WORKSPACE_MAX_AGE_DAYS",
    "CleanupAction",
    "CleanupCandidate",
    "CleanupReason",
    "WorkspacePruneSummary",
    "WorkspaceRetentionPolicy",
    "adapt_workspace_summary_to_result",
    "execute_workspace_retention",
    "plan_workspace_retention",
    "prune_workspace_root",
    "resolve_global_workspace_root",
    "resolve_repo_workspace_root",
    "resolve_workspace_retention_policy",
]
