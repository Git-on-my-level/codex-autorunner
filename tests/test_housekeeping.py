from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

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


def test_directory_rule_records_unlink_errors(tmp_path: Path) -> None:
    base = tmp_path / "uploads"
    base.mkdir()
    now = time.time()
    oldest = base / "a.txt"
    newest = base / "b.txt"
    _write_file(oldest, b"a", now - 300)
    _write_file(newest, b"b", now - 100)

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

    original_unlink = Path.unlink

    def unlink_raises(self, *args, **kwargs):
        if str(self).endswith("a.txt"):
            raise OSError(13, "Permission denied")
        return original_unlink(self, *args, **kwargs)

    with patch.object(Path, "unlink", unlink_raises):
        summary = run_housekeeping_once(config, tmp_path)
        result = summary.rules[0]
        assert result.errors == 1
        assert len(result.error_samples) == 1
        assert "unlink" in result.error_samples[0]
        assert "Permission denied" in result.error_samples[0]


def test_directory_rule_records_stat_errors(tmp_path: Path) -> None:
    base = tmp_path / "uploads"
    base.mkdir()
    now = time.time()
    oldest = base / "a.txt"
    _write_file(oldest, b"a", now - 300)

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
                max_files=0,
            )
        ],
    )

    original_stat = Path.stat

    def stat_raises(self, *args, **kwargs):
        if str(self).endswith("a.txt"):
            raise OSError(13, "Permission denied")
        return original_stat(self, *args, **kwargs)

    with patch.object(Path, "stat", stat_raises):
        summary = run_housekeeping_once(config, tmp_path)
        result = summary.rules[0]
        assert result.errors == 0
        assert len(result.error_samples) == 1
        assert "stat" in result.error_samples[0]
        assert "Permission denied" in result.error_samples[0]


def test_file_rule_records_truncate_errors(tmp_path: Path) -> None:
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

    def open_raises(self, *args, **kwargs):
        raise OSError(13, "Permission denied")

    with patch.object(Path, "open", open_raises):
        summary = run_housekeeping_once(config, tmp_path)
        result = summary.rules[0]
        assert result.errors == 1
        assert len(result.error_samples) == 1
        assert "truncate_bytes" in result.error_samples[0]
        assert "Permission denied" in result.error_samples[0]


def test_file_rule_records_truncate_lines_errors(tmp_path: Path) -> None:
    target = tmp_path / "update.log"
    target.write_bytes(b"line1\nline2\nline3\nline4\nline5\n")

    config = HousekeepingConfig(
        enabled=True,
        interval_seconds=1,
        min_file_age_seconds=0,
        dry_run=False,
        rules=[
            HousekeepingRule(
                name="truncate_lines",
                kind="file",
                path=str(target),
                max_lines=2,
            )
        ],
    )

    def open_raises(self, *args, **kwargs):
        raise OSError(13, "Permission denied")

    with patch.object(Path, "open", open_raises):
        summary = run_housekeeping_once(config, tmp_path)
        result = summary.rules[0]
        assert result.errors == 1
        assert len(result.error_samples) == 1
        assert "truncate_lines" in result.error_samples[0]
        assert "Permission denied" in result.error_samples[0]


def test_error_samples_are_bounded(tmp_path: Path) -> None:
    base = tmp_path / "uploads"
    base.mkdir()
    now = time.time()
    for i in range(10):
        file_path = base / f"{i}.txt"
        _write_file(file_path, b"x", now - 300)

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
                max_files=0,
            )
        ],
    )

    def unlink_raises(self, *args, **kwargs):
        raise OSError(13, "Permission denied")

    with patch.object(Path, "unlink", unlink_raises):
        summary = run_housekeeping_once(config, tmp_path)
        result = summary.rules[0]
        assert result.errors == 10
        assert len(result.error_samples) == 5


def test_error_samples_included_in_log_events(tmp_path: Path, caplog) -> None:
    base = tmp_path / "uploads"
    base.mkdir()
    now = time.time()
    oldest = base / "a.txt"
    newest = base / "b.txt"
    _write_file(oldest, b"a", now - 300)
    _write_file(newest, b"b", now - 100)

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

    original_unlink = Path.unlink

    def unlink_raises(self, *args, **kwargs):
        if str(self).endswith("a.txt"):
            raise OSError(13, "Permission denied")
        return original_unlink(self, *args, **kwargs)

    logger = logging.getLogger("test_housekeeping")

    with patch.object(Path, "unlink", unlink_raises):
        with caplog.at_level(logging.INFO):
            summary = run_housekeeping_once(config, tmp_path, logger=logger)
            result = summary.rules[0]

    assert result.errors == 1
    assert len(result.error_samples) == 1

    log_messages = [
        json.loads(r.getMessage())
        for r in caplog.records
        if "housekeeping.rule" in r.getMessage()
    ]
    assert len(log_messages) == 1
    log_data = log_messages[0]
    assert log_data["event"] == "housekeeping.rule"
    assert "error_samples" in log_data
    assert log_data["error_samples"] == result.error_samples
