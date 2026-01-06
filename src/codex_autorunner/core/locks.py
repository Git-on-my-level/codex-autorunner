import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .utils import atomic_write


@dataclass
class LockInfo:
    pid: Optional[int]
    started_at: Optional[str]
    host: Optional[str]


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_lock_info(lock_path: Path) -> LockInfo:
    if not lock_path.exists():
        return LockInfo(pid=None, started_at=None, host=None)
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return LockInfo(pid=None, started_at=None, host=None)
    if not text:
        return LockInfo(pid=None, started_at=None, host=None)
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            pid = payload.get("pid")
            return LockInfo(
                pid=int(pid) if isinstance(pid, int) or str(pid).isdigit() else None,
                started_at=payload.get("started_at"),
                host=payload.get("host"),
            )
        except Exception:
            return LockInfo(pid=None, started_at=None, host=None)
    pid = int(text) if text.isdigit() else None
    return LockInfo(pid=pid, started_at=None, host=None)


def write_lock_info(lock_path: Path, pid: int, *, started_at: str) -> None:
    payload = {
        "pid": pid,
        "started_at": started_at,
        "host": socket.gethostname(),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(lock_path, json.dumps(payload) + "\n")
