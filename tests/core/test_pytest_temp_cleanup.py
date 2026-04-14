from __future__ import annotations

import errno
from pathlib import Path

from codex_autorunner.core import pytest_temp_cleanup as cleanup_module
from codex_autorunner.core.pytest_temp_cleanup import (
    TempPathScanResult,
    TempRootProcess,
    cleanup_repo_pytest_temp_runs,
    cleanup_temp_paths,
    repo_pytest_temp_root,
)


def test_cleanup_repo_pytest_temp_runs_deletes_inactive_run_dirs(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    temp_root = repo_pytest_temp_root(repo_root, temp_base=tmp_path / "tmp")
    stale_run = temp_root / "stale"
    keep_run = temp_root / "keep"
    (stale_run / "data").mkdir(parents=True)
    (stale_run / "data" / "artifact.bin").write_bytes(b"1234")
    keep_run.mkdir(parents=True)

    summary = cleanup_repo_pytest_temp_runs(
        repo_root,
        keep_run_tokens={"keep"},
        temp_base=tmp_path / "tmp",
    )

    assert summary.scanned == 1
    assert summary.deleted == 1
    assert summary.active == 0
    assert stale_run.exists() is False
    assert keep_run.exists() is True


def test_cleanup_repo_pytest_temp_runs_skips_recent_run_dirs(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    temp_root = repo_pytest_temp_root(repo_root, temp_base=tmp_path / "tmp")
    recent_run = temp_root / "recent"
    (recent_run / "data").mkdir(parents=True)
    (recent_run / "data" / "artifact.bin").write_bytes(b"1234")

    summary = cleanup_repo_pytest_temp_runs(
        repo_root,
        temp_base=tmp_path / "tmp",
        min_age_seconds=300,
    )

    assert summary.scanned == 0
    assert summary.deleted == 0
    assert recent_run.exists() is True


def test_cleanup_temp_paths_skips_active_roots(tmp_path: Path) -> None:
    active_root = tmp_path / "active"
    active_root.mkdir()
    (active_root / "payload.txt").write_text("payload", encoding="utf-8")

    def _scan(path: Path) -> TempPathScanResult:
        return TempPathScanResult(
            path=path,
            bytes=7,
            active_processes=(
                TempRootProcess(
                    pid=123,
                    command="node",
                    descriptor="cwd",
                    path=str(path),
                ),
            ),
        )

    summary = cleanup_temp_paths((active_root,), scan_fn=_scan)

    assert summary.scanned == 1
    assert summary.deleted == 0
    assert summary.active == 1
    assert summary.failed == 0
    assert active_root.exists() is True
    assert summary.active_processes[0].command == "node"


def test_cleanup_temp_paths_tolerates_path_vanishing_after_delete_failure(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "stale"
    (target / "data").mkdir(parents=True)
    (target / "data" / "artifact.bin").write_bytes(b"1234")
    original_rmtree = cleanup_module.shutil.rmtree

    def _scan(path: Path) -> TempPathScanResult:
        return TempPathScanResult(path=path, bytes=4)

    def _rmtree_then_disappear(path: Path) -> None:
        original_rmtree(path)
        raise FileNotFoundError("already removed")

    monkeypatch.setattr(cleanup_module.shutil, "rmtree", _rmtree_then_disappear)

    summary = cleanup_temp_paths((target,), scan_fn=_scan)

    assert summary.scanned == 1
    assert summary.deleted == 0
    assert summary.failed == 1
    assert summary.bytes_after == 0
    assert target.exists() is False


def test_cleanup_temp_paths_retries_transient_directory_not_empty(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "stale"
    (target / "data").mkdir(parents=True)
    (target / "data" / "artifact.bin").write_bytes(b"1234")
    original_rmtree = cleanup_module.shutil.rmtree
    calls = {"count": 0}

    def _scan(path: Path) -> TempPathScanResult:
        return TempPathScanResult(path=path, bytes=4)

    def _rmtree_retry_then_succeed(path: Path) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError(errno.ENOTEMPTY, "Directory not empty")
        original_rmtree(path)

    monkeypatch.setattr(cleanup_module.shutil, "rmtree", _rmtree_retry_then_succeed)

    summary = cleanup_temp_paths((target,), scan_fn=_scan)

    assert calls["count"] == 2
    assert summary.scanned == 1
    assert summary.deleted == 1
    assert summary.failed == 0
    assert target.exists() is False
