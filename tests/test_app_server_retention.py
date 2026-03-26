from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_autorunner.core.state_retention import (
    CleanupAction,
    CleanupReason,
    RetentionBucket,
    RetentionClass,
    RetentionScope,
)
from codex_autorunner.integrations.app_server.retention import (
    DEFAULT_WORKSPACE_MAX_AGE_DAYS,
    WorkspacePruneSummary,
    WorkspaceRetentionPolicy,
    adapt_workspace_summary_to_result,
    execute_workspace_retention,
    plan_workspace_retention,
    prune_workspace_root,
    resolve_global_workspace_root,
    resolve_repo_workspace_root,
    resolve_workspace_retention_policy,
)
from codex_autorunner.integrations.app_server.supervisor import (
    WorkspaceAppServerSupervisor,
)


class TestResolveWorkspaceRetentionPolicy:
    def test_returns_default_max_age_days(self):
        policy = resolve_workspace_retention_policy(None)
        assert policy.max_age_days == DEFAULT_WORKSPACE_MAX_AGE_DAYS

    def test_reads_from_mapping(self):
        config = {"app_server_workspace_max_age_days": 14}
        policy = resolve_workspace_retention_policy(config)
        assert policy.max_age_days == 14

    def test_reads_from_object(self):
        class Config:
            app_server_workspace_max_age_days = 21

        policy = resolve_workspace_retention_policy(Config())
        assert policy.max_age_days == 21

    def test_coerces_nonnegative(self):
        policy = resolve_workspace_retention_policy(
            {"app_server_workspace_max_age_days": -5}
        )
        assert policy.max_age_days == 0


class TestResolveWorkspaceRoots:
    def test_global_workspace_root_uses_global_state(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CAR_GLOBAL_STATE_ROOT", str(tmp_path))
        root = resolve_global_workspace_root()
        assert root == tmp_path / "workspaces"

    def test_repo_workspace_root_uses_repo_state(self, tmp_path: Path):
        root = resolve_repo_workspace_root(tmp_path)
        assert root == tmp_path / ".codex-autorunner" / "app_server_workspaces"


class TestPlanWorkspaceRetention:
    def test_empty_root_returns_empty_plan(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
        )

        assert plan.total_bytes == 0
        assert plan.prune_count == 0
        assert plan.blocked_count == 0

    def test_marks_stale_workspaces_for_prune(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()
        old_workspace = root / "old123456789"
        old_workspace.mkdir()
        (old_workspace / "state.json").write_text("{}")

        old_mtime = datetime.now(timezone.utc) - timedelta(days=14)
        old_ts = old_mtime.timestamp()
        os.utime(old_workspace, (old_ts, old_ts))

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)
        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            now=now,
        )

        assert plan.prune_count == 1
        assert plan.blocked_count == 0
        candidate = plan.prune_candidates[0]
        assert candidate.action == CleanupAction.PRUNE
        assert candidate.reason == CleanupReason.STALE_WORKSPACE

    def test_blocks_active_workspaces(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()
        active_workspace = root / "active123456"
        active_workspace.mkdir()

        policy = WorkspaceRetentionPolicy(max_age_days=0)
        now = datetime.now(timezone.utc) - timedelta(days=10)
        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids={"active123456"},
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            now=now,
        )

        assert plan.blocked_count == 1
        assert plan.prune_count == 0
        blocked = plan.blocked_candidates[0]
        assert blocked.reason == CleanupReason.LIVE_WORKSPACE_GUARD

    def test_blocks_locked_workspaces(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()
        locked_workspace = root / "locked123456"
        locked_workspace.mkdir()

        policy = WorkspaceRetentionPolicy(max_age_days=0)
        now = datetime.now(timezone.utc) - timedelta(days=10)
        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids={"locked123456"},
            current_workspace_ids=set(),
            now=now,
        )

        assert plan.blocked_count == 1
        blocked = plan.blocked_candidates[0]
        assert blocked.reason == CleanupReason.LOCK_GUARD

    def test_blocks_current_workspaces(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()
        current_workspace = root / "current123456"
        current_workspace.mkdir()

        policy = WorkspaceRetentionPolicy(max_age_days=0)
        now = datetime.now(timezone.utc) - timedelta(days=10)
        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids={"current123456"},
            now=now,
        )

        assert plan.blocked_count == 1
        blocked = plan.blocked_candidates[0]
        assert blocked.reason == CleanupReason.ACTIVE_RUN_GUARD

    def test_keeps_recent_workspaces(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()
        recent_workspace = root / "recent123456"
        recent_workspace.mkdir()

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
        )

        assert plan.kept_count == 1
        assert plan.prune_count == 0


class TestExecuteWorkspaceRetention:
    def test_dry_run_does_not_delete(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()
        old_workspace = root / "old123456789"
        old_workspace.mkdir()
        (old_workspace / "state.json").write_text("{}")

        old_mtime = datetime.now(timezone.utc) - timedelta(days=10)
        old_ts = old_mtime.timestamp()
        os.utime(old_workspace, (old_ts, old_ts))

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)
        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            now=now,
        )

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=True)

        assert summary.pruned == 1
        assert old_workspace.exists()

    def test_execution_deletes_stale_workspaces(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()
        old_workspace = root / "old123456789"
        old_workspace.mkdir()
        (old_workspace / "state.json").write_text("{}")

        old_mtime = datetime.now(timezone.utc) - timedelta(days=10)
        old_ts = old_mtime.timestamp()
        os.utime(old_workspace, (old_ts, old_ts))

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)
        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            now=now,
        )

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=False)

        assert summary.pruned == 1
        assert not old_workspace.exists()


class TestPruneWorkspaceRoot:
    def test_combined_plan_and_execute(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        active_workspace = root / "active123456"
        active_workspace.mkdir()
        (active_workspace / "state.json").write_text("{}")

        stale_workspace = root / "stale123456789"
        stale_workspace.mkdir()
        (stale_workspace / "state.json").write_text("{}")
        old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp()
        os.utime(stale_workspace, (old_ts, old_ts))

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)

        summary = prune_workspace_root(
            root,
            policy=policy,
            active_workspace_ids={"active123456"},
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            dry_run=False,
            now=now,
        )

        assert summary.pruned == 1
        assert summary.kept >= 0
        assert active_workspace.exists()
        assert not stale_workspace.exists()

    def test_handles_nonexistent_root(self, tmp_path: Path):
        root = tmp_path / "nonexistent"
        policy = WorkspaceRetentionPolicy(max_age_days=7)

        summary = prune_workspace_root(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            dry_run=False,
        )

        assert summary.pruned == 0
        assert summary.kept == 0


class TestAdaptWorkspaceSummaryToResult:
    def test_adapts_summary_to_cleanup_result(self, tmp_path: Path):
        bucket = RetentionBucket(
            family="workspaces",
            scope=RetentionScope.GLOBAL,
            retention_class=RetentionClass.EPHEMERAL,
        )
        summary = WorkspacePruneSummary(
            kept=2,
            pruned=3,
            bytes_before=1000,
            bytes_after=400,
            pruned_paths=("/tmp/ws1", "/tmp/ws2", "/tmp/ws3"),
            blocked_paths=("/tmp/ws4",),
            blocked_reasons=("live_workspace_guard",),
        )

        result = adapt_workspace_summary_to_result(summary, bucket, dry_run=False)

        assert result.bucket == bucket
        assert result.deleted_count == 3
        assert result.deleted_bytes == 600
        assert result.success is True

    def test_dry_run_returns_zero_deleted(self, tmp_path: Path):
        bucket = RetentionBucket(
            family="workspaces",
            scope=RetentionScope.GLOBAL,
            retention_class=RetentionClass.EPHEMERAL,
        )
        summary = WorkspacePruneSummary(
            kept=2,
            pruned=3,
            bytes_before=1000,
            bytes_after=400,
            pruned_paths=("/tmp/ws1",),
            blocked_paths=(),
            blocked_reasons=(),
        )

        result = adapt_workspace_summary_to_result(summary, bucket, dry_run=True)

        assert result.deleted_count == 0
        assert result.deleted_bytes == 0


class TestSupervisorIntegration:
    def test_active_workspace_ids_returns_current_handles(self, tmp_path: Path):
        def env_builder(
            _workspace_root: Path, _workspace_id: str, _state_dir: Path
        ) -> dict:
            return {}

        supervisor = WorkspaceAppServerSupervisor(
            ["python", "-c", "print('noop')"],
            state_root=tmp_path,
            env_builder=env_builder,
        )

        assert supervisor.active_workspace_ids() == set()
        assert supervisor.state_root() == tmp_path
