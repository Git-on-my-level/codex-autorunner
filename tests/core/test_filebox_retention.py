from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.filebox import (
    inbox_dir,
    outbox_dir,
    outbox_pending_dir,
    outbox_sent_dir,
)
from codex_autorunner.core.filebox_lifecycle import consumed_dir, dismissed_dir
from codex_autorunner.core.filebox_retention import (
    FileBoxRetentionPolicy,
    prune_filebox_root,
    resolve_filebox_retention_policy,
)


def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _set_mtime(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    path.touch(exist_ok=True)
    path.chmod(0o644)
    import os

    os.utime(path, (ts, ts))


def test_prune_filebox_root_removes_stale_files_across_inbox_and_outbox(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 21, tzinfo=timezone.utc)
    stale = now - timedelta(days=8)
    fresh = now - timedelta(days=2)

    stale_inbox = _write(inbox_dir(tmp_path) / "stale-inbox.txt", b"inbox")
    fresh_inbox = _write(inbox_dir(tmp_path) / "fresh-inbox.txt", b"inbox")
    stale_outbox = _write(outbox_dir(tmp_path) / "stale-outbox.txt", b"outbox")
    stale_pending = _write(
        outbox_pending_dir(tmp_path) / "stale-pending.txt", b"pending"
    )
    fresh_sent = _write(outbox_sent_dir(tmp_path) / "fresh-sent.txt", b"sent")

    for path, when in (
        (stale_inbox, stale),
        (fresh_inbox, fresh),
        (stale_outbox, stale),
        (stale_pending, stale),
        (fresh_sent, fresh),
    ):
        _set_mtime(path, when)

    summary = prune_filebox_root(
        tmp_path,
        policy=FileBoxRetentionPolicy(inbox_max_age_days=7, outbox_max_age_days=7),
        now=now,
    )

    assert summary.inbox_pruned == 1
    assert summary.outbox_pruned == 2
    assert not stale_inbox.exists()
    assert fresh_inbox.exists()
    assert not stale_outbox.exists()
    assert not stale_pending.exists()
    assert fresh_sent.exists()


def test_prune_filebox_root_dry_run_preserves_files(tmp_path: Path) -> None:
    now = datetime(2026, 3, 21, tzinfo=timezone.utc)
    stale = now - timedelta(days=8)
    path = _write(outbox_sent_dir(tmp_path) / "artifact.txt", b"artifact")
    _set_mtime(path, stale)

    summary = prune_filebox_root(
        tmp_path,
        policy=FileBoxRetentionPolicy(inbox_max_age_days=7, outbox_max_age_days=7),
        scope="outbox",
        dry_run=True,
        now=now,
    )

    assert summary.inbox_pruned == 0
    assert summary.outbox_pruned == 1
    assert path.exists()


def test_prune_filebox_root_skips_symlinks(tmp_path: Path) -> None:
    now = datetime(2026, 3, 21, tzinfo=timezone.utc)
    stale = now - timedelta(days=8)
    target = _write(outbox_dir(tmp_path) / "artifact.txt", b"artifact")
    _set_mtime(target, stale)
    symlink = outbox_pending_dir(tmp_path) / "artifact-link.txt"
    symlink.parent.mkdir(parents=True, exist_ok=True)
    try:
        symlink.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")

    summary = prune_filebox_root(
        tmp_path,
        policy=FileBoxRetentionPolicy(inbox_max_age_days=7, outbox_max_age_days=7),
        scope="outbox",
        now=now,
    )

    assert summary.outbox_pruned == 1
    assert not target.exists()
    assert symlink.is_symlink()


def test_prune_filebox_root_leaves_archived_inbox_files_recoverable(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 21, tzinfo=timezone.utc)
    stale = now - timedelta(days=30)
    consumed = _write(consumed_dir(tmp_path) / "done.txt", b"done")
    dismissed = _write(dismissed_dir(tmp_path) / "skip.txt", b"skip")
    _set_mtime(consumed, stale)
    _set_mtime(dismissed, stale)

    summary = prune_filebox_root(
        tmp_path,
        policy=FileBoxRetentionPolicy(inbox_max_age_days=7, outbox_max_age_days=7),
        now=now,
    )

    assert summary.inbox_pruned == 0
    assert summary.outbox_pruned == 0
    assert consumed.exists()
    assert dismissed.exists()


def test_resolve_filebox_retention_policy_supports_mapping_and_object() -> None:
    assert resolve_filebox_retention_policy(
        {"filebox_inbox_max_age_days": "9", "filebox_outbox_max_age_days": 11}
    ) == FileBoxRetentionPolicy(inbox_max_age_days=9, outbox_max_age_days=11)
    assert resolve_filebox_retention_policy(
        SimpleNamespace(filebox_inbox_max_age_days=5, filebox_outbox_max_age_days=6)
    ) == FileBoxRetentionPolicy(inbox_max_age_days=5, outbox_max_age_days=6)


class TestFileBoxDryRunExecuteParity:
    def test_dry_run_and_execute_same_counts(self, tmp_path: Path) -> None:
        now = datetime(2026, 3, 21, tzinfo=timezone.utc)
        stale = now - timedelta(days=8)
        fresh = now - timedelta(days=2)

        repo_a = tmp_path / "dry"
        repo_b = tmp_path / "exec"

        for repo in (repo_a, repo_b):
            for name in ("stale-in.txt", "fresh-in.txt"):
                p = _write(inbox_dir(repo) / name, b"inbox")
                ts = stale if name.startswith("stale") else fresh
                _set_mtime(p, ts)
            for name in ("stale-out.txt", "fresh-out.txt"):
                p = _write(outbox_sent_dir(repo) / name, b"outbox")
                ts = stale if name.startswith("stale") else fresh
                _set_mtime(p, ts)

        policy = FileBoxRetentionPolicy(inbox_max_age_days=7, outbox_max_age_days=7)

        dry_summary = prune_filebox_root(repo_a, policy=policy, dry_run=True, now=now)
        exec_summary = prune_filebox_root(repo_b, policy=policy, dry_run=False, now=now)

        assert dry_summary.inbox_pruned == exec_summary.inbox_pruned == 1
        assert dry_summary.inbox_kept == exec_summary.inbox_kept == 1
        assert dry_summary.outbox_pruned == exec_summary.outbox_pruned == 1
        assert dry_summary.outbox_kept == exec_summary.outbox_kept == 1
        assert dry_summary.bytes_before == exec_summary.bytes_before

    def test_dry_run_preserves_all_files(self, tmp_path: Path) -> None:
        now = datetime(2026, 3, 21, tzinfo=timezone.utc)
        stale = now - timedelta(days=8)

        stale_file = _write(inbox_dir(tmp_path) / "stale.txt", b"data")
        _set_mtime(stale_file, stale)

        policy = FileBoxRetentionPolicy(inbox_max_age_days=7, outbox_max_age_days=7)
        summary = prune_filebox_root(tmp_path, policy=policy, dry_run=True, now=now)

        assert summary.inbox_pruned == 1
        assert stale_file.exists()

    def test_dry_run_and_execute_parity_with_scoped_inbox_only(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 3, 21, tzinfo=timezone.utc)
        stale = now - timedelta(days=8)

        repo_a = tmp_path / "dry"
        repo_b = tmp_path / "exec"

        for repo in (repo_a, repo_b):
            p = _write(inbox_dir(repo) / "stale.txt", b"inbox")
            _set_mtime(p, stale)
            p = _write(outbox_sent_dir(repo) / "stale.txt", b"outbox")
            _set_mtime(p, stale)

        policy = FileBoxRetentionPolicy(inbox_max_age_days=7, outbox_max_age_days=7)

        dry_summary = prune_filebox_root(
            repo_a, policy=policy, scope="inbox", dry_run=True, now=now
        )
        exec_summary = prune_filebox_root(
            repo_b, policy=policy, scope="inbox", dry_run=False, now=now
        )

        assert dry_summary.inbox_pruned == exec_summary.inbox_pruned == 1
        assert dry_summary.outbox_pruned == exec_summary.outbox_pruned == 0

    def test_pruned_paths_listed_in_summary(self, tmp_path: Path) -> None:
        now = datetime(2026, 3, 21, tzinfo=timezone.utc)
        stale = now - timedelta(days=8)

        stale_a = _write(inbox_dir(tmp_path) / "stale-a.txt", b"a")
        stale_b = _write(inbox_dir(tmp_path) / "stale-b.txt", b"b")
        _set_mtime(stale_a, stale)
        _set_mtime(stale_b, stale)

        policy = FileBoxRetentionPolicy(inbox_max_age_days=7, outbox_max_age_days=7)
        summary = prune_filebox_root(tmp_path, policy=policy, dry_run=False, now=now)

        assert len(summary.pruned_paths) == 2
        pruned_names = {Path(p).name for p in summary.pruned_paths}
        assert "stale-a.txt" in pruned_names
        assert "stale-b.txt" in pruned_names
