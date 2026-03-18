from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_autorunner.core.archive_retention import (
    RunArchiveRetentionPolicy,
    WorktreeArchiveRetentionPolicy,
    prune_run_archive_root,
    prune_worktree_archive_root,
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
