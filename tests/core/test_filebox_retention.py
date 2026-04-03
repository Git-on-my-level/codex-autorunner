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
