from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.managed_processes import ProcessRecord, list_process_records


class ProcessCategory(Enum):
    CAR_SERVICE = "car_service"
    OPENCODE = "opencode"
    APP_SERVER = "app_server"
    OTHER = "other"


class ProcessOwnership(Enum):
    MANAGED = "managed"
    STALE_RECORD = "stale_record"
    UNTRACKED_LIVE_PROCESS = "untracked_live_process"
    OWNER_MISSING = "owner_missing"


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    pgid: int
    command: str
    category: ProcessCategory
    rss_kb: Optional[int] = None
    elapsed: Optional[str] = None
    ownership: Optional[ProcessOwnership] = None
    record: Optional[ProcessRecord] = None


@dataclass
class ProcessSnapshot:
    car_service_processes: list[ProcessInfo] = field(default_factory=list)
    opencode_processes: list[ProcessInfo] = field(default_factory=list)
    app_server_processes: list[ProcessInfo] = field(default_factory=list)
    other_processes: list[ProcessInfo] = field(default_factory=list)
    collected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        def _process_info_to_dict(p: ProcessInfo) -> dict[str, Any]:
            result: dict[str, Any] = {
                "pid": p.pid,
                "ppid": p.ppid,
                "pgid": p.pgid,
                "command": p.command,
                "category": p.category.value,
            }
            if p.rss_kb is not None:
                result["rss_kb"] = p.rss_kb
            if p.elapsed is not None:
                result["elapsed"] = p.elapsed
            if p.ownership is not None:
                result["ownership"] = p.ownership.value
            if p.record is not None:
                result["record"] = {
                    "kind": p.record.kind,
                    "workspace_id": p.record.workspace_id,
                    "base_url": p.record.base_url,
                    "owner_pid": p.record.owner_pid,
                    "started_at": p.record.started_at,
                }
            return result

        return {
            "collected_at": self.collected_at,
            "counts": {
                "car_services": self.car_service_count,
                "managed_runtimes": self.managed_runtime_count,
                "opencode": self.opencode_count,
                "codex_app_server": self.codex_app_server_count,
                "app_server": self.codex_app_server_count,
                "total": self.total_count,
            },
            "car_services": [
                _process_info_to_dict(p) for p in self.car_service_processes
            ],
            "opencode": [_process_info_to_dict(p) for p in self.opencode_processes],
            "codex_app_server": [
                _process_info_to_dict(p) for p in self.app_server_processes
            ],
            "app_server": [_process_info_to_dict(p) for p in self.app_server_processes],
            "other": [_process_info_to_dict(p) for p in self.other_processes],
        }

    @property
    def car_service_count(self) -> int:
        return len(self.car_service_processes)

    @property
    def opencode_count(self) -> int:
        return len(self.opencode_processes)

    @property
    def app_server_count(self) -> int:
        return len(self.app_server_processes)

    @property
    def codex_app_server_count(self) -> int:
        return len(self.app_server_processes)

    @property
    def managed_runtime_count(self) -> int:
        return self.opencode_count + self.codex_app_server_count

    @property
    def total_count(self) -> int:
        return self.car_service_count + self.managed_runtime_count


# Match underscore-named binary only as the argv0 segment (not a parent dir like
# .../codex_autorunner/.venv/bin/codex), which would misclassify app-server/opencode.
_CODEX_AUTORUNNER_UNDERSCORE_EXE_RE = re.compile(
    r"(?:^|[/\\])codex_autorunner(?=\s|$)",
)

CAR_SERVICE_MARKERS = (
    "codex-autorunner hub ",
    "codex-autorunner discord ",
    "codex-autorunner telegram ",
    "codex-autorunner flow ",
    "codex-autorunner serve ",
)

OPENCODE_MARKERS = ("opencode",)

APP_SERVER_MARKERS = (
    "codex app-server",
    "codexapp-server",
    "codex:app-server",
    "codex -- app-server",
)


def _classify_process(command: str) -> ProcessCategory:
    command_lc = command.lower()
    if _CODEX_AUTORUNNER_UNDERSCORE_EXE_RE.search(command_lc):
        return ProcessCategory.CAR_SERVICE
    if any(marker in command_lc for marker in CAR_SERVICE_MARKERS):
        return ProcessCategory.CAR_SERVICE
    if any(marker in command_lc for marker in APP_SERVER_MARKERS):
        return ProcessCategory.APP_SERVER
    if any(marker in command_lc for marker in OPENCODE_MARKERS):
        return ProcessCategory.OPENCODE
    return ProcessCategory.OTHER


def parse_ps_output(
    output: str,
    classifier: Callable[[str], ProcessCategory] = _classify_process,
) -> ProcessSnapshot:
    snapshot = ProcessSnapshot()
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=5)
        if len(parts) < 6:
            continue
        pid_raw = parts[0]
        ppid_raw = parts[1]
        pgid_raw = parts[2]
        rss_raw = parts[3]
        elapsed = parts[4]
        command = parts[5]
        if not (pid_raw.isdigit() and ppid_raw.isdigit() and pgid_raw.isdigit()):
            continue
        pid = int(pid_raw)
        ppid = int(ppid_raw)
        pgid = int(pgid_raw)
        rss_kb: Optional[int] = None
        if rss_raw.isdigit():
            rss_kb = int(rss_raw)
        category = classifier(command)
        info = ProcessInfo(
            pid=pid,
            ppid=ppid,
            pgid=pgid,
            command=command,
            category=category,
            rss_kb=rss_kb,
            elapsed=elapsed or None,
        )
        if category == ProcessCategory.CAR_SERVICE:
            snapshot.car_service_processes.append(info)
        elif category == ProcessCategory.OPENCODE:
            snapshot.opencode_processes.append(info)
        elif category == ProcessCategory.APP_SERVER:
            snapshot.app_server_processes.append(info)
        else:
            snapshot.other_processes.append(info)
    return snapshot


def get_ps_output() -> str:
    try:
        proc = subprocess.run(
            [
                "ps",
                "-ax",
                "-o",
                "pid=",
                "-o",
                "ppid=",
                "-o",
                "pgid=",
                "-o",
                "rss=",
                "-o",
                "etime=",
                "-o",
                "command=",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout or ""
    except OSError:
        return ""


def collect_processes(
    ps_output_getter: Optional[Callable[[], str]] = None,
) -> ProcessSnapshot:
    output = ""
    if ps_output_getter is not None:
        output = ps_output_getter()
    else:
        output = get_ps_output()
    return parse_ps_output(output)


def _find_record_for_pid(
    records: list[ProcessRecord], pid: int
) -> Optional[ProcessRecord]:
    for record in records:
        if record.pid == pid:
            return record
    return None


def _find_record_for_pgid(
    records: list[ProcessRecord], pgid: int
) -> Optional[ProcessRecord]:
    for record in records:
        if record.pgid == pgid:
            return record
    return None


def enrich_with_ownership(
    snapshot: ProcessSnapshot,
    repo_root: Optional[Path] = None,
) -> ProcessSnapshot:
    if repo_root is None:
        return snapshot

    try:
        process_records = list_process_records(repo_root)
    except Exception:  # intentional: non-critical process enrichment
        process_records = []

    record_pids = {r.pid for r in process_records if r.pid is not None}
    record_pgids = {r.pgid for r in process_records if r.pgid is not None}

    def determine_ownership(
        proc: ProcessInfo,
    ) -> tuple[Optional[ProcessOwnership], Optional[ProcessRecord]]:
        if proc.pid in record_pids:
            record = _find_record_for_pid(process_records, proc.pid)
            if record:
                return ProcessOwnership.MANAGED, record
        if proc.pgid in record_pgids:
            record = _find_record_for_pgid(process_records, proc.pgid)
            if record:
                return ProcessOwnership.MANAGED, record

        owner_pid = os.getppid() if hasattr(os, "getppid") else None
        if owner_pid is not None and proc.ppid == owner_pid:
            return ProcessOwnership.UNTRACKED_LIVE_PROCESS, None

        return ProcessOwnership.OWNER_MISSING, None

    for proc in snapshot.opencode_processes:
        ownership, record = determine_ownership(proc)
        proc.ownership = ownership
        proc.record = record

    for proc in snapshot.app_server_processes:
        ownership, record = determine_ownership(proc)
        proc.ownership = ownership
        proc.record = record

    return snapshot
