from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, Optional

if TYPE_CHECKING:
    from .archive_retention import ArchivePruneSummary
    from .filebox_retention import FileBoxPruneSummary
    from .report_retention import PruneSummary


class RetentionScope(str, Enum):
    REPO = "repo"
    HUB = "hub"
    GLOBAL = "global"


class RetentionClass(str, Enum):
    DURABLE = "durable"
    REVIEWABLE = "reviewable"
    EPHEMERAL = "ephemeral"
    CACHE_ONLY = "cache-only"


class CleanupAction(str, Enum):
    KEEP = "keep"
    PRUNE = "prune"
    COMPACT = "compact"
    ARCHIVE_THEN_PRUNE = "archive_then_prune"
    SKIP_BLOCKED = "skip_blocked"


class CleanupReason(str, Enum):
    AGE_LIMIT = "age_limit"
    COUNT_LIMIT = "count_limit"
    BYTE_BUDGET = "byte_budget"
    RESOLVED_STAGING = "resolved_staging"
    STALE_WORKSPACE = "stale_workspace"
    CACHE_REBUILDABLE = "cache_rebuildable"
    ACTIVE_RUN_GUARD = "active_run_guard"
    LOCK_GUARD = "lock_guard"
    LIVE_WORKSPACE_GUARD = "live_workspace_guard"
    CANONICAL_STORE_GUARD = "canonical_store_guard"
    STABLE_OUTPUT_GUARD = "stable_output_guard"
    PRESERVE_REQUESTED = "preserve_requested"
    POLICY_DISABLED = "policy_disabled"
    NO_CANDIDATES = "no_candidates"


@dataclass(frozen=True)
class RetentionBucket:
    family: str
    scope: RetentionScope
    retention_class: RetentionClass


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    size_bytes: int
    bucket: RetentionBucket
    action: CleanupAction
    reason: CleanupReason


@dataclass(frozen=True)
class CleanupPlan:
    bucket: RetentionBucket
    candidates: tuple[CleanupCandidate, ...]
    total_bytes: int
    reclaimable_bytes: int
    kept_count: int
    prune_count: int
    blocked_count: int

    @property
    def blocked_candidates(self) -> tuple[CleanupCandidate, ...]:
        return tuple(
            c for c in self.candidates if c.action == CleanupAction.SKIP_BLOCKED
        )

    @property
    def prune_candidates(self) -> tuple[CleanupCandidate, ...]:
        return tuple(c for c in self.candidates if c.action == CleanupAction.PRUNE)


@dataclass(frozen=True)
class CleanupResult:
    bucket: RetentionBucket
    plan: CleanupPlan
    deleted_paths: tuple[Path, ...]
    deleted_count: int
    deleted_bytes: int
    kept_bytes: int
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


@dataclass(frozen=True)
class AggregatedCleanupPlan:
    plans: tuple[CleanupPlan, ...]

    @property
    def total_candidates(self) -> int:
        return sum(len(p.candidates) for p in self.plans)

    @property
    def total_reclaimable_bytes(self) -> int:
        return sum(p.reclaimable_bytes for p in self.plans)

    @property
    def total_kept_count(self) -> int:
        return sum(p.kept_count for p in self.plans)

    @property
    def total_prune_count(self) -> int:
        return sum(p.prune_count for p in self.plans)

    @property
    def total_blocked_count(self) -> int:
        return sum(p.blocked_count for p in self.plans)

    def by_scope(self) -> Mapping[RetentionScope, AggregatedCleanupPlan]:
        grouped: dict[RetentionScope, list[CleanupPlan]] = {}
        for plan in self.plans:
            grouped.setdefault(plan.bucket.scope, []).append(plan)
        return {
            scope: AggregatedCleanupPlan(plans=tuple(scope_plans))
            for scope, scope_plans in grouped.items()
        }

    def by_retention_class(self) -> Mapping[RetentionClass, AggregatedCleanupPlan]:
        grouped: dict[RetentionClass, list[CleanupPlan]] = {}
        for plan in self.plans:
            grouped.setdefault(plan.bucket.retention_class, []).append(plan)
        return {
            rc: AggregatedCleanupPlan(plans=tuple(rc_plans))
            for rc, rc_plans in grouped.items()
        }

    def by_family(self) -> Mapping[str, AggregatedCleanupPlan]:
        grouped: dict[str, list[CleanupPlan]] = {}
        for plan in self.plans:
            grouped.setdefault(plan.bucket.family, []).append(plan)
        return {
            family: AggregatedCleanupPlan(plans=tuple(family_plans))
            for family, family_plans in grouped.items()
        }


@dataclass(frozen=True)
class AggregatedCleanupResult:
    results: tuple[CleanupResult, ...]

    @property
    def total_deleted_count(self) -> int:
        return sum(r.deleted_count for r in self.results)

    @property
    def total_deleted_bytes(self) -> int:
        return sum(r.deleted_bytes for r in self.results)

    @property
    def total_kept_bytes(self) -> int:
        return sum(r.kept_bytes for r in self.results)

    @property
    def has_errors(self) -> bool:
        return any(r.errors for r in self.results)

    @property
    def all_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for r in self.results:
            errors.extend(r.errors)
        return tuple(errors)

    def by_scope(self) -> Mapping[RetentionScope, AggregatedCleanupResult]:
        grouped: dict[RetentionScope, list[CleanupResult]] = {}
        for result in self.results:
            grouped.setdefault(result.bucket.scope, []).append(result)
        return {
            scope: AggregatedCleanupResult(results=tuple(scope_results))
            for scope, scope_results in grouped.items()
        }

    def by_retention_class(self) -> Mapping[RetentionClass, AggregatedCleanupResult]:
        grouped: dict[RetentionClass, list[CleanupResult]] = {}
        for result in self.results:
            grouped.setdefault(result.bucket.retention_class, []).append(result)
        return {
            rc: AggregatedCleanupResult(results=tuple(rc_results))
            for rc, rc_results in grouped.items()
        }


def make_cleanup_plan(
    bucket: RetentionBucket,
    candidates: Iterable[CleanupCandidate],
) -> CleanupPlan:
    candidates_tuple = tuple(candidates)
    total_bytes = sum(c.size_bytes for c in candidates_tuple)
    prune_candidates = [c for c in candidates_tuple if c.action == CleanupAction.PRUNE]
    blocked_candidates = [
        c for c in candidates_tuple if c.action == CleanupAction.SKIP_BLOCKED
    ]
    kept_candidates = [c for c in candidates_tuple if c.action == CleanupAction.KEEP]

    return CleanupPlan(
        bucket=bucket,
        candidates=candidates_tuple,
        total_bytes=total_bytes,
        reclaimable_bytes=sum(c.size_bytes for c in prune_candidates),
        kept_count=len(kept_candidates),
        prune_count=len(prune_candidates),
        blocked_count=len(blocked_candidates),
    )


def make_cleanup_result(
    plan: CleanupPlan,
    deleted_paths: Iterable[Path],
    deleted_bytes: int,
    errors: Optional[Iterable[str]] = None,
) -> CleanupResult:
    deleted_tuple = tuple(deleted_paths)
    kept_bytes = plan.total_bytes - deleted_bytes
    return CleanupResult(
        bucket=plan.bucket,
        plan=plan,
        deleted_paths=deleted_tuple,
        deleted_count=len(deleted_tuple),
        deleted_bytes=deleted_bytes,
        kept_bytes=kept_bytes,
        errors=tuple(errors) if errors is not None else (),
    )


def aggregate_cleanup_plans(plans: Iterable[CleanupPlan]) -> AggregatedCleanupPlan:
    return AggregatedCleanupPlan(plans=tuple(plans))


def aggregate_cleanup_results(
    results: Iterable[CleanupResult],
) -> AggregatedCleanupResult:
    return AggregatedCleanupResult(results=tuple(results))


def adapt_archive_prune_summary_to_plan(
    summary: "ArchivePruneSummary",
    bucket: RetentionBucket,
) -> CleanupPlan:
    from .archive_retention import ArchivePruneSummary

    if not isinstance(summary, ArchivePruneSummary):
        raise TypeError("summary must be an ArchivePruneSummary")

    candidates: list[CleanupCandidate] = []
    for path_str in summary.pruned_paths:
        path = Path(path_str)
        candidates.append(
            CleanupCandidate(
                path=path,
                size_bytes=0,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.AGE_LIMIT,
            )
        )

    return CleanupPlan(
        bucket=bucket,
        candidates=tuple(candidates),
        total_bytes=summary.bytes_before,
        reclaimable_bytes=summary.bytes_before - summary.bytes_after,
        kept_count=summary.kept,
        prune_count=summary.pruned,
        blocked_count=0,
    )


def adapt_archive_prune_summary_to_result(
    summary: "ArchivePruneSummary",
    bucket: RetentionBucket,
    dry_run: bool = False,
) -> CleanupResult:
    plan = adapt_archive_prune_summary_to_plan(summary, bucket)
    deleted_paths = tuple(Path(p) for p in summary.pruned_paths) if not dry_run else ()
    deleted_bytes = 0 if dry_run else (summary.bytes_before - summary.bytes_after)

    return CleanupResult(
        bucket=bucket,
        plan=plan,
        deleted_paths=deleted_paths,
        deleted_count=len(deleted_paths),
        deleted_bytes=deleted_bytes,
        kept_bytes=summary.bytes_after,
        errors=(),
    )


def adapt_filebox_prune_summary_to_plan(
    summary: "FileBoxPruneSummary",
    bucket: RetentionBucket,
) -> CleanupPlan:
    from .filebox_retention import FileBoxPruneSummary

    if not isinstance(summary, FileBoxPruneSummary):
        raise TypeError("summary must be a FileBoxPruneSummary")

    candidates: list[CleanupCandidate] = []
    for path_str in summary.pruned_paths:
        path = Path(path_str)
        candidates.append(
            CleanupCandidate(
                path=path,
                size_bytes=0,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.AGE_LIMIT,
            )
        )

    return CleanupPlan(
        bucket=bucket,
        candidates=tuple(candidates),
        total_bytes=summary.bytes_before,
        reclaimable_bytes=summary.bytes_before - summary.bytes_after,
        kept_count=summary.inbox_kept + summary.outbox_kept,
        prune_count=summary.inbox_pruned + summary.outbox_pruned,
        blocked_count=0,
    )


def adapt_filebox_prune_summary_to_result(
    summary: "FileBoxPruneSummary",
    bucket: RetentionBucket,
    dry_run: bool = False,
) -> CleanupResult:
    plan = adapt_filebox_prune_summary_to_plan(summary, bucket)
    deleted_paths = tuple(Path(p) for p in summary.pruned_paths) if not dry_run else ()
    deleted_bytes = 0 if dry_run else (summary.bytes_before - summary.bytes_after)

    return CleanupResult(
        bucket=bucket,
        plan=plan,
        deleted_paths=deleted_paths,
        deleted_count=len(deleted_paths),
        deleted_bytes=deleted_bytes,
        kept_bytes=summary.bytes_after,
        errors=(),
    )


def adapt_report_prune_summary_to_plan(
    summary: "PruneSummary",
    bucket: RetentionBucket,
) -> CleanupPlan:
    from .report_retention import PruneSummary

    if not isinstance(summary, PruneSummary):
        raise TypeError("summary must be a PruneSummary")

    candidates: list[CleanupCandidate] = []
    for _ in range(summary.pruned):
        candidates.append(
            CleanupCandidate(
                path=Path("<unknown>"),
                size_bytes=0,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.COUNT_LIMIT,
            )
        )

    return CleanupPlan(
        bucket=bucket,
        candidates=tuple(candidates),
        total_bytes=summary.bytes_before,
        reclaimable_bytes=summary.bytes_before - summary.bytes_after,
        kept_count=summary.kept,
        prune_count=summary.pruned,
        blocked_count=0,
    )


def adapt_report_prune_summary_to_result(
    summary: "PruneSummary",
    bucket: RetentionBucket,
    dry_run: bool = False,
) -> CleanupResult:
    plan = adapt_report_prune_summary_to_plan(summary, bucket)
    deleted_count = 0 if dry_run else summary.pruned
    deleted_bytes = 0 if dry_run else (summary.bytes_before - summary.bytes_after)

    return CleanupResult(
        bucket=bucket,
        plan=plan,
        deleted_paths=(),
        deleted_count=deleted_count,
        deleted_bytes=deleted_bytes,
        kept_bytes=summary.bytes_after,
        errors=(),
    )


__all__ = [
    "RetentionScope",
    "RetentionClass",
    "CleanupAction",
    "CleanupReason",
    "RetentionBucket",
    "CleanupCandidate",
    "CleanupPlan",
    "CleanupResult",
    "AggregatedCleanupPlan",
    "AggregatedCleanupResult",
    "make_cleanup_plan",
    "make_cleanup_result",
    "aggregate_cleanup_plans",
    "aggregate_cleanup_results",
    "adapt_archive_prune_summary_to_plan",
    "adapt_archive_prune_summary_to_result",
    "adapt_filebox_prune_summary_to_plan",
    "adapt_filebox_prune_summary_to_result",
    "adapt_report_prune_summary_to_plan",
    "adapt_report_prune_summary_to_result",
]
