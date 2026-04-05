from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codex_autorunner.core.pytest_temp_cleanup import (
    TempPathScanResult,
    TempRootProcess,
    cleanup_repo_pytest_temp_runs,
    cleanup_temp_paths,
    find_processes_using_path,
    repo_pytest_temp_root,
    scan_temp_path,
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


def test_scan_temp_path_fails_closed_when_lsof_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "active"
    root.mkdir()
    (root / "payload.txt").write_text("payload", encoding="utf-8")

    def _raise_file_not_found(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(subprocess, "run", _raise_file_not_found)

    scan = scan_temp_path(root)

    assert scan.active_processes == ()
    assert scan.scan_error == "lsof is unavailable"


def test_find_processes_using_path_parses_stdout_when_lsof_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "active"
    root.mkdir()

    def _completed_process(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout=b"p123\0cpython\0fcwd\0n/tmp/active\0",
            stderr=b"permission denied\n",
        )

    monkeypatch.setattr(subprocess, "run", _completed_process)

    processes = find_processes_using_path(root)

    assert processes == (
        TempRootProcess(
            pid=123,
            command="python",
            descriptor="cwd",
            path="/tmp/active",
        ),
    )
