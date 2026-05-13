from __future__ import annotations

import json
import signal
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


def test_stale_alive_health_still_counts_as_alive(tmp_path: Path) -> None:
    health = worker_process.FlowWorkerHealth(
        status="stale_alive",
        pid=123,
        cmdline=["python"],
        artifact_path=tmp_path / "worker.json",
        stale_reason="semantic_progress_stale_without_active_tool",
    )

    assert health.is_alive is True


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


def test_detect_active_tool_uses_worker_process_group(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    artifacts_dir = worker_process._worker_artifacts_dir(tmp_path, run_id)
    out_log = artifacts_dir / "worker.out.log"
    out_log.write_text("pytest output\n", encoding="utf-8")
    monkeypatch.setattr(worker_process, "_process_group_for_pid", lambda _pid: 4242)
    monkeypatch.setattr(
        worker_process,
        "_read_process_group_rows",
        lambda _pgid: [
            {
                "pid": 4242,
                "ppid": 1,
                "pgid": 4242,
                "elapsed_seconds": 120,
                "command": "python -m codex_autorunner flow worker",
            },
            {
                "pid": 4300,
                "ppid": 4242,
                "pgid": 4242,
                "elapsed_seconds": 65,
                "command": ".venv/bin/python -m pytest -q",
            },
        ],
    )

    active_tool = worker_process.detect_active_tool(tmp_path, run_id, 4242)

    assert active_tool is not None
    assert active_tool.pid == 4300
    assert active_tool.command == ".venv/bin/python -m pytest -q"
    assert active_tool.elapsed_seconds == 65
    assert active_tool.last_activity_at is not None


def test_detect_active_tool_respects_artifacts_root(
    monkeypatch, tmp_path: Path
) -> None:
    """Log-based activity must use the same artifacts root as worker health checks."""
    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    custom_root = tmp_path / "custom-flow-artifacts"
    artifacts_dir = worker_process._worker_artifacts_dir(
        tmp_path, run_id, artifacts_root=custom_root
    )
    (artifacts_dir / "worker.out.log").write_text(
        "custom root logs\n", encoding="utf-8"
    )

    monkeypatch.setattr(worker_process, "_process_group_for_pid", lambda _pid: 4242)
    monkeypatch.setattr(
        worker_process,
        "_read_process_group_rows",
        lambda _pgid: [
            {
                "pid": 4242,
                "ppid": 1,
                "pgid": 4242,
                "elapsed_seconds": 10,
                "command": "python -m codex_autorunner flow worker",
            },
            {
                "pid": 4300,
                "ppid": 4242,
                "pgid": 4242,
                "elapsed_seconds": 5,
                "command": "tool",
            },
        ],
    )

    active_tool = worker_process.detect_active_tool(
        tmp_path, run_id, 4242, artifacts_root=custom_root
    )

    assert active_tool is not None
    assert active_tool.output_updated_at is not None
    default_dir = (
        tmp_path
        / ".codex-autorunner"
        / "flows"
        / worker_process._normalized_run_id(run_id)
    )
    assert not default_dir.exists()


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


def test_terminate_flow_worker_pid_records_exit_and_signals(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    artifacts_dir = worker_process._worker_artifacts_dir(tmp_path, run_id)
    worker_process._write_worker_metadata(
        worker_process._worker_metadata_path(artifacts_dir),
        pid=4242,
        cmd=["python", "-m", "codex_autorunner", "flow", "worker"],
        repo_root=tmp_path,
    )
    signals: list[str] = []
    running = [False]
    monkeypatch.setattr(
        worker_process,
        "_send_signal",
        lambda _pid, sig: signals.append(sig.name),
    )
    monkeypatch.setattr(
        worker_process,
        "_pid_is_running",
        lambda _pid: running.pop(0) if running else False,
    )

    stopped = worker_process.terminate_flow_worker_pid(
        tmp_path,
        run_id,
        pid=4242,
        reason="semantic_progress_stale_without_active_tool",
        terminate_grace_seconds=0.0,
    )

    assert stopped is True
    assert signals == ["SIGTERM"]
    exit_data = json.loads((artifacts_dir / "worker.exit.json").read_text())
    assert exit_data["exit_origin"] == "stale_alive_recovery"
    assert exit_data["exit_kind"] == "reaped_stale_alive"
    assert exit_data["reap_reason"] == "semantic_progress_stale_without_active_tool"


def test_cleanup_spawned_flow_workers_terminates_registered_process_group(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    signals: list[tuple[int, int]] = []

    class _FakeProc:
        pid = 777
        returncode = None

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def wait(self, timeout=None):  # type: ignore[no-untyped-def]
            self.returncode = -signal.SIGTERM
            return self.returncode

    def _fake_popen(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return _FakeProc()

    monkeypatch.setattr(worker_process.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker_process.os, "name", "posix", raising=False)
    monkeypatch.setattr(worker_process.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        worker_process.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    proc, out, err = worker_process.spawn_flow_worker(tmp_path, run_id)
    out.close()
    err.close()

    worker_process.cleanup_spawned_flow_workers(timeout_seconds=0.01)

    assert proc.poll() == -signal.SIGTERM
    assert signals == [(777, signal.SIGTERM)]
