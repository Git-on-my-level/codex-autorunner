from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.archive_retention import (
    RunArchiveRetentionPolicy,
    WorktreeArchiveRetentionPolicy,
    prune_run_archive_root,
    prune_worktree_archive_root,
    resolve_run_archive_retention_policy,
    resolve_worktree_archive_retention_policy,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_snapshot(
    archive_root: Path,
    worktree_id: str,
    snapshot_id: str,
    *,
    created_at: str,
    payload: str,
) -> Path:
    snapshot_root = archive_root / worktree_id / snapshot_id
    _write(snapshot_root / "META.json", f'{{"created_at": "{created_at}"}}\n')
    _write(snapshot_root / "tickets" / "TICKET-001.md", payload)
    return snapshot_root


def test_prune_worktree_archive_root_respects_per_repo_count(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive" / "worktrees"
    oldest = _write_snapshot(
        archive_root,
        "repo-a",
        "20260101T000000Z--repo-a--1111111",
        created_at="2026-01-01T00:00:00Z",
        payload="old",
    )
    newest = _write_snapshot(
        archive_root,
        "repo-a",
        "20260102T000000Z--repo-a--2222222",
        created_at="2026-01-02T00:00:00Z",
        payload="new",
    )

    summary = prune_worktree_archive_root(
        archive_root,
        policy=WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=1,
            max_age_days=365,
            max_total_bytes=1_000_000,
        ),
    )

    assert summary.pruned == 1
    assert not oldest.exists()
    assert newest.exists()


def test_prune_worktree_archive_root_preserves_requested_snapshot(
    tmp_path: Path,
) -> None:
    archive_root = tmp_path / "archive" / "worktrees"
    kept = _write_snapshot(
        archive_root,
        "repo-a",
        "20260101T000000Z--repo-a--1111111",
        created_at="2026-01-01T00:00:00Z",
        payload="old",
    )
    newer = _write_snapshot(
        archive_root,
        "repo-a",
        "20260102T000000Z--repo-a--2222222",
        created_at="2026-01-02T00:00:00Z",
        payload="new",
    )

    summary = prune_worktree_archive_root(
        archive_root,
        policy=WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=0,
            max_age_days=365,
            max_total_bytes=1_000_000,
        ),
        preserve_paths=(kept,),
    )

    assert summary.kept == 1
    assert kept.exists()
    assert not newer.exists()


def test_prune_worktree_archive_root_ignores_incomplete_snapshot_without_meta(
    tmp_path: Path,
) -> None:
    archive_root = tmp_path / "archive" / "worktrees"
    older = _write_snapshot(
        archive_root,
        "repo-a",
        "20260101T000000Z--repo-a--1111111",
        created_at="2026-01-01T00:00:00Z",
        payload="old",
    )
    newer = _write_snapshot(
        archive_root,
        "repo-a",
        "20260102T000000Z--repo-a--2222222",
        created_at="2026-01-02T00:00:00Z",
        payload="new",
    )
    in_progress = archive_root / "repo-a" / "20260103T000000Z--repo-a--3333333"
    _write(in_progress / "tickets" / "TICKET-999.md", "partial")

    summary = prune_worktree_archive_root(
        archive_root,
        policy=WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=1,
            max_age_days=365,
            max_total_bytes=1_000_000,
        ),
    )

    assert summary.pruned == 1
    assert not older.exists()
    assert newer.exists()
    assert in_progress.exists()


def test_prune_run_archive_root_respects_total_bytes(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive" / "runs"
    old_run = archive_root / "run-old"
    new_run = archive_root / "run-new"
    _write(old_run / "flow_state" / "event.json", "a" * 32)
    _write(new_run / "flow_state" / "event.json", "b" * 8)
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(minutes=2)).timestamp()
    new_ts = (now - timedelta(minutes=1)).timestamp()
    os.utime(old_run, (old_ts, old_ts))
    os.utime(new_run, (new_ts, new_ts))

    summary = prune_run_archive_root(
        archive_root,
        policy=RunArchiveRetentionPolicy(
            max_entries=10,
            max_age_days=100000,
            max_total_bytes=16,
        ),
    )

    assert summary.pruned == 1
    assert not old_run.exists()
    assert new_run.exists()


def test_resolve_worktree_archive_retention_policy_accepts_parsed_config_objects() -> (
    None
):
    policy = resolve_worktree_archive_retention_policy(
        SimpleNamespace(
            worktree_archive_max_snapshots_per_repo=7,
            worktree_archive_max_age_days=14,
            worktree_archive_max_total_bytes=42,
        )
    )

    assert policy == WorktreeArchiveRetentionPolicy(
        max_snapshots_per_repo=7,
        max_age_days=14,
        max_total_bytes=42,
    )


def test_resolve_run_archive_retention_policy_accepts_mapping_defaults() -> None:
    policy = resolve_run_archive_retention_policy(
        {
            "run_archive_max_entries": "12",
            "run_archive_max_age_days": None,
            "run_archive_max_total_bytes": "256",
        }
    )

    assert policy == RunArchiveRetentionPolicy(
        max_entries=12,
        max_age_days=30,
        max_total_bytes=256,
    )


class TestWorktreeArchiveDryRunExecuteParity:
    def test_dry_run_and_execute_same_kept_pruned_counts(self, tmp_path: Path) -> None:
        archive_root_a = tmp_path / "dry" / "archive" / "worktrees"
        archive_root_b = tmp_path / "exec" / "archive" / "worktrees"

        for root in (archive_root_a, archive_root_b):
            _write_snapshot(
                root,
                "repo-a",
                "20260101T000000Z--repo-a--1111111",
                created_at="2026-01-01T00:00:00Z",
                payload="old",
            )
            _write_snapshot(
                root,
                "repo-a",
                "20260102T000000Z--repo-a--2222222",
                created_at="2026-01-02T00:00:00Z",
                payload="newer",
            )
            _write_snapshot(
                root,
                "repo-a",
                "20260103T000000Z--repo-a--3333333",
                created_at="2026-01-03T00:00:00Z",
                payload="newest",
            )

        policy = WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=1,
            max_age_days=365,
            max_total_bytes=1_000_000,
        )

        dry_summary = prune_worktree_archive_root(
            archive_root_a, policy=policy, dry_run=True
        )
        exec_summary = prune_worktree_archive_root(
            archive_root_b, policy=policy, dry_run=False
        )

        assert dry_summary.kept == exec_summary.kept
        assert dry_summary.pruned == exec_summary.pruned
        assert dry_summary.pruned == 2
        assert dry_summary.kept == 1

    def test_dry_run_preserves_all_files_execute_deletes(self, tmp_path: Path) -> None:
        archive_root = tmp_path / "archive" / "worktrees"
        oldest = _write_snapshot(
            archive_root,
            "repo-x",
            "20260101T000000Z--repo-x--1111111",
            created_at="2026-01-01T00:00:00Z",
            payload="old",
        )
        newest = _write_snapshot(
            archive_root,
            "repo-x",
            "20260102T000000Z--repo-x--2222222",
            created_at="2026-01-02T00:00:00Z",
            payload="new",
        )

        policy = WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=1,
            max_age_days=365,
            max_total_bytes=1_000_000,
        )

        dry_summary = prune_worktree_archive_root(
            archive_root, policy=policy, dry_run=True
        )
        assert dry_summary.pruned == 1
        assert oldest.exists()
        assert newest.exists()

    def test_parity_with_byte_budget(self, tmp_path: Path) -> None:
        archive_root_a = tmp_path / "dry" / "archive" / "worktrees"
        archive_root_b = tmp_path / "exec" / "archive" / "worktrees"

        for root in (archive_root_a, archive_root_b):
            _write_snapshot(
                root,
                "repo-a",
                "20260101T000000Z--repo-a--1111111",
                created_at="2026-01-01T00:00:00Z",
                payload="a" * 500,
            )

        policy = WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=10,
            max_age_days=365,
            max_total_bytes=10,
        )

        dry_summary = prune_worktree_archive_root(
            archive_root_a, policy=policy, dry_run=True
        )
        exec_summary = prune_worktree_archive_root(
            archive_root_b, policy=policy, dry_run=False
        )

        assert dry_summary.pruned == exec_summary.pruned
        assert dry_summary.kept == exec_summary.kept

    def test_parity_with_multiple_repos(self, tmp_path: Path) -> None:
        archive_root_a = tmp_path / "dry" / "archive" / "worktrees"
        archive_root_b = tmp_path / "exec" / "archive" / "worktrees"

        for root in (archive_root_a, archive_root_b):
            _write_snapshot(
                root,
                "repo-a",
                "20260101T000000Z--repo-a--1111111",
                created_at="2026-01-01T00:00:00Z",
                payload="old-a",
            )
            _write_snapshot(
                root,
                "repo-a",
                "20260102T000000Z--repo-a--2222222",
                created_at="2026-01-02T00:00:00Z",
                payload="new-a",
            )
            _write_snapshot(
                root,
                "repo-b",
                "20260101T000000Z--repo-b--1111111",
                created_at="2026-01-01T00:00:00Z",
                payload="old-b",
            )

        policy = WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=1,
            max_age_days=365,
            max_total_bytes=1_000_000,
        )

        dry_summary = prune_worktree_archive_root(
            archive_root_a, policy=policy, dry_run=True
        )
        exec_summary = prune_worktree_archive_root(
            archive_root_b, policy=policy, dry_run=False
        )

        assert dry_summary.pruned == exec_summary.pruned == 1
        assert dry_summary.kept == exec_summary.kept == 2


class TestRunArchiveDryRunExecuteParity:
    def test_dry_run_and_execute_same_counts(self, tmp_path: Path) -> None:
        archive_root_a = tmp_path / "dry" / "archive" / "runs"
        archive_root_b = tmp_path / "exec" / "archive" / "runs"

        now = datetime.now(timezone.utc)
        for root in (archive_root_a, archive_root_b):
            old_run = root / "run-old"
            new_run = root / "run-new"
            _write(old_run / "flow_state" / "event.json", "a" * 32)
            _write(new_run / "flow_state" / "event.json", "b" * 8)
            old_ts = (now - timedelta(minutes=2)).timestamp()
            new_ts = (now - timedelta(minutes=1)).timestamp()
            os.utime(old_run, (old_ts, old_ts))
            os.utime(new_run, (new_ts, new_ts))

        policy = RunArchiveRetentionPolicy(
            max_entries=1,
            max_age_days=100000,
            max_total_bytes=1_000_000,
        )

        dry_summary = prune_run_archive_root(
            archive_root_a, policy=policy, dry_run=True
        )
        exec_summary = prune_run_archive_root(
            archive_root_b, policy=policy, dry_run=False
        )

        assert dry_summary.kept == exec_summary.kept == 1
        assert dry_summary.pruned == exec_summary.pruned == 1

    def test_dry_run_preserves_files_execute_deletes(self, tmp_path: Path) -> None:
        archive_root = tmp_path / "archive" / "runs"
        now = datetime.now(timezone.utc)
        old_run = archive_root / "run-old"
        new_run = archive_root / "run-new"
        _write(old_run / "data.json", "old")
        _write(new_run / "data.json", "new")
        old_ts = (now - timedelta(minutes=2)).timestamp()
        new_ts = (now - timedelta(minutes=1)).timestamp()
        os.utime(old_run, (old_ts, old_ts))
        os.utime(new_run, (new_ts, new_ts))

        policy = RunArchiveRetentionPolicy(
            max_entries=1,
            max_age_days=100000,
            max_total_bytes=1_000_000,
        )

        dry_summary = prune_run_archive_root(archive_root, policy=policy, dry_run=True)
        assert dry_summary.pruned == 1
        assert old_run.exists()
        assert new_run.exists()

    def test_run_archive_prune_paths_sorted(self, tmp_path: Path) -> None:
        archive_root = tmp_path / "archive" / "runs"
        now = datetime.now(timezone.utc)
        for i in range(5):
            run_dir = archive_root / f"run-{i:03d}"
            _write(run_dir / "data.json", f"run-{i}")
            ts = (now - timedelta(minutes=5 - i)).timestamp()
            os.utime(run_dir, (ts, ts))

        policy = RunArchiveRetentionPolicy(
            max_entries=2,
            max_age_days=100000,
            max_total_bytes=1_000_000,
        )

        summary = prune_run_archive_root(archive_root, policy=policy, dry_run=True)
        assert summary.pruned == 3
        paths = summary.pruned_paths
        assert paths == tuple(sorted(paths))


class TestWorktreeArchiveLargeFixture:
    def test_many_repos_each_respects_own_count_budget(self, tmp_path: Path) -> None:
        archive_root = tmp_path / "archive" / "worktrees"
        repo_count = 5
        snapshots_per_repo = 8
        keep_per_repo = 2

        for r in range(repo_count):
            repo_id = f"repo-{r}"
            for s in range(snapshots_per_repo):
                day = s + 1
                _write_snapshot(
                    archive_root,
                    repo_id,
                    f"202601{day:02d}T000000Z--{repo_id}--{s:07d}",
                    created_at=f"2026-01-{day:02d}T00:00:00Z",
                    payload=f"repo-{r}-snap-{s}",
                )

        policy = WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=keep_per_repo,
            max_age_days=365,
            max_total_bytes=1_000_000_000,
        )

        dry_summary = prune_worktree_archive_root(
            archive_root, policy=policy, dry_run=True
        )
        exec_summary = prune_worktree_archive_root(
            archive_root, policy=policy, dry_run=False
        )

        expected_pruned = repo_count * (snapshots_per_repo - keep_per_repo)
        expected_kept = repo_count * keep_per_repo
        assert dry_summary.pruned == exec_summary.pruned == expected_pruned
        assert dry_summary.kept == exec_summary.kept == expected_kept

        for r in range(repo_count):
            repo_id = f"repo-{r}"
            remaining = sorted((archive_root / repo_id).iterdir(), key=lambda p: p.name)
            assert len(remaining) == keep_per_repo

    def test_byte_budget_applied_across_all_repos(self, tmp_path: Path) -> None:
        archive_root = tmp_path / "archive" / "worktrees"
        large_payload = "x" * 200

        for repo_idx in range(3):
            repo_id = f"repo-{repo_idx}"
            for snap_idx in range(3):
                day = snap_idx + 1
                _write_snapshot(
                    archive_root,
                    repo_id,
                    f"202601{day:02d}T000000Z--{repo_id}--{snap_idx:07d}",
                    created_at=f"2026-01-{day:02d}T00:00:00Z",
                    payload=large_payload,
                )

        policy = WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=10,
            max_age_days=365,
            max_total_bytes=100,
        )

        dry_summary = prune_worktree_archive_root(
            archive_root, policy=policy, dry_run=True
        )
        exec_summary = prune_worktree_archive_root(
            archive_root, policy=policy, dry_run=False
        )

        assert dry_summary.pruned == exec_summary.pruned
        assert dry_summary.kept == exec_summary.kept
        assert exec_summary.bytes_after <= 100 or exec_summary.kept == 0

    def test_age_based_pruning_across_mixed_ages(self, tmp_path: Path) -> None:
        archive_root = tmp_path / "archive" / "worktrees"
        ages_days = [1, 5, 10, 20, 40, 60, 90]

        for i, _age in enumerate(ages_days):
            _write_snapshot(
                archive_root,
                "repo-a",
                f"2026-01-{i + 1:02d}T00:00:00Z--repo-a--{i:07d}",
                created_at=f"2026-01-{i + 1:02d}T00:00:00Z",
                payload=f"snap-{i}",
            )

        policy = WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=100,
            max_age_days=30,
            max_total_bytes=1_000_000_000,
        )

        summary = prune_worktree_archive_root(
            archive_root, policy=policy, dry_run=False
        )

        assert summary.pruned > 0
        for pruned_path_str in summary.pruned_paths:
            p = Path(pruned_path_str)
            assert not p.exists()

    def test_incomplete_snapshots_preserved_regardless_of_policy(
        self, tmp_path: Path
    ) -> None:
        archive_root = tmp_path / "archive" / "worktrees"

        for i in range(3):
            _write_snapshot(
                archive_root,
                "repo-a",
                f"2026010{i + 1}T000000Z--repo-a--{i:07d}",
                created_at=f"2026-01-0{i + 1}T00:00:00Z",
                payload=f"snap-{i}",
            )

        incomplete_dirs = []
        for i in range(3):
            d = archive_root / "repo-a" / f"incomplete-{i}"
            _write(d / "tickets" / "TICKET-001.md", "partial")
            incomplete_dirs.append(d)

        policy = WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=1,
            max_age_days=0,
            max_total_bytes=0,
        )

        prune_worktree_archive_root(archive_root, policy=policy, dry_run=False)

        for d in incomplete_dirs:
            assert d.exists(), f"Incomplete snapshot {d.name} should be preserved"

    def test_dry_run_execute_byte_accounting_parity_many_snapshots(
        self, tmp_path: Path
    ) -> None:
        archive_root_a = tmp_path / "dry" / "archive" / "worktrees"
        archive_root_b = tmp_path / "exec" / "archive" / "worktrees"

        for root in (archive_root_a, archive_root_b):
            for r in range(3):
                repo_id = f"repo-{r}"
                for s in range(5):
                    day = s + 1
                    _write_snapshot(
                        root,
                        repo_id,
                        f"202601{day:02d}T000000Z--{repo_id}--{s:07d}",
                        created_at=f"2026-01-{day:02d}T00:00:00Z",
                        payload="x" * (50 + s * 10),
                    )

        policy = WorktreeArchiveRetentionPolicy(
            max_snapshots_per_repo=2,
            max_age_days=365,
            max_total_bytes=500,
        )

        dry_summary = prune_worktree_archive_root(
            archive_root_a, policy=policy, dry_run=True
        )
        exec_summary = prune_worktree_archive_root(
            archive_root_b, policy=policy, dry_run=False
        )

        assert dry_summary.bytes_before == exec_summary.bytes_before
        assert dry_summary.pruned == exec_summary.pruned
        assert dry_summary.kept == exec_summary.kept


class TestRunArchiveLargeFixture:
    def test_many_entries_respects_count_budget(self, tmp_path: Path) -> None:
        archive_root = tmp_path / "archive" / "runs"
        now = datetime.now(timezone.utc)
        entry_count = 50
        keep_count = 10

        for i in range(entry_count):
            run_dir = archive_root / f"run-{i:04d}"
            _write(run_dir / "data.json", f"run-{i}")
            ts = (now - timedelta(minutes=entry_count - i)).timestamp()
            os.utime(run_dir, (ts, ts))

        policy = RunArchiveRetentionPolicy(
            max_entries=keep_count,
            max_age_days=100000,
            max_total_bytes=1_000_000_000,
        )

        dry_summary = prune_run_archive_root(archive_root, policy=policy, dry_run=True)
        exec_summary = prune_run_archive_root(
            archive_root, policy=policy, dry_run=False
        )

        expected_pruned = entry_count - keep_count
        assert dry_summary.pruned == exec_summary.pruned == expected_pruned
        assert dry_summary.kept == exec_summary.kept == keep_count

        remaining = sorted(p.name for p in archive_root.iterdir() if p.is_dir())
        assert len(remaining) == keep_count

    def test_age_based_pruning_removes_old_entries(self, tmp_path: Path) -> None:
        archive_root = tmp_path / "archive" / "runs"
        now = datetime.now(timezone.utc)

        for i in range(10):
            run_dir = archive_root / f"run-{i:04d}"
            _write(run_dir / "data.json", f"run-{i}")
            age_days = 60 - i * 5
            ts = (now - timedelta(days=age_days)).timestamp()
            os.utime(run_dir, (ts, ts))

        policy = RunArchiveRetentionPolicy(
            max_entries=100,
            max_age_days=30,
            max_total_bytes=1_000_000_000,
        )

        summary = prune_run_archive_root(archive_root, policy=policy, dry_run=False)

        assert summary.pruned > 0
        for path_str in summary.pruned_paths:
            assert not Path(path_str).exists()

    def test_dry_run_execute_parity_with_many_entries(self, tmp_path: Path) -> None:
        archive_root_a = tmp_path / "dry" / "archive" / "runs"
        archive_root_b = tmp_path / "exec" / "archive" / "runs"
        now = datetime.now(timezone.utc)

        for root in (archive_root_a, archive_root_b):
            for i in range(20):
                run_dir = root / f"run-{i:04d}"
                _write(run_dir / "flow_state" / "event.json", "x" * (20 + i))
                ts = (now - timedelta(minutes=20 - i)).timestamp()
                os.utime(run_dir, (ts, ts))

        policy = RunArchiveRetentionPolicy(
            max_entries=5,
            max_age_days=100000,
            max_total_bytes=200,
        )

        dry_summary = prune_run_archive_root(
            archive_root_a, policy=policy, dry_run=True
        )
        exec_summary = prune_run_archive_root(
            archive_root_b, policy=policy, dry_run=False
        )

        assert dry_summary.pruned == exec_summary.pruned
        assert dry_summary.kept == exec_summary.kept
        assert dry_summary.bytes_before == exec_summary.bytes_before
