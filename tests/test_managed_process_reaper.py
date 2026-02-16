from __future__ import annotations

import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_autorunner.core.managed_processes import registry
from codex_autorunner.core.managed_processes.reaper import reap_managed_processes


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
        lambda _pid: False,
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

    assert summary.killed == 1
    assert summary.removed == 1
    assert summary.skipped == 0
    assert killpg_calls == [(2999, signal.SIGTERM), (2999, signal.SIGKILL)]
    assert kill_calls == [(2111, signal.SIGTERM), (2111, signal.SIGKILL)]
    assert registry.read_process_record(tmp_path, "opencode", "ws-dead") is None


def test_reaper_dry_run_does_not_kill_or_delete(monkeypatch, tmp_path: Path) -> None:
    rec = _record(workspace_id="ws-dry", pid=3111, pgid=3111, owner_pid=7777)
    registry.write_process_record(tmp_path, rec)

    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.managed_processes.reaper._pid_is_running",
        lambda _pid: False,
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
        lambda _pid: True,
    )

    summary = reap_managed_processes(tmp_path, max_record_age_seconds=24 * 60 * 60)

    assert summary.killed == 0
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
        lambda _pid: True,
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

    assert summary.killed == 1
    assert summary.removed == 1
    assert summary.skipped == 0
    assert killpg_calls == [(5111, signal.SIGTERM), (5111, signal.SIGKILL)]
    assert kill_calls == [(5111, signal.SIGTERM), (5111, signal.SIGKILL)]
