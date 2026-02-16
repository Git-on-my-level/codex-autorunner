from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional


class ProcessCategory(Enum):
    OPENCODE = "opencode"
    APP_SERVER = "app_server"
    OTHER = "other"


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    pgid: int
    command: str
    category: ProcessCategory


@dataclass
class ProcessSnapshot:
    opencode_processes: list[ProcessInfo] = field(default_factory=list)
    app_server_processes: list[ProcessInfo] = field(default_factory=list)
    other_processes: list[ProcessInfo] = field(default_factory=list)
    collected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "collected_at": self.collected_at,
            "opencode": [
                {
                    "pid": p.pid,
                    "ppid": p.ppid,
                    "pgid": p.pgid,
                    "command": p.command,
                }
                for p in self.opencode_processes
            ],
            "app_server": [
                {
                    "pid": p.pid,
                    "ppid": p.ppid,
                    "pgid": p.pgid,
                    "command": p.command,
                }
                for p in self.app_server_processes
            ],
            "other": [
                {
                    "pid": p.pid,
                    "ppid": p.ppid,
                    "pgid": p.pgid,
                    "command": p.command,
                }
                for p in self.other_processes
            ],
        }

    @property
    def opencode_count(self) -> int:
        return len(self.opencode_processes)

    @property
    def app_server_count(self) -> int:
        return len(self.app_server_processes)


OPENCODE_MARKERS = (
    "opencode",
    "codex_autorunner",
)

APP_SERVER_MARKERS = (
    "codex app-server",
    "codexapp-server",
    "codex:app-server",
    "codex -- app-server",
)


def _classify_process(command: str) -> ProcessCategory:
    command_lc = command.lower()
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
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        pid_raw = parts[0]
        ppid_raw = parts[1]
        pgid_raw = parts[2]
        command = parts[3]
        if not (pid_raw.isdigit() and ppid_raw.isdigit() and pgid_raw.isdigit()):
            continue
        pid = int(pid_raw)
        ppid = int(ppid_raw)
        pgid = int(pgid_raw)
        category = classifier(command)
        info = ProcessInfo(
            pid=pid,
            ppid=ppid,
            pgid=pgid,
            command=command,
            category=category,
        )
        if category == ProcessCategory.OPENCODE:
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
                "command=",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout or ""
    except Exception:
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
