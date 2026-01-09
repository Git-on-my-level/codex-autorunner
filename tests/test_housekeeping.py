from __future__ import annotations

import os
import time
from pathlib import Path

from codex_autorunner.housekeeping import (
    HousekeepingConfig,
    HousekeepingRule,
    run_housekeeping_for_roots,
    run_housekeeping_once,
)


def _write_file(path: Path, payload: bytes, mtime: float) -> None:
    path.write_bytes(payload)
    os.utime(path, (mtime, mtime))


def test_directory_rule_max_files_deletes_oldest(tmp_path: Path) -> None:
    base = tmp_path / "uploads"
    base.mkdir()
    now = time.time()
    oldest = base / "a.txt"
    middle = base / "b.txt"
    newest = base / "c.txt"
    _write_file(oldest, b"a", now - 300)
    _write_file(middle, b"b", now - 200)
    _write_file(newest, b"c", now - 100)

    config = HousekeepingConfig(
        enabled=True,
        interval_seconds=1,
        min_file_age_seconds=0,
        dry_run=False,
        rules=[
            HousekeepingRule(
                name="max_files",
                kind="directory",
                path=str(base),
                glob="*.txt",
                max_files=1,
            )
        ],
    )

    summary = run_housekeeping_once(config, tmp_path)
    result = summary.rules[0]
    assert result.deleted_count == 2
    assert newest.exists()
    assert not oldest.exists()


def test_directory_rule_respects_min_age(tmp_path: Path) -> None:
    base = tmp_path / "runs"
    base.mkdir()
    now = time.time()
    target = base / "run.log"
    _write_file(target, b"log", now)

    config = HousekeepingConfig(
        enabled=True,
        interval_seconds=1,
        min_file_age_seconds=3600,
        dry_run=False,
        rules=[
            HousekeepingRule(
                name="min_age",
                kind="directory",
                path=str(base),
                glob="*.log",
                max_files=0,
            )
        ],
    )

    summary = run_housekeeping_once(config, tmp_path)
    result = summary.rules[0]
    assert result.deleted_count == 0
    assert target.exists()


def test_directory_rule_dry_run_does_not_delete(tmp_path: Path) -> None:
    base = tmp_path / "cache"
    base.mkdir()
    now = time.time()
    target = base / "item.txt"
    _write_file(target, b"payload", now - 1000)

    config = HousekeepingConfig(
        enabled=True,
        interval_seconds=1,
        min_file_age_seconds=0,
        dry_run=True,
        rules=[
            HousekeepingRule(
                name="dry_run",
                kind="directory",
                path=str(base),
                glob="*.txt",
                max_files=0,
            )
        ],
    )

    summary = run_housekeeping_once(config, tmp_path)
    result = summary.rules[0]
    assert result.deleted_count == 1
    assert target.exists()


def test_file_rule_truncates_tail_bytes(tmp_path: Path) -> None:
    target = tmp_path / "update.log"
    target.write_bytes(b"abcdefghij")

    config = HousekeepingConfig(
        enabled=True,
        interval_seconds=1,
        min_file_age_seconds=0,
        dry_run=False,
        rules=[
            HousekeepingRule(
                name="truncate_bytes",
                kind="file",
                path=str(target),
                max_bytes=4,
            )
        ],
    )

    summary = run_housekeeping_once(config, tmp_path)
    result = summary.rules[0]
    assert target.read_bytes() == b"ghij"
    assert result.truncated_bytes > 0


def test_run_housekeeping_for_roots_skips_absolute_after_first(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    target = tmp_path / "absolute.log"
    target.write_bytes(b"abcd")

    config = HousekeepingConfig(
        enabled=True,
        interval_seconds=1,
        min_file_age_seconds=0,
        dry_run=False,
        rules=[
            HousekeepingRule(
                name="absolute_file",
                kind="file",
                path=str(target),
                max_bytes=2,
            )
        ],
    )

    summaries = run_housekeeping_for_roots(config, [root_a, root_b])
    assert len(summaries) == 2
    assert len(summaries[0].rules) == 1
    assert summaries[1].rules == []
