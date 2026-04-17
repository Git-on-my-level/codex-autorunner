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


def _set_tree_mtime(path: Path, timestamp: float) -> None:
    descendants = sorted(path.rglob("*"), key=lambda candidate: len(candidate.parts))
    for candidate in reversed(descendants):
        os.utime(candidate, (timestamp, timestamp))
    os.utime(path, (timestamp, timestamp))


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
        _set_tree_mtime(old_workspace, old_ts)

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

    def test_keeps_workspace_with_recent_nested_activity(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()
        workspace = root / "recent123456"
        workspace.mkdir()
        nested = workspace / "state" / "session.json"
        nested.parent.mkdir()
        nested.write_text("{}")

        old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp()
        _set_tree_mtime(workspace, old_ts)
        recent_ts = datetime.now(timezone.utc).timestamp()
        os.utime(nested, (recent_ts, recent_ts))
        os.utime(workspace, (old_ts, old_ts))

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
        _set_tree_mtime(old_workspace, old_ts)

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
        assert summary.bytes_before > summary.bytes_after
        assert old_workspace.exists()

    def test_execution_deletes_stale_workspaces(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()
        old_workspace = root / "old123456789"
        old_workspace.mkdir()
        (old_workspace / "state.json").write_text("{}")

        old_mtime = datetime.now(timezone.utc) - timedelta(days=10)
        old_ts = old_mtime.timestamp()
        _set_tree_mtime(old_workspace, old_ts)

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

    def test_failed_delete_does_not_report_reclaimed_bytes(
        self, monkeypatch, tmp_path: Path
    ):
        root = tmp_path / "workspaces"
        root.mkdir()
        old_workspace = root / "old123456789"
        old_workspace.mkdir()
        (old_workspace / "state.json").write_text("{}")

        old_mtime = datetime.now(timezone.utc) - timedelta(days=10)
        old_ts = old_mtime.timestamp()
        _set_tree_mtime(old_workspace, old_ts)

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

        def _fail_remove(_path: Path) -> None:
            raise OSError("boom")

        monkeypatch.setattr(
            "codex_autorunner.integrations.app_server.retention._remove_tree",
            _fail_remove,
        )

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=False)

        assert summary.pruned == 0
        assert summary.bytes_after == summary.bytes_before
        assert summary.blocked_paths == (str(old_workspace),)
        assert summary.blocked_reasons == ("deletion_failed",)
        assert old_workspace.exists()


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
        _set_tree_mtime(stale_workspace, old_ts)

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
        assert result.plan.total_bytes == 1000
        assert result.plan.reclaimable_bytes == 600
        assert result.plan.prune_count == 3
        assert result.plan.blocked_count == 1
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

        assert result.plan.reclaimable_bytes == 600
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


class TestWorkspaceRetentionSafetyGuards:
    def test_never_deletes_active_workspace_even_if_stale(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        active_workspace = root / "active123456"
        active_workspace.mkdir()
        (active_workspace / "state.json").write_text("{}")

        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
        _set_tree_mtime(active_workspace, old_ts)

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)

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
        assert blocked.action == CleanupAction.SKIP_BLOCKED

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=False)

        assert summary.pruned == 0
        assert active_workspace.exists()

    def test_never_deletes_locked_workspace_even_if_stale(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        locked_workspace = root / "locked123456"
        locked_workspace.mkdir()
        (locked_workspace / "lock").write_text("locked")

        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
        _set_tree_mtime(locked_workspace, old_ts)

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)

        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids={"locked123456"},
            current_workspace_ids=set(),
            now=now,
        )

        assert plan.blocked_count == 1
        assert plan.prune_count == 0
        blocked = plan.blocked_candidates[0]
        assert blocked.reason == CleanupReason.LOCK_GUARD

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=False)

        assert summary.pruned == 0
        assert locked_workspace.exists()

    def test_never_deletes_current_run_workspace_even_if_stale(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        current_workspace = root / "current123456"
        current_workspace.mkdir()
        (current_workspace / "run.json").write_text("{}")

        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
        _set_tree_mtime(current_workspace, old_ts)

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)

        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids={"current123456"},
            now=now,
        )

        assert plan.blocked_count == 1
        assert plan.prune_count == 0
        blocked = plan.blocked_candidates[0]
        assert blocked.reason == CleanupReason.ACTIVE_RUN_GUARD

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=False)

        assert summary.pruned == 0
        assert current_workspace.exists()

    def test_multiple_guards_all_protect_workspace(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        active_workspace = root / "active123456"
        active_workspace.mkdir()
        (active_workspace / "state.json").write_text("{}")

        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
        _set_tree_mtime(active_workspace, old_ts)

        policy = WorkspaceRetentionPolicy(max_age_days=0)
        now = datetime.now(timezone.utc)

        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids={"active123456"},
            locked_workspace_ids={"active123456"},
            current_workspace_ids={"active123456"},
            now=now,
        )

        assert plan.blocked_count == 1
        assert plan.prune_count == 0

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=False)

        assert summary.pruned == 0
        assert active_workspace.exists()

    def test_deletes_only_truly_stale_workspaces(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        active_workspace = root / "active123456"
        active_workspace.mkdir()
        (active_workspace / "state.json").write_text("{}")

        stale_workspace = root / "stale123456789"
        stale_workspace.mkdir()
        (stale_workspace / "old_data.json").write_text("{}")

        recent_workspace = root / "recent123456"
        recent_workspace.mkdir()

        old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp()
        _set_tree_mtime(stale_workspace, old_ts)

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)

        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids={"active123456"},
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            now=now,
        )

        assert plan.blocked_count == 1
        assert plan.prune_count == 1
        assert plan.kept_count == 1

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=False)

        assert summary.pruned == 1
        assert active_workspace.exists()
        assert recent_workspace.exists()
        assert not stale_workspace.exists()

    def test_dry_run_never_deletes_any_workspace(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        for i in range(5):
            ws = root / f"stale{i:09d}"
            ws.mkdir()
            (ws / "data.txt").write_text("data")
            old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp()
            _set_tree_mtime(ws, old_ts)

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

        assert plan.prune_count == 5

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=True)

        assert summary.pruned == 5

        for i in range(5):
            ws = root / f"stale{i:09d}"
            assert ws.exists()

    def test_respects_max_age_boundary_exactly(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        boundary_workspace = root / "boundary123456"
        boundary_workspace.mkdir()
        (boundary_workspace / "state.json").write_text("{}")

        cutoff_time = datetime.now(timezone.utc) - timedelta(days=7)
        boundary_ts = cutoff_time.timestamp()
        _set_tree_mtime(boundary_workspace, boundary_ts)

        policy = WorkspaceRetentionPolicy(max_age_days=7)

        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            now=cutoff_time + timedelta(seconds=1),
        )

        assert plan.kept_count == 1
        assert plan.prune_count == 0

    def test_workspace_outside_root_is_blocked(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        outside_root = tmp_path / "outside"
        outside_root.mkdir()

        policy = WorkspaceRetentionPolicy(max_age_days=0)

        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
        )

        assert plan.blocked_count == 0


class TestWorkspaceRetentionIntegration:
    def test_end_to_end_cleanup_with_mixed_states(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        active_ws = root / "active123456"
        active_ws.mkdir()
        (active_ws / "state.json").write_text('{"active": true}')

        locked_ws = root / "locked123456"
        locked_ws.mkdir()
        (locked_ws / "lock").write_text("1")

        current_ws = root / "current123456"
        current_ws.mkdir()
        (current_ws / "run.json").write_text("{}")

        recent_ws = root / "recent123456"
        recent_ws.mkdir()
        (recent_ws / "data.json").write_text("{}")

        stale_ws1 = root / "stale123456789"
        stale_ws1.mkdir()
        (stale_ws1 / "old.json").write_text("{}")
        old_ts1 = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp()
        _set_tree_mtime(stale_ws1, old_ts1)

        stale_ws2 = root / "old987654321"
        stale_ws2.mkdir()
        (stale_ws2 / "data.txt").write_text("old data")
        old_ts2 = (datetime.now(timezone.utc) - timedelta(days=21)).timestamp()
        _set_tree_mtime(stale_ws2, old_ts2)

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)

        summary = prune_workspace_root(
            root,
            policy=policy,
            active_workspace_ids={"active123456"},
            locked_workspace_ids={"locked123456"},
            current_workspace_ids={"current123456"},
            dry_run=False,
            now=now,
        )

        assert summary.pruned == 2
        assert active_ws.exists()
        assert locked_ws.exists()
        assert current_ws.exists()
        assert recent_ws.exists()
        assert not stale_ws1.exists()
        assert not stale_ws2.exists()

    def test_byte_accounting_across_deletions(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        stale_ws = root / "stale123456789"
        stale_ws.mkdir()
        (stale_ws / "big_file.txt").write_text("x" * 10000)
        (stale_ws / "small_file.txt").write_text("y" * 100)

        old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp()
        _set_tree_mtime(stale_ws, old_ts)

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

        assert plan.reclaimable_bytes > 0

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=False)

        assert summary.pruned == 1
        assert summary.bytes_before > 0
        assert summary.bytes_after == 0
        assert not stale_ws.exists()

    def test_empty_workspace_root_handles_gracefully(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

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
        assert summary.bytes_before == 0
        assert summary.bytes_after == 0

    def test_missing_workspace_root_returns_empty_summary(self, tmp_path: Path):
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

    def test_workspace_with_nested_directories_calculates_size(self, tmp_path: Path):
        root = tmp_path / "workspaces"
        root.mkdir()

        stale_ws = root / "stale123456789"
        stale_ws.mkdir()
        (stale_ws / "level1").mkdir()
        (stale_ws / "level1" / "file1.txt").write_text("x" * 1000)
        (stale_ws / "level1" / "level2").mkdir()
        (stale_ws / "level1" / "level2" / "file2.txt").write_text("y" * 500)

        old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp()
        _set_tree_mtime(stale_ws, old_ts)

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

        assert plan.reclaimable_bytes >= 1500

        summary = execute_workspace_retention(plan, workspace_root=root, dry_run=False)

        assert summary.pruned == 1
        assert not stale_ws.exists()


class TestWorkspaceRetentionPolicyResolution:
    def test_policy_respects_config_object(self):
        class Config:
            app_server_workspace_max_age_days = 14

        policy = resolve_workspace_retention_policy(Config())
        assert policy.max_age_days == 14

    def test_policy_respects_dict_config(self):
        config = {"app_server_workspace_max_age_days": 21}
        policy = resolve_workspace_retention_policy(config)
        assert policy.max_age_days == 21

    def test_policy_uses_default_when_missing(self):
        policy = resolve_workspace_retention_policy(None)
        assert policy.max_age_days == DEFAULT_WORKSPACE_MAX_AGE_DAYS

    def test_policy_coerces_negative_to_zero(self):
        policy = resolve_workspace_retention_policy(
            {"app_server_workspace_max_age_days": -5}
        )
        assert policy.max_age_days == 0

    def test_policy_coerces_string_to_int(self):
        policy = resolve_workspace_retention_policy(
            {"app_server_workspace_max_age_days": "30"}
        )
        assert policy.max_age_days == 30

    def test_policy_ignores_invalid_string(self):
        policy = resolve_workspace_retention_policy(
            {"app_server_workspace_max_age_days": "invalid"}
        )
        assert policy.max_age_days == DEFAULT_WORKSPACE_MAX_AGE_DAYS


class TestWorkspaceRetentionDryRunExecuteParity:
    def test_dry_run_and_execute_same_candidate_counts(self, tmp_path: Path):
        root_a = tmp_path / "dry" / "workspaces"
        root_b = tmp_path / "exec" / "workspaces"

        for root in (root_a, root_b):
            root.mkdir(parents=True)
            stale = root / "stale123456789"
            stale.mkdir()
            (stale / "data.json").write_text("{}")
            old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp()
            _set_tree_mtime(stale, old_ts)

            recent = root / "recent123456"
            recent.mkdir()

            active = root / "active123456"
            active.mkdir()
            (active / "state.json").write_text("{}")

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)
        active_ids = {"active123456"}

        plan_dry = plan_workspace_retention(
            root_a,
            policy=policy,
            active_workspace_ids=active_ids,
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            now=now,
        )
        plan_exec = plan_workspace_retention(
            root_b,
            policy=policy,
            active_workspace_ids=active_ids,
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            now=now,
        )

        assert plan_dry.prune_count == plan_exec.prune_count == 1
        assert plan_dry.blocked_count == plan_exec.blocked_count == 1
        assert plan_dry.kept_count == plan_exec.kept_count == 1

        summary_dry = execute_workspace_retention(
            plan_dry, workspace_root=root_a, dry_run=True
        )
        summary_exec = execute_workspace_retention(
            plan_exec, workspace_root=root_b, dry_run=False
        )

        assert summary_dry.pruned == summary_exec.pruned == 1
        assert (root_a / "stale123456789").exists()
        assert not (root_b / "stale123456789").exists()

    def test_dry_run_and_execute_same_blocked_reasons(self, tmp_path: Path):
        root_a = tmp_path / "dry" / "workspaces"
        root_b = tmp_path / "exec" / "workspaces"

        for root in (root_a, root_b):
            root.mkdir(parents=True)
            locked = root / "locked123456"
            locked.mkdir()
            (locked / "lock").write_text("1")
            old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp()
            _set_tree_mtime(locked, old_ts)

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)

        plan_dry = plan_workspace_retention(
            root_a,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids={"locked123456"},
            current_workspace_ids=set(),
            now=now,
        )
        plan_exec = plan_workspace_retention(
            root_b,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids={"locked123456"},
            current_workspace_ids=set(),
            now=now,
        )

        assert plan_dry.blocked_count == plan_exec.blocked_count == 1
        assert plan_dry.blocked_candidates[0].reason == CleanupReason.LOCK_GUARD
        assert plan_exec.blocked_candidates[0].reason == CleanupReason.LOCK_GUARD

    def test_dry_run_and_execute_same_guard_reasons_for_active_locked_current(
        self, tmp_path: Path
    ):
        root = tmp_path / "workspaces"
        root.mkdir()

        active_ws = root / "active123456"
        active_ws.mkdir()
        locked_ws = root / "locked123456"
        locked_ws.mkdir()
        current_ws = root / "current123456"
        current_ws.mkdir()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
        for ws in (active_ws, locked_ws, current_ws):
            _set_tree_mtime(ws, old_ts)

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)
        plan = plan_workspace_retention(
            root,
            policy=policy,
            active_workspace_ids={"active123456"},
            locked_workspace_ids={"locked123456"},
            current_workspace_ids={"current123456"},
            now=now,
        )

        reasons = {c.path.name: c.reason for c in plan.blocked_candidates}
        assert reasons["active123456"] == CleanupReason.LIVE_WORKSPACE_GUARD
        assert reasons["locked123456"] == CleanupReason.LOCK_GUARD
        assert reasons["current123456"] == CleanupReason.ACTIVE_RUN_GUARD

    def test_byte_accounting_parity_dry_run_vs_execute(self, tmp_path: Path):
        root_a = tmp_path / "dry" / "workspaces"
        root_b = tmp_path / "exec" / "workspaces"

        for root in (root_a, root_b):
            root.mkdir(parents=True)
            stale = root / "stale123456789"
            stale.mkdir()
            (stale / "big.txt").write_text("x" * 5000)
            old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp()
            _set_tree_mtime(stale, old_ts)

        policy = WorkspaceRetentionPolicy(max_age_days=7)
        now = datetime.now(timezone.utc)

        plan_dry = plan_workspace_retention(
            root_a,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            now=now,
        )
        plan_exec = plan_workspace_retention(
            root_b,
            policy=policy,
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            now=now,
        )

        assert plan_dry.reclaimable_bytes == plan_exec.reclaimable_bytes
        assert plan_dry.total_bytes == plan_exec.total_bytes

        summary_dry = execute_workspace_retention(
            plan_dry, workspace_root=root_a, dry_run=True
        )
        summary_exec = execute_workspace_retention(
            plan_exec, workspace_root=root_b, dry_run=False
        )

        assert summary_dry.pruned == summary_exec.pruned == 1
        assert summary_dry.bytes_before == summary_exec.bytes_before

    def test_adapt_parity_dry_run_vs_execute(self, tmp_path: Path):
        bucket = RetentionBucket(
            family="workspaces",
            scope=RetentionScope.GLOBAL,
            retention_class=RetentionClass.EPHEMERAL,
        )
        summary = WorkspacePruneSummary(
            kept=1,
            pruned=2,
            bytes_before=1000,
            bytes_after=400,
            pruned_paths=("/tmp/ws1", "/tmp/ws2"),
            blocked_paths=("/tmp/ws3",),
            blocked_reasons=("live_workspace_guard",),
        )

        result_dry = adapt_workspace_summary_to_result(summary, bucket, dry_run=True)
        result_exec = adapt_workspace_summary_to_result(summary, bucket, dry_run=False)

        assert result_dry.plan.prune_count == result_exec.plan.prune_count == 2
        assert result_dry.plan.blocked_count == result_exec.plan.blocked_count == 1
        assert result_dry.plan.reclaimable_bytes == result_exec.plan.reclaimable_bytes

        assert result_dry.deleted_count == 0
        assert result_dry.deleted_bytes == 0
        assert result_exec.deleted_count == 2
        assert result_exec.deleted_bytes == 600
