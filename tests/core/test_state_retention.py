from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.archive_retention import ArchivePruneSummary
from codex_autorunner.core.filebox_retention import FileBoxPruneSummary
from codex_autorunner.core.report_retention import PruneSummary
from codex_autorunner.core.state_retention import (
    CleanupAction,
    CleanupCandidate,
    CleanupReason,
    RetentionBucket,
    RetentionClass,
    RetentionScope,
    adapt_archive_prune_summary_to_plan,
    adapt_archive_prune_summary_to_result,
    adapt_filebox_prune_summary_to_plan,
    adapt_filebox_prune_summary_to_result,
    adapt_report_prune_summary_to_plan,
    adapt_report_prune_summary_to_result,
    aggregate_cleanup_plans,
    aggregate_cleanup_results,
    make_cleanup_plan,
    make_cleanup_result,
)


def test_retention_bucket_is_hashable() -> None:
    bucket = RetentionBucket(
        family="worktree_archives",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.REVIEWABLE,
    )
    assert hash(bucket) is not None
    assert bucket in {bucket}


def test_cleanup_candidate_is_frozen() -> None:
    candidate = CleanupCandidate(
        path=Path("/tmp/test.txt"),
        size_bytes=1024,
        bucket=RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        ),
        action=CleanupAction.PRUNE,
        reason=CleanupReason.AGE_LIMIT,
    )
    assert candidate.path == Path("/tmp/test.txt")
    assert candidate.size_bytes == 1024


def test_make_cleanup_plan_groups_candidates_by_action() -> None:
    bucket = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    candidates = [
        CleanupCandidate(
            path=Path("/tmp/log1.txt"),
            size_bytes=100,
            bucket=bucket,
            action=CleanupAction.KEEP,
            reason=CleanupReason.POLICY_DISABLED,
        ),
        CleanupCandidate(
            path=Path("/tmp/log2.txt"),
            size_bytes=200,
            bucket=bucket,
            action=CleanupAction.PRUNE,
            reason=CleanupReason.AGE_LIMIT,
        ),
        CleanupCandidate(
            path=Path("/tmp/log3.txt"),
            size_bytes=300,
            bucket=bucket,
            action=CleanupAction.SKIP_BLOCKED,
            reason=CleanupReason.ACTIVE_RUN_GUARD,
        ),
    ]

    plan = make_cleanup_plan(bucket, candidates)

    assert plan.total_bytes == 600
    assert plan.reclaimable_bytes == 200
    assert plan.kept_count == 1
    assert plan.prune_count == 1
    assert plan.blocked_count == 1
    assert len(plan.prune_candidates) == 1
    assert len(plan.blocked_candidates) == 1


def test_make_cleanup_plan_with_empty_candidates() -> None:
    bucket = RetentionBucket(
        family="empty",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )

    plan = make_cleanup_plan(bucket, [])

    assert plan.total_bytes == 0
    assert plan.reclaimable_bytes == 0
    assert plan.kept_count == 0
    assert plan.prune_count == 0
    assert plan.blocked_count == 0


def test_make_cleanup_result_calculates_kept_bytes() -> None:
    bucket = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    candidates = [
        CleanupCandidate(
            path=Path("/tmp/log1.txt"),
            size_bytes=100,
            bucket=bucket,
            action=CleanupAction.KEEP,
            reason=CleanupReason.POLICY_DISABLED,
        ),
        CleanupCandidate(
            path=Path("/tmp/log2.txt"),
            size_bytes=200,
            bucket=bucket,
            action=CleanupAction.PRUNE,
            reason=CleanupReason.AGE_LIMIT,
        ),
    ]
    plan = make_cleanup_plan(bucket, candidates)

    result = make_cleanup_result(
        plan,
        deleted_paths=[Path("/tmp/log2.txt")],
        deleted_bytes=200,
    )

    assert result.bucket == bucket
    assert result.plan == plan
    assert result.deleted_count == 1
    assert result.deleted_bytes == 200
    assert result.kept_bytes == 100
    assert result.success is True
    assert len(result.errors) == 0


def test_make_cleanup_result_tracks_errors() -> None:
    bucket = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    plan = make_cleanup_plan(bucket, [])

    result = make_cleanup_result(
        plan,
        deleted_paths=[],
        deleted_bytes=0,
        errors=["Permission denied: /tmp/locked.txt"],
    )

    assert result.success is False
    assert len(result.errors) == 1
    assert "Permission denied" in result.errors[0]


def test_aggregate_cleanup_plans_sums_totals() -> None:
    bucket1 = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    bucket2 = RetentionBucket(
        family="archives",
        scope=RetentionScope.HUB,
        retention_class=RetentionClass.REVIEWABLE,
    )

    plan1 = make_cleanup_plan(
        bucket1,
        [
            CleanupCandidate(
                path=Path("/tmp/a.txt"),
                size_bytes=100,
                bucket=bucket1,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.AGE_LIMIT,
            ),
        ],
    )
    plan2 = make_cleanup_plan(
        bucket2,
        [
            CleanupCandidate(
                path=Path("/tmp/b.txt"),
                size_bytes=200,
                bucket=bucket2,
                action=CleanupAction.KEEP,
                reason=CleanupReason.STABLE_OUTPUT_GUARD,
            ),
            CleanupCandidate(
                path=Path("/tmp/c.txt"),
                size_bytes=300,
                bucket=bucket2,
                action=CleanupAction.SKIP_BLOCKED,
                reason=CleanupReason.ACTIVE_RUN_GUARD,
            ),
        ],
    )

    aggregated = aggregate_cleanup_plans([plan1, plan2])

    assert aggregated.total_candidates == 3
    assert aggregated.total_reclaimable_bytes == 100
    assert aggregated.total_kept_count == 1
    assert aggregated.total_prune_count == 1
    assert aggregated.total_blocked_count == 1


def test_aggregate_cleanup_plans_groups_by_scope() -> None:
    repo_bucket = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    hub_bucket = RetentionBucket(
        family="archives",
        scope=RetentionScope.HUB,
        retention_class=RetentionClass.REVIEWABLE,
    )
    global_bucket = RetentionBucket(
        family="workspaces",
        scope=RetentionScope.GLOBAL,
        retention_class=RetentionClass.EPHEMERAL,
    )

    plan1 = make_cleanup_plan(repo_bucket, [])
    plan2 = make_cleanup_plan(hub_bucket, [])
    plan3 = make_cleanup_plan(global_bucket, [])

    aggregated = aggregate_cleanup_plans([plan1, plan2, plan3])

    by_scope = aggregated.by_scope()
    assert RetentionScope.REPO in by_scope
    assert RetentionScope.HUB in by_scope
    assert RetentionScope.GLOBAL in by_scope
    assert by_scope[RetentionScope.REPO].total_candidates == 0
    assert by_scope[RetentionScope.HUB].total_candidates == 0


def test_aggregate_cleanup_plans_groups_by_retention_class() -> None:
    ephemeral_bucket = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    reviewable_bucket = RetentionBucket(
        family="archives",
        scope=RetentionScope.HUB,
        retention_class=RetentionClass.REVIEWABLE,
    )

    plan1 = make_cleanup_plan(ephemeral_bucket, [])
    plan2 = make_cleanup_plan(reviewable_bucket, [])

    aggregated = aggregate_cleanup_plans([plan1, plan2])

    by_class = aggregated.by_retention_class()
    assert RetentionClass.EPHEMERAL in by_class
    assert RetentionClass.REVIEWABLE in by_class
    assert RetentionClass.DURABLE not in by_class


def test_aggregate_cleanup_plans_groups_by_family() -> None:
    logs_bucket = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    archives_bucket = RetentionBucket(
        family="archives",
        scope=RetentionScope.HUB,
        retention_class=RetentionClass.REVIEWABLE,
    )

    plan1 = make_cleanup_plan(logs_bucket, [])
    plan2 = make_cleanup_plan(archives_bucket, [])

    aggregated = aggregate_cleanup_plans([plan1, plan2])

    by_family = aggregated.by_family()
    assert "logs" in by_family
    assert "archives" in by_family


def test_aggregate_cleanup_results_sums_totals() -> None:
    bucket = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    plan = make_cleanup_plan(bucket, [])

    result1 = make_cleanup_result(plan, [Path("/tmp/a.txt")], deleted_bytes=100)
    result2 = make_cleanup_result(
        plan,
        [Path("/tmp/b.txt"), Path("/tmp/c.txt")],
        deleted_bytes=300,
        errors=["error1"],
    )

    aggregated = aggregate_cleanup_results([result1, result2])

    assert aggregated.total_deleted_count == 3
    assert aggregated.total_deleted_bytes == 400
    assert aggregated.has_errors is True
    assert len(aggregated.all_errors) == 1


def test_aggregate_cleanup_results_by_scope() -> None:
    repo_bucket = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    hub_bucket = RetentionBucket(
        family="archives",
        scope=RetentionScope.HUB,
        retention_class=RetentionClass.REVIEWABLE,
    )

    plan1 = make_cleanup_plan(repo_bucket, [])
    plan2 = make_cleanup_plan(hub_bucket, [])

    result1 = make_cleanup_result(plan1, [Path("/tmp/a.txt")], deleted_bytes=100)
    result2 = make_cleanup_result(plan2, [Path("/tmp/b.txt")], deleted_bytes=200)

    aggregated = aggregate_cleanup_results([result1, result2])

    by_scope = aggregated.by_scope()
    assert by_scope[RetentionScope.REPO].total_deleted_bytes == 100
    assert by_scope[RetentionScope.HUB].total_deleted_bytes == 200


def test_aggregate_cleanup_results_by_retention_class() -> None:
    ephemeral_bucket = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    reviewable_bucket = RetentionBucket(
        family="archives",
        scope=RetentionScope.HUB,
        retention_class=RetentionClass.REVIEWABLE,
    )

    plan1 = make_cleanup_plan(ephemeral_bucket, [])
    plan2 = make_cleanup_plan(reviewable_bucket, [])

    result1 = make_cleanup_result(plan1, [Path("/tmp/a.txt")], deleted_bytes=100)
    result2 = make_cleanup_result(plan2, [Path("/tmp/b.txt")], deleted_bytes=200)

    aggregated = aggregate_cleanup_results([result1, result2])

    by_class = aggregated.by_retention_class()
    assert by_class[RetentionClass.EPHEMERAL].total_deleted_bytes == 100
    assert by_class[RetentionClass.REVIEWABLE].total_deleted_bytes == 200


def test_cleanup_plan_prune_and_blocked_candidates_properties() -> None:
    bucket = RetentionBucket(
        family="test",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    candidates = [
        CleanupCandidate(
            path=Path("/tmp/keep.txt"),
            size_bytes=50,
            bucket=bucket,
            action=CleanupAction.KEEP,
            reason=CleanupReason.STABLE_OUTPUT_GUARD,
        ),
        CleanupCandidate(
            path=Path("/tmp/prune1.txt"),
            size_bytes=100,
            bucket=bucket,
            action=CleanupAction.PRUNE,
            reason=CleanupReason.AGE_LIMIT,
        ),
        CleanupCandidate(
            path=Path("/tmp/blocked.txt"),
            size_bytes=150,
            bucket=bucket,
            action=CleanupAction.SKIP_BLOCKED,
            reason=CleanupReason.ACTIVE_RUN_GUARD,
        ),
        CleanupCandidate(
            path=Path("/tmp/prune2.txt"),
            size_bytes=200,
            bucket=bucket,
            action=CleanupAction.PRUNE,
            reason=CleanupReason.BYTE_BUDGET,
        ),
    ]

    plan = make_cleanup_plan(bucket, candidates)

    assert len(plan.prune_candidates) == 2
    assert len(plan.blocked_candidates) == 1
    assert plan.reclaimable_bytes == 300


def test_adapt_archive_prune_summary_to_plan() -> None:
    summary = ArchivePruneSummary(
        kept=5,
        pruned=3,
        bytes_before=1000,
        bytes_after=400,
        pruned_paths=("/tmp/a.txt", "/tmp/b.txt", "/tmp/c.txt"),
    )
    bucket = RetentionBucket(
        family="worktree_archives",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.REVIEWABLE,
    )

    plan = adapt_archive_prune_summary_to_plan(summary, bucket)

    assert plan.bucket == bucket
    assert plan.total_bytes == 1000
    assert plan.reclaimable_bytes == 600
    assert plan.kept_count == 5
    assert plan.prune_count == 3
    assert plan.blocked_count == 0
    assert len(plan.prune_candidates) == 3


def test_adapt_archive_prune_summary_to_result_dry_run() -> None:
    summary = ArchivePruneSummary(
        kept=5,
        pruned=3,
        bytes_before=1000,
        bytes_after=400,
        pruned_paths=("/tmp/a.txt", "/tmp/b.txt", "/tmp/c.txt"),
    )
    bucket = RetentionBucket(
        family="worktree_archives",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.REVIEWABLE,
    )

    result = adapt_archive_prune_summary_to_result(summary, bucket, dry_run=True)

    assert result.deleted_count == 0
    assert result.deleted_bytes == 0
    assert result.kept_bytes == 400
    assert result.success is True


def test_adapt_archive_prune_summary_to_result_executed() -> None:
    summary = ArchivePruneSummary(
        kept=5,
        pruned=3,
        bytes_before=1000,
        bytes_after=400,
        pruned_paths=("/tmp/a.txt", "/tmp/b.txt", "/tmp/c.txt"),
    )
    bucket = RetentionBucket(
        family="worktree_archives",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.REVIEWABLE,
    )

    result = adapt_archive_prune_summary_to_result(summary, bucket, dry_run=False)

    assert result.deleted_count == 3
    assert result.deleted_bytes == 600
    assert result.kept_bytes == 400


def test_adapt_filebox_prune_summary_to_plan() -> None:
    summary = FileBoxPruneSummary(
        inbox_kept=2,
        inbox_pruned=1,
        outbox_kept=3,
        outbox_pruned=2,
        bytes_before=500,
        bytes_after=200,
        pruned_paths=(
            "/tmp/inbox/old.txt",
            "/tmp/outbox/sent1.txt",
            "/tmp/outbox/sent2.txt",
        ),
    )
    bucket = RetentionBucket(
        family="filebox",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )

    plan = adapt_filebox_prune_summary_to_plan(summary, bucket)

    assert plan.bucket == bucket
    assert plan.total_bytes == 500
    assert plan.reclaimable_bytes == 300
    assert plan.kept_count == 5
    assert plan.prune_count == 3
    assert plan.blocked_count == 0


def test_adapt_filebox_prune_summary_to_result_dry_run() -> None:
    summary = FileBoxPruneSummary(
        inbox_kept=2,
        inbox_pruned=1,
        outbox_kept=3,
        outbox_pruned=2,
        bytes_before=500,
        bytes_after=200,
        pruned_paths=("/tmp/inbox/old.txt",),
    )
    bucket = RetentionBucket(
        family="filebox",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )

    result = adapt_filebox_prune_summary_to_result(summary, bucket, dry_run=True)

    assert result.deleted_count == 0
    assert result.deleted_bytes == 0
    assert result.kept_bytes == 200


def test_adapt_filebox_prune_summary_to_result_executed() -> None:
    summary = FileBoxPruneSummary(
        inbox_kept=2,
        inbox_pruned=1,
        outbox_kept=3,
        outbox_pruned=2,
        bytes_before=500,
        bytes_after=200,
        pruned_paths=("/tmp/inbox/old.txt", "/tmp/outbox/sent1.txt"),
    )
    bucket = RetentionBucket(
        family="filebox",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )

    result = adapt_filebox_prune_summary_to_result(summary, bucket, dry_run=False)

    assert result.deleted_count == 2
    assert result.deleted_bytes == 300


def test_adapt_report_prune_summary_to_plan() -> None:
    summary = PruneSummary(
        kept=10,
        pruned=5,
        bytes_before=2000,
        bytes_after=1000,
    )
    bucket = RetentionBucket(
        family="reports",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.REVIEWABLE,
    )

    plan = adapt_report_prune_summary_to_plan(summary, bucket)

    assert plan.bucket == bucket
    assert plan.total_bytes == 2000
    assert plan.reclaimable_bytes == 1000
    assert plan.kept_count == 10
    assert plan.prune_count == 5
    assert plan.blocked_count == 0


def test_adapt_report_prune_summary_to_result() -> None:
    summary = PruneSummary(
        kept=10,
        pruned=5,
        bytes_before=2000,
        bytes_after=1000,
    )
    bucket = RetentionBucket(
        family="reports",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.REVIEWABLE,
    )

    result = adapt_report_prune_summary_to_result(summary, bucket)

    assert result.deleted_count == 5
    assert result.deleted_bytes == 1000
    assert result.kept_bytes == 1000
    assert result.success is True
