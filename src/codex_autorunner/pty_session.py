import os
import fcntl
import select
import struct
import termios
import time
from typing import Dict, Optional

from ptyprocess import PtyProcess


def default_env(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    base = os.environ.copy()
    if env:
        base.update(env)
    base.setdefault("TERM", "xterm-256color")
    base.setdefault("COLORTERM", "truecolor")
    return base


class PTYSession:
    def __init__(self, cmd: list[str], cwd: str, env: Optional[Dict[str, str]] = None):
        # echo=False to avoid double-printing user keystrokes
        self.proc = PtyProcess.spawn(cmd, cwd=cwd, env=default_env(env), echo=False)
        self.fd = self.proc.fd
        self.closed = False
        self.last_active = time.time()

    def resize(self, cols: int, rows: int) -> None:
        if self.closed:
            return
        buf = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, buf)
        self.last_active = time.time()

    def write(self, data: bytes) -> None:
        if self.closed:
            return
        os.write(self.fd, data)
        self.last_active = time.time()

    def read(self, max_bytes: int = 4096) -> bytes:
        if self.closed:
            return b""
        readable, _, _ = select.select([self.fd], [], [], 0)
        if not readable:
            return b""
        try:
            chunk = os.read(self.fd, max_bytes)
        except OSError:
            self.terminate()
            return b""
        if chunk:
            self.last_active = time.time()
        return chunk

    def isalive(self) -> bool:
        return not self.closed and self.proc.isalive()

    def exit_code(self) -> Optional[int]:
        return self.proc.exitstatus if not self.proc.isalive() else None

    def is_stale(self, max_idle_seconds: int) -> bool:
        return (time.time() - self.last_active) > max_idle_seconds

    def terminate(self) -> None:
        if self.closed:
            return
        try:
            self.proc.terminate(force=True)
        except Exception:
            pass
        self.closed = True
