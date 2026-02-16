from __future__ import annotations

import os
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
    return True


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
    removed: int = 0
    skipped: int = 0


def _kill_record_processes(record: ProcessRecord) -> bool:
    return terminate_record(
        record.pid,
        record.pgid,
        grace_seconds=REAPER_GRACE_SECONDS,
        kill_seconds=REAPER_KILL_SECONDS,
    )


def reap_managed_processes(
    repo_root: Path,
    *,
    dry_run: bool = False,
    max_record_age_seconds: int = DEFAULT_MAX_RECORD_AGE_SECONDS,
) -> ReapSummary:
    summary = ReapSummary()
    for record in list_process_records(repo_root):
        owner_running = _pid_is_running(record.owner_pid)
        record_old = _is_older_than(record, max_record_age_seconds)
        should_reap = (not owner_running) or record_old

        if not should_reap:
            summary.skipped += 1
            continue

        has_target = record.pgid is not None or record.pid is not None
        if dry_run:
            if has_target:
                summary.killed += 1
            continue

        kill_ok = _kill_record_processes(record)
        if has_target and kill_ok:
            summary.killed += 1
        if has_target and not kill_ok:
            summary.skipped += 1
            continue

        if delete_process_record(repo_root, record.kind, record.record_key()):
            summary.removed += 1

    return summary
