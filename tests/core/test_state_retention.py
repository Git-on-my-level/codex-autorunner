from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codex_autorunner.core.archive_retention import ArchivePruneSummary
from codex_autorunner.core.filebox_retention import FileBoxPruneSummary
from codex_autorunner.core.report_retention import PruneSummary
from codex_autorunner.core.state_retention import (
    AggregatedCleanupPlan,
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
    adapt_housekeeping_rule_result_to_plan,
    adapt_housekeeping_rule_result_to_result,
    adapt_report_prune_summary_to_plan,
    adapt_report_prune_summary_to_result,
    adapt_workspace_summary_to_result,
    aggregate_cleanup_results,
    make_cleanup_plan,
    make_cleanup_result,
    summarize_cleanup_plan_lifecycle,
)
from codex_autorunner.housekeeping import HousekeepingRuleResult


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


def test_summarize_cleanup_plan_lifecycle_uses_shared_vocabulary() -> None:
    bucket = RetentionBucket(
        family="logs",
        scope=RetentionScope.REPO,
        retention_class=RetentionClass.EPHEMERAL,
    )
    plan = make_cleanup_plan(
        bucket,
        [
            CleanupCandidate(
                path=Path("/tmp/keep.txt"),
                size_bytes=1,
                bucket=bucket,
                action=CleanupAction.KEEP,
                reason=CleanupReason.POLICY_DISABLED,
            ),
            CleanupCandidate(
                path=Path("/tmp/prune.txt"),
                size_bytes=2,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.AGE_LIMIT,
            ),
            CleanupCandidate(
                path=Path("/tmp/blocked.txt"),
                size_bytes=3,
                bucket=bucket,
                action=CleanupAction.SKIP_BLOCKED,
                reason=CleanupReason.ACTIVE_RUN_GUARD,
            ),
        ],
    )

    summary = summarize_cleanup_plan_lifecycle(plan)
    assert summary["actions"] == {"blocked": 1, "keep": 1, "prune": 1}
    assert summary["reasons"] == {
        "active_run_guard": 1,
        "age_limit": 1,
        "policy_disabled": 1,
    }


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

    aggregated = AggregatedCleanupPlan(plans=(plan1, plan2))

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

    aggregated = AggregatedCleanupPlan(plans=(plan1, plan2, plan3))

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

    aggregated = AggregatedCleanupPlan(plans=(plan1, plan2))

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

    aggregated = AggregatedCleanupPlan(plans=(plan1, plan2))

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


def test_adapt_housekeeping_rule_result_to_plan() -> None:
    summary = HousekeepingRuleResult(
        name="update_cache",
        kind="directory",
        scanned_count=5,
        deleted_count=2,
        deleted_bytes=123,
        deleted_paths=["/tmp/a.txt", "/tmp/b.txt"],
    )
    bucket = RetentionBucket(
        family="update_cache",
        scope=RetentionScope.GLOBAL,
        retention_class=RetentionClass.CACHE_ONLY,
    )

    plan = adapt_housekeeping_rule_result_to_plan(
        summary,
        bucket,
        reason=CleanupReason.CACHE_REBUILDABLE,
    )

    assert plan.bucket == bucket
    assert plan.total_bytes == 123
    assert plan.reclaimable_bytes == 123
    assert plan.kept_count == 3
    assert plan.prune_count == 2
    assert plan.blocked_count == 0


def test_adapt_housekeeping_rule_result_to_result_dry_run() -> None:
    summary = HousekeepingRuleResult(
        name="update_cache",
        kind="directory",
        scanned_count=5,
        deleted_count=2,
        deleted_bytes=123,
        error_samples=["sample error"],
        deleted_paths=["/tmp/a.txt", "/tmp/b.txt"],
    )
    bucket = RetentionBucket(
        family="update_cache",
        scope=RetentionScope.GLOBAL,
        retention_class=RetentionClass.CACHE_ONLY,
    )

    result = adapt_housekeeping_rule_result_to_result(
        summary,
        bucket,
        dry_run=True,
        reason=CleanupReason.CACHE_REBUILDABLE,
    )

    assert result.deleted_count == 0
    assert result.deleted_bytes == 0
    assert result.kept_bytes == 0
    assert result.errors == ("sample error",)


def test_adapt_housekeeping_rule_result_to_result_executed() -> None:
    summary = HousekeepingRuleResult(
        name="update_cache",
        kind="directory",
        scanned_count=5,
        deleted_count=2,
        deleted_bytes=123,
        deleted_paths=["/tmp/a.txt", "/tmp/b.txt"],
    )
    bucket = RetentionBucket(
        family="update_cache",
        scope=RetentionScope.GLOBAL,
        retention_class=RetentionClass.CACHE_ONLY,
    )

    result = adapt_housekeeping_rule_result_to_result(
        summary,
        bucket,
        dry_run=False,
        reason=CleanupReason.CACHE_REBUILDABLE,
    )

    assert result.deleted_count == 2
    assert result.deleted_bytes == 123
    assert result.kept_bytes == 0


def test_adapt_report_prune_summary_to_plan() -> None:
    summary = PruneSummary(
        kept=10,
        pruned=5,
        bytes_before=2000,
        bytes_after=1000,
        pruned_paths=(
            "/tmp/history-00.md",
            "/tmp/history-01.md",
            "/tmp/history-02.md",
            "/tmp/history-03.md",
            "/tmp/history-04.md",
        ),
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
        pruned_paths=(
            "/tmp/history-00.md",
            "/tmp/history-01.md",
            "/tmp/history-02.md",
            "/tmp/history-03.md",
            "/tmp/history-04.md",
        ),
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


class TestAdapterTypeValidation:
    def test_adapt_archive_rejects_wrong_type(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )
        invalid_summary: Any = "not a summary"
        try:
            adapt_archive_prune_summary_to_plan(invalid_summary, bucket)
            raise AssertionError("Should have raised TypeError")
        except TypeError as e:
            assert "ArchivePruneSummary" in str(e)

    def test_adapt_filebox_rejects_wrong_type(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        invalid_summary: Any = {"not": "a summary"}
        try:
            adapt_filebox_prune_summary_to_plan(invalid_summary, bucket)
            raise AssertionError("Should have raised TypeError")
        except TypeError as e:
            assert "FileBoxPruneSummary" in str(e)

    def test_adapt_report_rejects_wrong_type(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )
        invalid_summary: Any = 123
        try:
            adapt_report_prune_summary_to_plan(invalid_summary, bucket)
            raise AssertionError("Should have raised TypeError")
        except TypeError as e:
            assert "PruneSummary" in str(e)


class TestCleanupActionSemantics:
    def test_keep_action_not_counted_as_reclaimable(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        candidates = [
            CleanupCandidate(
                path=Path("/tmp/keep1.txt"),
                size_bytes=1000,
                bucket=bucket,
                action=CleanupAction.KEEP,
                reason=CleanupReason.STABLE_OUTPUT_GUARD,
            ),
            CleanupCandidate(
                path=Path("/tmp/keep2.txt"),
                size_bytes=2000,
                bucket=bucket,
                action=CleanupAction.KEEP,
                reason=CleanupReason.POLICY_DISABLED,
            ),
        ]
        plan = make_cleanup_plan(bucket, candidates)

        assert plan.total_bytes == 3000
        assert plan.reclaimable_bytes == 0
        assert plan.kept_count == 2
        assert plan.prune_count == 0
        assert plan.blocked_count == 0

    def test_blocked_candidates_not_in_reclaimable_bytes(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        candidates = [
            CleanupCandidate(
                path=Path("/tmp/blocked.txt"),
                size_bytes=5000,
                bucket=bucket,
                action=CleanupAction.SKIP_BLOCKED,
                reason=CleanupReason.ACTIVE_RUN_GUARD,
            ),
            CleanupCandidate(
                path=Path("/tmp/prune.txt"),
                size_bytes=1000,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.AGE_LIMIT,
            ),
        ]
        plan = make_cleanup_plan(bucket, candidates)

        assert plan.total_bytes == 6000
        assert plan.reclaimable_bytes == 1000
        assert plan.blocked_count == 1
        assert plan.prune_count == 1

    def test_mixed_actions_computed_correctly(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        candidates = [
            CleanupCandidate(
                path=Path("/tmp/keep.txt"),
                size_bytes=100,
                bucket=bucket,
                action=CleanupAction.KEEP,
                reason=CleanupReason.PRESERVE_REQUESTED,
            ),
            CleanupCandidate(
                path=Path("/tmp/prune1.txt"),
                size_bytes=200,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.AGE_LIMIT,
            ),
            CleanupCandidate(
                path=Path("/tmp/blocked.txt"),
                size_bytes=300,
                bucket=bucket,
                action=CleanupAction.SKIP_BLOCKED,
                reason=CleanupReason.LIVE_WORKSPACE_GUARD,
            ),
            CleanupCandidate(
                path=Path("/tmp/prune2.txt"),
                size_bytes=400,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.BYTE_BUDGET,
            ),
        ]
        plan = make_cleanup_plan(bucket, candidates)

        assert plan.total_bytes == 1000
        assert plan.reclaimable_bytes == 600
        assert plan.kept_count == 1
        assert plan.prune_count == 2
        assert plan.blocked_count == 1
        assert len(plan.prune_candidates) == 2
        assert len(plan.blocked_candidates) == 1


class TestCleanupReasonCoverage:
    def test_all_cleanup_reasons_are_valid(self) -> None:
        reasons = [
            CleanupReason.AGE_LIMIT,
            CleanupReason.COUNT_LIMIT,
            CleanupReason.BYTE_BUDGET,
            CleanupReason.RESOLVED_STAGING,
            CleanupReason.STALE_WORKSPACE,
            CleanupReason.CACHE_REBUILDABLE,
            CleanupReason.ACTIVE_RUN_GUARD,
            CleanupReason.LOCK_GUARD,
            CleanupReason.LIVE_WORKSPACE_GUARD,
            CleanupReason.CANONICAL_STORE_GUARD,
            CleanupReason.STABLE_OUTPUT_GUARD,
            CleanupReason.PRESERVE_REQUESTED,
            CleanupReason.POLICY_DISABLED,
            CleanupReason.NO_CANDIDATES,
        ]

        for reason in reasons:
            candidate = CleanupCandidate(
                path=Path("/tmp/test.txt"),
                size_bytes=100,
                bucket=RetentionBucket(
                    family="test",
                    scope=RetentionScope.REPO,
                    retention_class=RetentionClass.EPHEMERAL,
                ),
                action=CleanupAction.PRUNE,
                reason=reason,
            )
            assert candidate.reason == reason

    def test_guard_reasons_map_to_skip_blocked(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        guard_reasons = [
            CleanupReason.ACTIVE_RUN_GUARD,
            CleanupReason.LOCK_GUARD,
            CleanupReason.LIVE_WORKSPACE_GUARD,
            CleanupReason.CANONICAL_STORE_GUARD,
            CleanupReason.STABLE_OUTPUT_GUARD,
        ]

        for reason in guard_reasons:
            candidate = CleanupCandidate(
                path=Path("/tmp/guarded.txt"),
                size_bytes=100,
                bucket=bucket,
                action=CleanupAction.SKIP_BLOCKED,
                reason=reason,
            )
            assert candidate.action == CleanupAction.SKIP_BLOCKED


class TestAggregationEdgeCases:
    def test_aggregate_empty_plans(self) -> None:
        aggregated = AggregatedCleanupPlan(plans=())

        assert aggregated.total_candidates == 0
        assert aggregated.total_reclaimable_bytes == 0
        assert aggregated.total_kept_count == 0
        assert aggregated.total_prune_count == 0
        assert aggregated.total_blocked_count == 0

    def test_aggregate_empty_results(self) -> None:
        aggregated = aggregate_cleanup_results([])

        assert aggregated.total_deleted_count == 0
        assert aggregated.total_deleted_bytes == 0
        assert aggregated.total_kept_bytes == 0
        assert aggregated.has_errors is False
        assert len(aggregated.all_errors) == 0

    def test_aggregate_single_plan(self) -> None:
        bucket = RetentionBucket(
            family="solo",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        plan = make_cleanup_plan(
            bucket,
            [
                CleanupCandidate(
                    path=Path("/tmp/test.txt"),
                    size_bytes=100,
                    bucket=bucket,
                    action=CleanupAction.PRUNE,
                    reason=CleanupReason.AGE_LIMIT,
                )
            ],
        )

        aggregated = AggregatedCleanupPlan(plans=(plan,))

        assert aggregated.total_candidates == 1
        assert aggregated.total_reclaimable_bytes == 100
        assert aggregated.total_prune_count == 1

    def test_aggregate_results_with_multiple_errors(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        plan = make_cleanup_plan(bucket, [])

        result1 = make_cleanup_result(
            plan, [], deleted_bytes=0, errors=["error1", "error2"]
        )
        result2 = make_cleanup_result(plan, [], deleted_bytes=0, errors=["error3"])

        aggregated = aggregate_cleanup_results([result1, result2])

        assert aggregated.has_errors is True
        assert len(aggregated.all_errors) == 3
        assert "error1" in aggregated.all_errors
        assert "error2" in aggregated.all_errors
        assert "error3" in aggregated.all_errors

    def test_aggregate_by_scope_with_no_matches(self) -> None:
        hub_bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.HUB,
            retention_class=RetentionClass.REVIEWABLE,
        )
        plan = make_cleanup_plan(hub_bucket, [])

        aggregated = AggregatedCleanupPlan(plans=(plan,))
        by_scope = aggregated.by_scope()

        assert RetentionScope.HUB in by_scope
        assert RetentionScope.REPO not in by_scope
        assert RetentionScope.GLOBAL not in by_scope

    def test_aggregate_by_family_groups_correctly(self) -> None:
        bucket1 = RetentionBucket(
            family="family_a",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        bucket2 = RetentionBucket(
            family="family_b",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        bucket3 = RetentionBucket(
            family="family_a",
            scope=RetentionScope.HUB,
            retention_class=RetentionClass.EPHEMERAL,
        )

        plan1 = make_cleanup_plan(
            bucket1,
            [
                CleanupCandidate(
                    path=Path("/tmp/a1.txt"),
                    size_bytes=100,
                    bucket=bucket1,
                    action=CleanupAction.PRUNE,
                    reason=CleanupReason.AGE_LIMIT,
                )
            ],
        )
        plan2 = make_cleanup_plan(
            bucket2,
            [
                CleanupCandidate(
                    path=Path("/tmp/b1.txt"),
                    size_bytes=200,
                    bucket=bucket2,
                    action=CleanupAction.PRUNE,
                    reason=CleanupReason.AGE_LIMIT,
                )
            ],
        )
        plan3 = make_cleanup_plan(
            bucket3,
            [
                CleanupCandidate(
                    path=Path("/tmp/a2.txt"),
                    size_bytes=300,
                    bucket=bucket3,
                    action=CleanupAction.PRUNE,
                    reason=CleanupReason.AGE_LIMIT,
                )
            ],
        )

        aggregated = AggregatedCleanupPlan(plans=(plan1, plan2, plan3))
        by_family = aggregated.by_family()

        assert "family_a" in by_family
        assert "family_b" in by_family
        assert by_family["family_a"].total_candidates == 2
        assert by_family["family_a"].total_reclaimable_bytes == 400
        assert by_family["family_b"].total_candidates == 1
        assert by_family["family_b"].total_reclaimable_bytes == 200


class TestCleanupResultSemantics:
    def test_result_kept_bytes_computed_from_plan(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        candidates = [
            CleanupCandidate(
                path=Path("/tmp/keep.txt"),
                size_bytes=500,
                bucket=bucket,
                action=CleanupAction.KEEP,
                reason=CleanupReason.STABLE_OUTPUT_GUARD,
            ),
            CleanupCandidate(
                path=Path("/tmp/prune.txt"),
                size_bytes=300,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.AGE_LIMIT,
            ),
        ]
        plan = make_cleanup_plan(bucket, candidates)

        result = make_cleanup_result(
            plan,
            deleted_paths=[Path("/tmp/prune.txt")],
            deleted_bytes=300,
        )

        assert result.kept_bytes == 500
        assert result.deleted_bytes == 300
        assert result.plan.total_bytes == 800

    def test_result_with_no_deletions(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        plan = make_cleanup_plan(bucket, [])

        result = make_cleanup_result(plan, deleted_paths=[], deleted_bytes=0)

        assert result.deleted_count == 0
        assert result.deleted_bytes == 0
        assert result.success is True
        assert len(result.errors) == 0

    def test_result_failure_with_errors(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        plan = make_cleanup_plan(bucket, [])

        result = make_cleanup_result(
            plan,
            deleted_paths=[],
            deleted_bytes=0,
            errors=["Failed to delete: permission denied"],
        )

        assert result.success is False
        assert len(result.errors) == 1

    def test_deleted_paths_preserved_in_result(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        plan = make_cleanup_plan(bucket, [])

        paths = [Path("/tmp/a.txt"), Path("/tmp/b.txt"), Path("/tmp/c.txt")]
        result = make_cleanup_result(plan, deleted_paths=paths, deleted_bytes=0)

        assert result.deleted_count == 3
        assert result.deleted_paths == tuple(paths)


class TestRetentionClassAndScope:
    def test_all_retention_classes_are_valid(self) -> None:
        classes = [
            RetentionClass.DURABLE,
            RetentionClass.REVIEWABLE,
            RetentionClass.EPHEMERAL,
            RetentionClass.CACHE_ONLY,
        ]

        for rc in classes:
            bucket = RetentionBucket(
                family="test",
                scope=RetentionScope.REPO,
                retention_class=rc,
            )
            assert bucket.retention_class == rc

    def test_all_retention_scopes_are_valid(self) -> None:
        scopes = [
            RetentionScope.REPO,
            RetentionScope.HUB,
            RetentionScope.GLOBAL,
        ]

        for scope in scopes:
            bucket = RetentionBucket(
                family="test",
                scope=scope,
                retention_class=RetentionClass.EPHEMERAL,
            )
            assert bucket.scope == scope

    def test_bucket_equality_and_hashing(self) -> None:
        bucket1 = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        bucket2 = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        bucket3 = RetentionBucket(
            family="other",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )

        assert bucket1 == bucket2
        assert bucket1 != bucket3
        assert hash(bucket1) == hash(bucket2)
        assert bucket1 in {bucket2}
        assert bucket3 not in {bucket1}


class TestAdapterDryRunExecuteParity:
    def test_archive_adapter_dry_vs_execute_same_plan_different_deletion(self) -> None:
        summary = ArchivePruneSummary(
            kept=3,
            pruned=2,
            bytes_before=1000,
            bytes_after=400,
            pruned_paths=("/tmp/a", "/tmp/b"),
        )
        bucket = RetentionBucket(
            family="worktree_archives",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )

        result_dry = adapt_archive_prune_summary_to_result(
            summary, bucket, dry_run=True
        )
        result_exec = adapt_archive_prune_summary_to_result(
            summary, bucket, dry_run=False
        )

        assert result_dry.plan.prune_count == result_exec.plan.prune_count == 2
        assert result_dry.plan.kept_count == result_exec.plan.kept_count == 3
        assert result_dry.plan.reclaimable_bytes == result_exec.plan.reclaimable_bytes
        assert result_dry.plan.total_bytes == result_exec.plan.total_bytes

        assert result_dry.deleted_count == 0
        assert result_dry.deleted_bytes == 0
        assert result_exec.deleted_count == 2
        assert result_exec.deleted_bytes == 600
        assert result_dry.kept_bytes == result_exec.kept_bytes == 400

    def test_filebox_adapter_dry_vs_execute_same_plan_different_deletion(self) -> None:
        summary = FileBoxPruneSummary(
            inbox_kept=2,
            inbox_pruned=1,
            outbox_kept=1,
            outbox_pruned=2,
            bytes_before=800,
            bytes_after=300,
            pruned_paths=("/tmp/in1", "/tmp/out1", "/tmp/out2"),
        )
        bucket = RetentionBucket(
            family="filebox",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )

        result_dry = adapt_filebox_prune_summary_to_result(
            summary, bucket, dry_run=True
        )
        result_exec = adapt_filebox_prune_summary_to_result(
            summary, bucket, dry_run=False
        )

        assert result_dry.plan.prune_count == result_exec.plan.prune_count == 3
        assert result_dry.plan.kept_count == result_exec.plan.kept_count == 3
        assert result_dry.plan.reclaimable_bytes == result_exec.plan.reclaimable_bytes

        assert result_dry.deleted_count == 0
        assert result_dry.deleted_bytes == 0
        assert result_exec.deleted_count == 3
        assert result_exec.deleted_bytes == 500

    def test_report_adapter_dry_vs_execute_same_plan_different_deletion(self) -> None:
        summary = PruneSummary(
            kept=8,
            pruned=4,
            bytes_before=2000,
            bytes_after=800,
            pruned_paths=(
                "/tmp/h-00.md",
                "/tmp/h-01.md",
                "/tmp/h-02.md",
                "/tmp/h-03.md",
            ),
        )
        bucket = RetentionBucket(
            family="reports",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )

        result_dry = adapt_report_prune_summary_to_result(summary, bucket, dry_run=True)
        result_exec = adapt_report_prune_summary_to_result(
            summary, bucket, dry_run=False
        )

        assert result_dry.plan.prune_count == result_exec.plan.prune_count == 4
        assert result_dry.plan.kept_count == result_exec.plan.kept_count == 8
        assert result_dry.plan.reclaimable_bytes == result_exec.plan.reclaimable_bytes

        assert result_dry.deleted_count == 0
        assert result_dry.deleted_bytes == 0
        assert result_exec.deleted_count == 4
        assert result_exec.deleted_bytes == 1200

    def test_housekeeping_adapter_dry_vs_execute_same_plan_different_deletion(
        self,
    ) -> None:
        summary = HousekeepingRuleResult(
            name="run_logs",
            kind="directory",
            scanned_count=10,
            deleted_count=3,
            deleted_bytes=300,
            deleted_paths=["/tmp/log-a.txt", "/tmp/log-b.txt", "/tmp/log-c.txt"],
        )
        bucket = RetentionBucket(
            family="logs",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )

        result_dry = adapt_housekeeping_rule_result_to_result(
            summary, bucket, dry_run=True, reason=CleanupReason.AGE_LIMIT
        )
        result_exec = adapt_housekeeping_rule_result_to_result(
            summary, bucket, dry_run=False, reason=CleanupReason.AGE_LIMIT
        )

        assert result_dry.plan.prune_count == result_exec.plan.prune_count == 3
        assert result_dry.plan.kept_count == result_exec.plan.kept_count == 7

        assert result_dry.deleted_count == 0
        assert result_dry.deleted_bytes == 0
        assert result_dry.deleted_paths == ()
        assert result_exec.deleted_count == 3
        assert result_exec.deleted_bytes == 300
        assert len(result_exec.deleted_paths) == 3
        assert Path("/tmp/log-a.txt") in result_exec.deleted_paths

    def test_workspace_adapter_does_not_double_count_blocked_entries(self) -> None:
        summary = SimpleNamespace(
            kept=2,
            pruned=1,
            bytes_before=1000,
            bytes_after=700,
            pruned_paths=("/tmp/ws-stale",),
            blocked_paths=("/tmp/ws-live",),
            blocked_reasons=("live_workspace_guard",),
        )
        bucket = RetentionBucket(
            family="workspaces",
            scope=RetentionScope.GLOBAL,
            retention_class=RetentionClass.EPHEMERAL,
        )

        result = adapt_workspace_summary_to_result(summary, bucket, dry_run=False)

        assert result.plan.kept_count == 1
        assert result.plan.prune_count == 1
        assert result.plan.blocked_count == 1
        assert len(result.plan.blocked_candidates) == 1

    def test_archive_adapter_candidates_have_correct_reason(self) -> None:
        summary = ArchivePruneSummary(
            kept=1,
            pruned=2,
            bytes_before=100,
            bytes_after=40,
            pruned_paths=("/tmp/a", "/tmp/b"),
        )
        bucket = RetentionBucket(
            family="run_archives",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )

        plan = adapt_archive_prune_summary_to_plan(summary, bucket)
        for candidate in plan.prune_candidates:
            assert candidate.action == CleanupAction.PRUNE
            assert candidate.reason == CleanupReason.AGE_LIMIT
            assert candidate.bucket == bucket

    def test_filebox_adapter_candidates_have_correct_reason(self) -> None:
        summary = FileBoxPruneSummary(
            inbox_kept=1,
            inbox_pruned=1,
            outbox_kept=0,
            outbox_pruned=0,
            bytes_before=100,
            bytes_after=50,
            pruned_paths=("/tmp/f.txt",),
        )
        bucket = RetentionBucket(
            family="filebox",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )

        plan = adapt_filebox_prune_summary_to_plan(summary, bucket)
        for candidate in plan.prune_candidates:
            assert candidate.action == CleanupAction.PRUNE
            assert candidate.reason == CleanupReason.AGE_LIMIT

    def test_report_adapter_candidates_use_count_limit_reason(self) -> None:
        summary = PruneSummary(
            kept=1,
            pruned=2,
            bytes_before=100,
            bytes_after=40,
        )
        bucket = RetentionBucket(
            family="reports",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )

        plan = adapt_report_prune_summary_to_plan(summary, bucket)
        for candidate in plan.prune_candidates:
            assert candidate.reason == CleanupReason.COUNT_LIMIT

    def test_housekeeping_adapter_candidates_use_provided_reason(self) -> None:
        for reason in (CleanupReason.AGE_LIMIT, CleanupReason.CACHE_REBUILDABLE):
            summary = HousekeepingRuleResult(
                name="test",
                kind="directory",
                scanned_count=5,
                deleted_count=1,
                deleted_bytes=10,
            )
            bucket = RetentionBucket(
                family="test",
                scope=RetentionScope.REPO,
                retention_class=RetentionClass.EPHEMERAL,
            )

            plan = adapt_housekeeping_rule_result_to_plan(
                summary, bucket, reason=reason
            )
            for candidate in plan.prune_candidates:
                assert candidate.reason == reason

    def test_housekeeping_adapter_path_uses_real_paths_when_available(self) -> None:
        summary = HousekeepingRuleResult(
            name="test",
            kind="directory",
            scanned_count=5,
            deleted_count=2,
            deleted_bytes=10,
            deleted_paths=["/tmp/a.txt", "/tmp/b.txt"],
        )
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )

        plan = adapt_housekeeping_rule_result_to_plan(
            summary, bucket, reason=CleanupReason.AGE_LIMIT
        )
        assert len(plan.candidates) == 2
        candidate_paths = {c.path for c in plan.candidates}
        assert Path("/tmp/a.txt") in candidate_paths
        assert Path("/tmp/b.txt") in candidate_paths

    def test_housekeeping_adapter_falls_back_to_unknown_when_no_paths(self) -> None:
        summary = HousekeepingRuleResult(
            name="test",
            kind="directory",
            scanned_count=5,
            deleted_count=2,
            deleted_bytes=10,
        )
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )

        plan = adapt_housekeeping_rule_result_to_plan(
            summary, bucket, reason=CleanupReason.AGE_LIMIT
        )
        for candidate in plan.candidates:
            assert candidate.path == Path("<unknown>")
            assert candidate.size_bytes == 0

    def test_report_adapter_path_uses_real_paths_when_available(self) -> None:
        summary = PruneSummary(
            kept=1,
            pruned=2,
            bytes_before=100,
            bytes_after=40,
            pruned_paths=("/tmp/history-a.md", "/tmp/history-b.md"),
        )
        bucket = RetentionBucket(
            family="reports",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )

        plan = adapt_report_prune_summary_to_plan(summary, bucket)
        assert len(plan.candidates) == 2
        candidate_paths = {c.path for c in plan.candidates}
        assert Path("/tmp/history-a.md") in candidate_paths
        assert Path("/tmp/history-b.md") in candidate_paths

    def test_report_adapter_falls_back_to_unknown_when_no_paths(self) -> None:
        summary = PruneSummary(
            kept=1,
            pruned=2,
            bytes_before=100,
            bytes_after=40,
        )
        bucket = RetentionBucket(
            family="reports",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )

        plan = adapt_report_prune_summary_to_plan(summary, bucket)
        for candidate in plan.candidates:
            assert candidate.path == Path("<unknown>")
            assert candidate.size_bytes == 0


class TestCrossFamilyAdapterConsistency:
    def test_all_adapters_produce_consistent_kept_plus_pruned(self) -> None:
        archive_bucket = RetentionBucket(
            family="worktree_archives",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )
        filebox_bucket = RetentionBucket(
            family="filebox",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        report_bucket = RetentionBucket(
            family="reports",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )
        housekeeping_bucket = RetentionBucket(
            family="logs",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )

        archive_summary = ArchivePruneSummary(
            kept=5, pruned=3, bytes_before=1000, bytes_after=400, pruned_paths=()
        )
        filebox_summary = FileBoxPruneSummary(
            inbox_kept=2,
            inbox_pruned=1,
            outbox_kept=3,
            outbox_pruned=2,
            bytes_before=500,
            bytes_after=200,
            pruned_paths=(),
        )
        report_summary = PruneSummary(
            kept=10, pruned=5, bytes_before=2000, bytes_after=1000
        )
        hk_summary = HousekeepingRuleResult(
            name="logs",
            kind="directory",
            scanned_count=20,
            deleted_count=7,
            deleted_bytes=350,
        )

        archive_plan = adapt_archive_prune_summary_to_plan(
            archive_summary, archive_bucket
        )
        filebox_plan = adapt_filebox_prune_summary_to_plan(
            filebox_summary, filebox_bucket
        )
        report_plan = adapt_report_prune_summary_to_plan(report_summary, report_bucket)
        hk_plan = adapt_housekeeping_rule_result_to_plan(
            hk_summary, housekeeping_bucket, reason=CleanupReason.AGE_LIMIT
        )

        for plan in (archive_plan, filebox_plan, report_plan, hk_plan):
            assert plan.kept_count + plan.prune_count >= 0
            assert plan.blocked_count >= 0

        assert archive_plan.kept_count + archive_plan.prune_count == 8
        assert filebox_plan.kept_count + filebox_plan.prune_count == 8
        assert report_plan.kept_count + report_plan.prune_count == 15
        assert hk_plan.kept_count + hk_plan.prune_count == 20

    def test_all_dry_run_adapters_report_zero_deletions(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )

        archive_summary = ArchivePruneSummary(
            kept=2, pruned=3, bytes_before=500, bytes_after=200, pruned_paths=("/a",)
        )
        filebox_summary = FileBoxPruneSummary(
            inbox_kept=1,
            inbox_pruned=1,
            outbox_kept=1,
            outbox_pruned=1,
            bytes_before=400,
            bytes_after=100,
            pruned_paths=("/b",),
        )
        report_summary = PruneSummary(
            kept=1,
            pruned=2,
            bytes_before=300,
            bytes_after=100,
            pruned_paths=("/c",),
        )
        hk_summary = HousekeepingRuleResult(
            name="test",
            kind="directory",
            scanned_count=5,
            deleted_count=2,
            deleted_bytes=100,
            deleted_paths=["/d"],
        )

        results = [
            adapt_archive_prune_summary_to_result(
                archive_summary, bucket, dry_run=True
            ),
            adapt_filebox_prune_summary_to_result(
                filebox_summary, bucket, dry_run=True
            ),
            adapt_report_prune_summary_to_result(report_summary, bucket, dry_run=True),
            adapt_housekeeping_rule_result_to_result(
                hk_summary, bucket, dry_run=True, reason=CleanupReason.AGE_LIMIT
            ),
        ]

        for result in results:
            assert result.deleted_count == 0
            assert result.deleted_bytes == 0
            assert result.deleted_paths == ()

    def test_all_execute_adapters_report_nonzero_deletions(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )

        archive_summary = ArchivePruneSummary(
            kept=1,
            pruned=2,
            bytes_before=300,
            bytes_after=100,
            pruned_paths=("/a", "/b"),
        )
        filebox_summary = FileBoxPruneSummary(
            inbox_kept=1,
            inbox_pruned=1,
            outbox_kept=0,
            outbox_pruned=1,
            bytes_before=200,
            bytes_after=50,
            pruned_paths=("/c", "/d"),
        )
        report_summary = PruneSummary(
            kept=1,
            pruned=2,
            bytes_before=200,
            bytes_after=60,
            pruned_paths=("/e", "/f"),
        )
        hk_summary = HousekeepingRuleResult(
            name="test",
            kind="directory",
            scanned_count=5,
            deleted_count=2,
            deleted_bytes=80,
            deleted_paths=["/g", "/h"],
        )

        results = [
            adapt_archive_prune_summary_to_result(
                archive_summary, bucket, dry_run=False
            ),
            adapt_filebox_prune_summary_to_result(
                filebox_summary, bucket, dry_run=False
            ),
            adapt_report_prune_summary_to_result(report_summary, bucket, dry_run=False),
            adapt_housekeeping_rule_result_to_result(
                hk_summary, bucket, dry_run=False, reason=CleanupReason.AGE_LIMIT
            ),
        ]

        for result in results:
            assert result.deleted_count > 0
            assert result.deleted_bytes > 0

    def test_plan_reclaimable_equals_bytes_before_minus_bytes_after(self) -> None:
        bucket = RetentionBucket(
            family="test",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )

        archive_summary = ArchivePruneSummary(
            kept=3,
            pruned=2,
            bytes_before=1000,
            bytes_after=400,
            pruned_paths=("/a", "/b"),
        )
        filebox_summary = FileBoxPruneSummary(
            inbox_kept=2,
            inbox_pruned=1,
            outbox_kept=2,
            outbox_pruned=1,
            bytes_before=800,
            bytes_after=300,
            pruned_paths=(),
        )
        report_summary = PruneSummary(
            kept=5, pruned=3, bytes_before=600, bytes_after=150
        )

        for summary, adapter in [
            (archive_summary, adapt_archive_prune_summary_to_plan),
            (filebox_summary, adapt_filebox_prune_summary_to_plan),
            (report_summary, adapt_report_prune_summary_to_plan),
        ]:
            plan = adapter(summary, bucket)
            assert plan.reclaimable_bytes == plan.total_bytes - summary.bytes_after


class TestCanonicalBucketNaming:
    def test_repo_bucket_families_are_stable(self) -> None:
        from codex_autorunner.core.state_retention import (
            REPO_FILEBOX_BUCKET,
            REPO_GITHUB_CONTEXT_BUCKET,
            REPO_LOGS_BUCKET,
            REPO_REPORTS_BUCKET,
            REPO_REVIEW_RUNS_BUCKET,
            REPO_RUN_ARCHIVE_BUCKET,
            REPO_UPLOADS_BUCKET,
            REPO_WORKSPACE_BUCKET,
            REPO_WORKTREE_ARCHIVE_BUCKET,
        )

        assert REPO_WORKTREE_ARCHIVE_BUCKET.family == "worktree_archives"
        assert REPO_RUN_ARCHIVE_BUCKET.family == "run_archives"
        assert REPO_FILEBOX_BUCKET.family == "filebox"
        assert REPO_REPORTS_BUCKET.family == "reports"
        assert REPO_WORKSPACE_BUCKET.family == "workspaces"
        assert REPO_LOGS_BUCKET.family == "logs"
        assert REPO_UPLOADS_BUCKET.family == "uploads"
        assert REPO_GITHUB_CONTEXT_BUCKET.family == "github_context"
        assert REPO_REVIEW_RUNS_BUCKET.family == "review_runs"

    def test_global_bucket_families_are_stable(self) -> None:
        from codex_autorunner.core.state_retention import (
            GLOBAL_LOGS_BUCKET,
            GLOBAL_UPDATE_CACHE_BUCKET,
            GLOBAL_WORKSPACE_BUCKET,
        )

        assert GLOBAL_WORKSPACE_BUCKET.family == "workspaces"
        assert GLOBAL_WORKSPACE_BUCKET.scope == RetentionScope.GLOBAL
        assert GLOBAL_UPDATE_CACHE_BUCKET.family == "update_cache"
        assert GLOBAL_UPDATE_CACHE_BUCKET.scope == RetentionScope.GLOBAL
        assert GLOBAL_LOGS_BUCKET.family == "logs"
        assert GLOBAL_LOGS_BUCKET.scope == RetentionScope.GLOBAL

    def test_repo_buckets_have_repo_scope(self) -> None:
        from codex_autorunner.core.state_retention import (
            REPO_FILEBOX_BUCKET,
            REPO_GITHUB_CONTEXT_BUCKET,
            REPO_LOGS_BUCKET,
            REPO_REPORTS_BUCKET,
            REPO_REVIEW_RUNS_BUCKET,
            REPO_RUN_ARCHIVE_BUCKET,
            REPO_UPLOADS_BUCKET,
            REPO_WORKSPACE_BUCKET,
            REPO_WORKTREE_ARCHIVE_BUCKET,
        )

        for bucket in [
            REPO_WORKTREE_ARCHIVE_BUCKET,
            REPO_RUN_ARCHIVE_BUCKET,
            REPO_FILEBOX_BUCKET,
            REPO_REPORTS_BUCKET,
            REPO_WORKSPACE_BUCKET,
            REPO_LOGS_BUCKET,
            REPO_UPLOADS_BUCKET,
            REPO_GITHUB_CONTEXT_BUCKET,
            REPO_REVIEW_RUNS_BUCKET,
        ]:
            assert bucket.scope == RetentionScope.REPO

    def test_retention_class_assignment(self) -> None:
        from codex_autorunner.core.state_retention import (
            GLOBAL_UPDATE_CACHE_BUCKET,
            REPO_FILEBOX_BUCKET,
            REPO_GITHUB_CONTEXT_BUCKET,
            REPO_LOGS_BUCKET,
            REPO_REPORTS_BUCKET,
            REPO_REVIEW_RUNS_BUCKET,
            REPO_RUN_ARCHIVE_BUCKET,
            REPO_UPLOADS_BUCKET,
            REPO_WORKSPACE_BUCKET,
            REPO_WORKTREE_ARCHIVE_BUCKET,
        )

        assert REPO_WORKTREE_ARCHIVE_BUCKET.retention_class == RetentionClass.REVIEWABLE
        assert REPO_RUN_ARCHIVE_BUCKET.retention_class == RetentionClass.REVIEWABLE
        assert REPO_REPORTS_BUCKET.retention_class == RetentionClass.REVIEWABLE
        assert REPO_GITHUB_CONTEXT_BUCKET.retention_class == RetentionClass.REVIEWABLE
        assert REPO_REVIEW_RUNS_BUCKET.retention_class == RetentionClass.REVIEWABLE
        assert REPO_FILEBOX_BUCKET.retention_class == RetentionClass.EPHEMERAL
        assert REPO_WORKSPACE_BUCKET.retention_class == RetentionClass.EPHEMERAL
        assert REPO_LOGS_BUCKET.retention_class == RetentionClass.EPHEMERAL
        assert REPO_UPLOADS_BUCKET.retention_class == RetentionClass.EPHEMERAL
        assert GLOBAL_UPDATE_CACHE_BUCKET.retention_class == RetentionClass.CACHE_ONLY


class TestAggregationAcrossAllFamilies:
    def test_aggregated_plan_covers_all_repo_families(self) -> None:
        from codex_autorunner.core.state_retention import (
            REPO_FILEBOX_BUCKET,
            REPO_LOGS_BUCKET,
            REPO_REPORTS_BUCKET,
            REPO_RUN_ARCHIVE_BUCKET,
            REPO_WORKSPACE_BUCKET,
            REPO_WORKTREE_ARCHIVE_BUCKET,
        )

        families = [
            REPO_WORKTREE_ARCHIVE_BUCKET,
            REPO_RUN_ARCHIVE_BUCKET,
            REPO_FILEBOX_BUCKET,
            REPO_REPORTS_BUCKET,
            REPO_WORKSPACE_BUCKET,
            REPO_LOGS_BUCKET,
        ]

        plans = []
        for bucket in families:
            candidate = CleanupCandidate(
                path=Path(f"/tmp/{bucket.family}"),
                size_bytes=100,
                bucket=bucket,
                action=CleanupAction.PRUNE,
                reason=CleanupReason.AGE_LIMIT,
            )
            plans.append(make_cleanup_plan(bucket, [candidate]))

        aggregated = AggregatedCleanupPlan(plans=tuple(plans))

        assert aggregated.total_candidates == len(families)
        assert aggregated.total_prune_count == len(families)
        assert aggregated.total_reclaimable_bytes == len(families) * 100

        by_scope = aggregated.by_scope()
        assert RetentionScope.REPO in by_scope
        repo_plan = by_scope[RetentionScope.REPO]
        assert repo_plan.total_candidates == len(families)

    def test_aggregated_result_covers_mixed_scopes(self) -> None:
        from codex_autorunner.core.state_retention import (
            GLOBAL_UPDATE_CACHE_BUCKET,
            REPO_WORKTREE_ARCHIVE_BUCKET,
        )

        repo_plan = make_cleanup_plan(
            REPO_WORKTREE_ARCHIVE_BUCKET,
            [
                CleanupCandidate(
                    path=Path("/tmp/a"),
                    size_bytes=100,
                    bucket=REPO_WORKTREE_ARCHIVE_BUCKET,
                    action=CleanupAction.PRUNE,
                    reason=CleanupReason.AGE_LIMIT,
                )
            ],
        )
        global_plan = make_cleanup_plan(
            GLOBAL_UPDATE_CACHE_BUCKET,
            [
                CleanupCandidate(
                    path=Path("/tmp/b"),
                    size_bytes=200,
                    bucket=GLOBAL_UPDATE_CACHE_BUCKET,
                    action=CleanupAction.PRUNE,
                    reason=CleanupReason.CACHE_REBUILDABLE,
                )
            ],
        )

        repo_result = make_cleanup_result(
            repo_plan, [Path("/tmp/a")], deleted_bytes=100
        )
        global_result = make_cleanup_result(
            global_plan, [Path("/tmp/b")], deleted_bytes=200
        )

        aggregated = aggregate_cleanup_results([repo_result, global_result])

        assert aggregated.total_deleted_count == 2
        assert aggregated.total_deleted_bytes == 300

        by_scope = aggregated.by_scope()
        assert RetentionScope.REPO in by_scope
        assert RetentionScope.GLOBAL in by_scope
        assert by_scope[RetentionScope.REPO].total_deleted_bytes == 100
        assert by_scope[RetentionScope.GLOBAL].total_deleted_bytes == 200
