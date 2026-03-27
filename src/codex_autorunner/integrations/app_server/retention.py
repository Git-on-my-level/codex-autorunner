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
    CleanupResult,
    RetentionBucket,
    RetentionClass,
    RetentionScope,
    make_cleanup_plan,
    make_cleanup_result,
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
        try:
            stat = path.stat()
        except OSError:
            continue
        size_bytes = _path_size_bytes(path)
        entries.append(
            _WorkspaceEntry(
                workspace_id=path.name,
                path=path,
                size_bytes=size_bytes,
                mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        )
    return entries


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
        except Exception:
            blocked_paths.append(str(path))
            blocked_reasons.append("path_outside_root")
            failed_prune_count += 1
            continue

        size_bytes = _path_size_bytes(path)
        if not dry_run:
            try:
                _remove_tree(path)
            except OSError:
                blocked_paths.append(str(path))
                blocked_reasons.append("deletion_failed")
                failed_prune_count += 1
                continue
            deleted_bytes += size_bytes

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


def adapt_workspace_summary_to_result(
    summary: WorkspacePruneSummary,
    bucket: RetentionBucket,
    dry_run: bool = False,
) -> CleanupResult:
    candidates: list[CleanupCandidate] = []
    for path_str in summary.pruned_paths:
        candidates.append(
            CleanupCandidate(
                path=Path(path_str),
                size_bytes=0,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.STALE_WORKSPACE,
            )
        )
    for i, path_str in enumerate(summary.blocked_paths):
        reason_str = (
            summary.blocked_reasons[i]
            if i < len(summary.blocked_reasons)
            else "unknown"
        )
        reason = _map_reason_str_to_cleanup_reason(reason_str)
        candidates.append(
            CleanupCandidate(
                path=Path(path_str),
                size_bytes=0,
                bucket=bucket,
                action=CleanupAction.SKIP_BLOCKED,
                reason=reason,
            )
        )

    plan = CleanupPlan(
        bucket=bucket,
        candidates=tuple(candidates),
        total_bytes=summary.bytes_before,
        reclaimable_bytes=summary.bytes_before - summary.bytes_after,
        kept_count=summary.kept,
        prune_count=summary.pruned,
        blocked_count=len(summary.blocked_paths),
    )
    deleted_paths = tuple(Path(p) for p in summary.pruned_paths) if not dry_run else ()
    deleted_bytes = 0 if dry_run else (summary.bytes_before - summary.bytes_after)

    return make_cleanup_result(
        plan,
        deleted_paths=deleted_paths,
        deleted_bytes=deleted_bytes,
    )


def _map_reason_str_to_cleanup_reason(reason_str: str) -> CleanupReason:
    mapping = {
        "live_workspace_guard": CleanupReason.LIVE_WORKSPACE_GUARD,
        "lock_guard": CleanupReason.LOCK_GUARD,
        "active_run_guard": CleanupReason.ACTIVE_RUN_GUARD,
        "canonical_store_guard": CleanupReason.CANONICAL_STORE_GUARD,
        "path_outside_root": CleanupReason.CANONICAL_STORE_GUARD,
        "deletion_failed": CleanupReason.CANONICAL_STORE_GUARD,
    }
    return mapping.get(reason_str, CleanupReason.CANONICAL_STORE_GUARD)


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
