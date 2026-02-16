from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_autorunner.core.managed_processes import registry
from codex_autorunner.core.managed_processes.reaper import reap_managed_processes


def _assert_process_gone(pid: int) -> None:
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return

        try:
            status = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                check=False,
                capture_output=True,
                text=True,
            )
            stat = status.stdout.strip().split()
            if stat and stat[0].startswith("Z"):
                return
        except (OSError, subprocess.SubprocessError):
            return
        time.sleep(0.1)
    pytest.fail(f"process {pid} still running after reaper termination")


def _started_at(hours_ago: int) -> str:
    now = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return now.isoformat().replace("+00:00", "Z")


def _record(
    *,
    kind: str = "opencode",
    workspace_id: str | None = "ws-1",
    pid: int | None = 2001,
    pgid: int | None = 2001,
    owner_pid: int = 1001,
    started_at: str = "2026-02-15T12:00:00Z",
) -> registry.ProcessRecord:
    return registry.ProcessRecord(
        kind=kind,
        workspace_id=workspace_id,
        pid=pid,
        pgid=pgid,
        base_url=None,
        command=["opencode", "serve"],
        owner_pid=owner_pid,
        started_at=started_at,
        metadata={},
    )


def test_reaper_kills_process_group_and_pid_when_owner_is_dead(
    monkeypatch, tmp_path: Path
) -> None:
    rec = _record(workspace_id="ws-dead", pid=2111, pgid=2999, owner_pid=4321)
    registry.write_process_record(tmp_path, rec)

    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pid_is_running",
        lambda pid: pid == 9001,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pgid_is_running",
        lambda _pgid: False,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.process_termination.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.process_termination.os.kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    summary = reap_managed_processes(tmp_path)

    assert summary.killed == 0
    assert summary.signaled == 0
    assert summary.removed == 1
    assert summary.skipped == 0
    assert killpg_calls == [(2999, signal.SIGTERM), (2999, signal.SIGKILL)]
    assert kill_calls == [(2111, signal.SIGTERM), (2111, signal.SIGKILL)]
    assert registry.read_process_record(tmp_path, "opencode", "ws-dead") is None


def test_reaper_kills_processes_for_pid_keyed_record(
    monkeypatch, tmp_path: Path
) -> None:
    rec = _record(
        workspace_id=None,
        pid=6111,
        pgid=6111,
        owner_pid=1111,
        started_at=_started_at(hours_ago=48),
    )
    registry.write_process_record(tmp_path, rec)

    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pid_is_running",
        lambda pid: pid == 1111,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pgid_is_running",
        lambda _pgid: False,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.process_termination.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.process_termination.os.kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    summary = reap_managed_processes(tmp_path, max_record_age_seconds=60)

    assert summary.killed == 0
    assert summary.signaled == 0
    assert summary.removed == 1
    assert summary.skipped == 0
    assert killpg_calls == [(6111, signal.SIGTERM), (6111, signal.SIGKILL)]
    assert kill_calls == [(6111, signal.SIGTERM), (6111, signal.SIGKILL)]
    assert registry.read_process_record(tmp_path, "opencode", "6111") is None


def test_reaper_dry_run_does_not_kill_or_delete(monkeypatch, tmp_path: Path) -> None:
    rec = _record(workspace_id="ws-dry", pid=3111, pgid=3111, owner_pid=7777)
    registry.write_process_record(tmp_path, rec)

    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pid_is_running",
        lambda pid: pid == 9001,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pgid_is_running",
        lambda _pgid: False,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.process_termination.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.process_termination.os.kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    summary = reap_managed_processes(tmp_path, dry_run=True)

    assert summary.killed == 1
    assert summary.signaled == 0
    assert summary.removed == 0
    assert summary.skipped == 0
    assert killpg_calls == []
    assert kill_calls == []
    assert registry.read_process_record(tmp_path, "opencode", "ws-dry") is not None


def test_reaper_skips_active_records(monkeypatch, tmp_path: Path) -> None:
    rec = _record(
        workspace_id="ws-active",
        pid=4111,
        pgid=4111,
        owner_pid=9001,
        started_at=_started_at(hours_ago=1),
    )
    registry.write_process_record(tmp_path, rec)

    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pid_is_running",
        lambda pid: pid == 9001,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pgid_is_running",
        lambda _pgid: False,
    )

    summary = reap_managed_processes(tmp_path, max_record_age_seconds=24 * 60 * 60)

    assert summary.killed == 0
    assert summary.signaled == 0
    assert summary.removed == 0
    assert summary.skipped == 1
    assert registry.read_process_record(tmp_path, "opencode", "ws-active") is not None


def test_reaper_reaps_old_records_even_if_owner_running(
    monkeypatch, tmp_path: Path
) -> None:
    rec = _record(
        workspace_id="ws-old",
        pid=5111,
        pgid=5111,
        owner_pid=1234,
        started_at=_started_at(hours_ago=48),
    )
    registry.write_process_record(tmp_path, rec)

    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pid_is_running",
        lambda pid: pid == 1234,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pgid_is_running",
        lambda _pgid: False,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.process_termination.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.process_termination.os.kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    summary = reap_managed_processes(tmp_path, max_record_age_seconds=60)

    assert summary.killed == 0
    assert summary.signaled == 0
    assert summary.removed == 1
    assert summary.skipped == 0
    assert killpg_calls == [(5111, signal.SIGTERM), (5111, signal.SIGKILL)]
    assert kill_calls == [(5111, signal.SIGTERM), (5111, signal.SIGKILL)]


@pytest.mark.skipif(os.name == "nt", reason="Requires POSIX process groups")
def test_reaper_reaps_sigterm_ignoring_process_group(tmp_path: Path) -> None:
    parent_code = textwrap.dedent(
        """
        import os
        import signal
        import subprocess
        import sys
        import time

        def _ignore_signal(_sig, _frame):
            pass

        signal.signal(signal.SIGTERM, _ignore_signal)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)",
            ]
        )
        print(f"{os.getpid()} {child.pid} {os.getpgrp()}", flush=True)
        while True:
            time.sleep(1)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        line = process.stdout.readline()
        parent_pid_text, child_pid_text, pgid_text = line.strip().split()
        parent_pid = int(parent_pid_text)
        child_pid = int(child_pid_text)
        pgid = int(pgid_text)

        registry.write_process_record(
            tmp_path,
            _record(
                workspace_id="ws-sigkill",
                pid=parent_pid,
                pgid=pgid,
                owner_pid=parent_pid,
                started_at=_started_at(hours_ago=48),
            ),
        )

        summary = reap_managed_processes(tmp_path, max_record_age_seconds=60)

        assert summary.killed == 1
        assert summary.signaled == 0
        assert summary.removed == 1
        assert summary.skipped == 0
        assert registry.read_process_record(tmp_path, "opencode", "ws-sigkill") is None
        _assert_process_gone(parent_pid)
        _assert_process_gone(child_pid)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
