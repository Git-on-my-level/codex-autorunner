from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codex_autorunner.core.flows import worker_process


def _disable_proc_cmdline_probe(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    original_exists = worker_process.Path.exists

    def fake_exists(path_obj: Path) -> bool:
        path_text = str(path_obj)
        if path_text.startswith("/proc/") and path_text.endswith("/cmdline"):
            return False
        return original_exists(path_obj)

    monkeypatch.setattr(worker_process.Path, "exists", fake_exists)


def test_check_worker_health_prefers_metadata_cmdline(monkeypatch, tmp_path):
    """Worker health should trust stored cmdline, not the current interpreter."""

    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    artifacts_dir = worker_process._worker_artifacts_dir(tmp_path, run_id)

    stored_cmd = [
        f"{sys.executable}-other",
        "-m",
        "codex_autorunner",
        "flow",
        "worker",
        "--repo",
        str(tmp_path),
        "--run-id",
        worker_process._normalized_run_id(run_id),
    ]
    # Sanity-check that we're simulating a different interpreter than the test runner.
    assert stored_cmd[0] != sys.executable

    worker_process._write_worker_metadata(
        worker_process._worker_metadata_path(artifacts_dir),
        pid=12345,
        cmd=stored_cmd,
        repo_root=tmp_path,
    )

    monkeypatch.setattr(worker_process, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(
        worker_process, "_read_process_cmdline", lambda pid: list(stored_cmd)
    )

    health = worker_process.check_worker_health(tmp_path, run_id)

    assert health.status == "alive"
    assert health.cmdline == stored_cmd


def test_cmdline_matches_when_executable_resolves_differently(tmp_path: Path) -> None:
    real_exec = tmp_path / "python-real"
    real_exec.write_text("#!/bin/sh\n", encoding="utf-8")
    real_exec.chmod(0o755)
    alias_exec = tmp_path / "python-alias"
    alias_exec.symlink_to(real_exec)

    expected = [
        str(alias_exec),
        "-m",
        "codex_autorunner",
        "flow",
        "worker",
        "--repo",
        str(tmp_path),
        "--run-id",
        "3022db08-82b8-40dd-8cfa-d04eb0fcded2",
    ]
    actual = [str(real_exec), *expected[1:]]

    assert worker_process._cmdline_matches(expected, actual)


def test_read_process_cmdline_uses_wide_ps(monkeypatch) -> None:
    _disable_proc_cmdline_probe(monkeypatch)
    seen: list[list[str]] = []

    def fake_check_output(cmd: list[str], stderr=None):  # type: ignore[no-untyped-def]
        seen.append(list(cmd))
        return b"python -m codex_autorunner flow worker --repo /tmp --run-id test"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    cmdline = worker_process._read_process_cmdline(123)

    assert cmdline is not None
    assert seen
    assert seen[0] == ["ps", "-ww", "-p", "123", "-o", "command="]


def test_read_process_cmdline_falls_back_without_wide_flag(monkeypatch) -> None:
    _disable_proc_cmdline_probe(monkeypatch)
    seen: list[list[str]] = []

    def fake_check_output(cmd: list[str], stderr=None):  # type: ignore[no-untyped-def]
        seen.append(list(cmd))
        if cmd[:2] == ["ps", "-ww"]:
            raise subprocess.CalledProcessError(returncode=1, cmd=cmd)
        return b"python -m codex_autorunner flow worker --repo /tmp --run-id test"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    cmdline = worker_process._read_process_cmdline(456)

    assert cmdline is not None
    assert seen[0] == ["ps", "-ww", "-p", "456", "-o", "command="]
    assert seen[1] == ["ps", "-p", "456", "-o", "command="]


def test_spawn_flow_worker_closes_streams_when_popen_fails(
    monkeypatch, tmp_path: Path
) -> None:
    class DummyHandle:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    opened: list[DummyHandle] = []

    def fake_open(_self, _mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = DummyHandle()
        opened.append(handle)
        return handle

    def fail_popen(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(worker_process.Path, "open", fake_open)
    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    with pytest.raises(RuntimeError, match="spawn failed"):
        worker_process.spawn_flow_worker(tmp_path, run_id)

    assert len(opened) == 2
    assert all(handle.closed for handle in opened)
