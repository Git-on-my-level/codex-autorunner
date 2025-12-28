import asyncio
import collections
import os
import fcntl
import select
import struct
import termios
import time
from typing import Dict, Optional

from ptyprocess import PtyProcess

REPLAY_END = object()


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


class ActiveSession:
    def __init__(
        self, session_id: str, pty: PTYSession, loop: asyncio.AbstractEventLoop
    ):
        self.id = session_id
        self.pty = pty
        # Keep a bounded scrollback buffer for reconnects.
        # This is sized in bytes (not chunks) so behavior is predictable.
        self._buffer_max_bytes = 512 * 1024  # 512KB
        self._buffer_bytes = 0
        self.buffer: collections.deque[bytes] = collections.deque()
        self.subscribers: set[asyncio.Queue] = set()
        self.lock = asyncio.Lock()
        self.loop = loop
        # Track recently-seen input IDs (from web UI) to make "send" retries idempotent.
        self._seen_input_ids_max = 256
        self._seen_input_ids: collections.deque[str] = collections.deque()
        self._seen_input_ids_set: set[str] = set()
        self._setup_reader()

    def mark_input_id_seen(self, input_id: str) -> bool:
        """Return True if this is the first time we've seen input_id."""
        if input_id in self._seen_input_ids_set:
            return False
        self._seen_input_ids_set.add(input_id)
        self._seen_input_ids.append(input_id)
        while len(self._seen_input_ids) > self._seen_input_ids_max:
            dropped = self._seen_input_ids.popleft()
            self._seen_input_ids_set.discard(dropped)
        return True

    def _setup_reader(self):
        self.loop.add_reader(self.pty.fd, self._read_callback)

    def _read_callback(self):
        try:
            if self.pty.closed:
                return
            data = os.read(self.pty.fd, 4096)
            if data:
                self.pty.last_active = time.time()
                self.buffer.append(data)
                self._buffer_bytes += len(data)
                while self._buffer_bytes > self._buffer_max_bytes and self.buffer:
                    dropped = self.buffer.popleft()
                    self._buffer_bytes -= len(dropped)
                for queue in list(self.subscribers):
                    try:
                        queue.put_nowait(data)
                    except asyncio.QueueFull:
                        pass
            else:
                self.close()
        except OSError:
            self.close()

    def add_subscriber(self) -> asyncio.Queue:
        q = asyncio.Queue()
        for chunk in self.buffer:
            q.put_nowait(chunk)
        q.put_nowait(REPLAY_END)
        self.subscribers.add(q)
        return q

    def remove_subscriber(self, q: asyncio.Queue):
        self.subscribers.discard(q)

    def close(self):
        if not self.pty.closed:
            try:
                self.loop.remove_reader(self.pty.fd)
            except Exception:
                pass
            self.pty.terminate()
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self.subscribers.clear()

    async def wait_closed(self, timeout: float = 5.0):
        """Wait for the underlying PTY process to terminate."""
        start = time.time()
        while time.time() - start < timeout:
            if not self.pty.isalive():
                return
            await asyncio.sleep(0.1)
