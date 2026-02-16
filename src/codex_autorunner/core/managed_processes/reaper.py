from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

from ..process_termination import terminate_record
from .registry import ProcessRecord, delete_process_record, list_process_records

REAPER_GRACE_SECONDS: Final = 0.2
REAPER_KILL_SECONDS: Final = 0.2

DEFAULT_MAX_RECORD_AGE_SECONDS = 6 * 60 * 60


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if _pid_is_zombie(pid):
        return False
    return True


def _pid_is_zombie(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    state = result.stdout.strip()
    return state != "" and state.split()[0].startswith("Z")


def _parse_iso_timestamp(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_older_than(record: ProcessRecord, max_age_seconds: int) -> bool:
    if max_age_seconds <= 0:
        return True
    try:
        started = _parse_iso_timestamp(record.started_at)
    except Exception:
        # Malformed timestamps are treated as stale.
        return True
    threshold = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
    return started < threshold


@dataclass
class ReapSummary:
    killed: int = 0
    signaled: int = 0
    removed: int = 0
    skipped: int = 0


def _kill_record_processes(record: ProcessRecord) -> bool:
    return terminate_record(
        record.pid,
        record.pgid,
        grace_seconds=REAPER_GRACE_SECONDS,
        kill_seconds=REAPER_KILL_SECONDS,
    )


def _pgid_is_running(pgid: int) -> bool:
    if os.name == "nt" or not hasattr(os, "killpg"):
        return False

    # `killpg(..., 0)` can return non-zero for permission/other failures.
    # Fall back to `ps` when available to avoid false positives for zombie
    # process groups and provide a more deterministic liveness decision.
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False

    try:
        result = subprocess.run(
            ["ps", "-g", str(pgid), "-o", "pid=,stat="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return True
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        if fields[1].startswith("Z"):
            continue
        return True
    return False


def _record_is_running(record: ProcessRecord) -> bool:
    if record.pid is not None and _pid_is_running(record.pid):
        return True
    if record.pgid is None:
        return False
    return _pgid_is_running(record.pgid)


def reap_managed_processes(
    repo_root: Path,
    *,
    dry_run: bool = False,
    max_record_age_seconds: int = DEFAULT_MAX_RECORD_AGE_SECONDS,
    force: bool = False,
) -> ReapSummary:
    summary = ReapSummary()
    for record in list_process_records(repo_root):
        owner_running = _pid_is_running(record.owner_pid)
        record_old = _is_older_than(record, max_record_age_seconds)
        should_reap = force or (not owner_running) or record_old

        if not should_reap:
            summary.skipped += 1
            continue

        has_target = record.pgid is not None or record.pid is not None
        if dry_run:
            if has_target:
                summary.killed += 1
            continue

        was_running = _record_is_running(record)
        _kill_record_processes(record)

        still_running = _record_is_running(record) if has_target else False
        if has_target and still_running:
            summary.signaled += 1
            summary.skipped += 1
            continue

        if has_target and was_running:
            summary.killed += 1

        if delete_process_record(repo_root, record.kind, record.record_key()):
            summary.removed += 1

    return summary
