import asyncio
import collections
import fcntl
import logging
import os
import select
import struct
import termios
import time
from typing import Dict, Optional

from ptyprocess import PtyProcess

logger = logging.getLogger("codex_autorunner.web.pty_session")

REPLAY_END = object()

ALT_SCREEN_ENTER_SEQS = (
    b"\x1b[?1049h",
    b"\x1b[?47h",
    b"\x1b[?1047h",
)
ALT_SCREEN_EXIT_SEQS = (
    b"\x1b[?1049l",
    b"\x1b[?47l",
    b"\x1b[?1047l",
)
ALT_SCREEN_SEQS = tuple((seq, True) for seq in ALT_SCREEN_ENTER_SEQS) + tuple(
    (seq, False) for seq in ALT_SCREEN_EXIT_SEQS
)
ALT_SCREEN_MAX_LEN = max(len(seq) for seq, _state in ALT_SCREEN_SEQS)
PTY_WRITE_CHUNK_BYTES = 16 * 1024
# Cap per-flush work to keep the event loop responsive.
PTY_WRITE_FLUSH_MAX_BYTES = 256 * 1024
# Hard cap to prevent unbounded buffering when the PTY can't accept input.
PTY_PENDING_MAX_BYTES = 1024 * 1024


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
        self._set_nonblocking()
        self.closed = False
        self.last_active = time.time()

    def _set_nonblocking(self) -> None:
        """Ensure PTY IO doesn't block event loop."""
        try:
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            if not (flags & os.O_NONBLOCK):
                fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except (OSError, IOError) as exc:
            logger.debug("Failed to set PTY to non-blocking mode: %s", exc)

    def resize(self, cols: int, rows: int) -> None:
        if self.closed:
            return
        buf = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, buf)
        self.last_active = time.time()

    def write(self, data: bytes) -> int:
        """Best-effort non-blocking write; returns bytes written.

        For user input, prefer ActiveSession.write_input so the loop never blocks.
        """
        if self.closed or not data:
            return 0
        try:
            written = os.write(self.fd, data)
        except (BlockingIOError, InterruptedError):
            return 0
        except OSError:
            self.terminate()
            return 0
        if written:
            self.last_active = time.time()
        return written

    def read(self, max_bytes: int = 4096) -> bytes:
        if self.closed:
            return b""
        readable, _, _ = select.select([self.fd], [], [], 0)
        if not readable:
            return b""
        try:
            chunk = os.read(self.fd, max_bytes)
        except BlockingIOError:
            return b""
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
        except (OSError, IOError) as exc:
            logger.debug("Failed to terminate PTY process: %s", exc)
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
        self.subscribers: set[asyncio.Queue[object]] = set()
        self.lock = asyncio.Lock()
        self.loop = loop
        # Buffered input keeps the event loop from blocking on PTY writes.
        self._pending_input = bytearray()
        self._writer_active = False
        # Track recently-seen input IDs (from web UI) to make "send" retries idempotent.
        self._seen_input_ids_max = 256
        self._seen_input_ids: collections.deque[str] = collections.deque()
        self._seen_input_ids_set: set[str] = set()
        now = time.time()
        self.last_output_at = now
        self.last_input_at = now
        self._output_since_idle = False
        self._idle_notified_at: Optional[float] = None
        self._alt_screen_active = False
        self._alt_screen_tail = b""
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

    def write_input(self, data: bytes) -> None:
        """Queue terminal input and flush without blocking the event loop."""
        if self.pty.closed or not data:
            return
        if len(self._pending_input) >= PTY_PENDING_MAX_BYTES:
            return
        remaining = PTY_PENDING_MAX_BYTES - len(self._pending_input)
        if len(data) > remaining:
            data = data[-remaining:]
        self._pending_input.extend(data)
        self._flush_pending_input()

    def _enable_writer(self) -> None:
        if self._writer_active:
            return
        try:
            self.loop.add_writer(self.pty.fd, self._flush_pending_input)
            self._writer_active = True
        except (OSError, IOError) as exc:
            logger.debug("Failed to enable PTY writer: %s", exc)
            self._writer_active = False

    def _disable_writer(self) -> None:
        if not self._writer_active:
            return
        try:
            self.loop.remove_writer(self.pty.fd)
        except (OSError, IOError) as exc:
            logger.debug("Failed to disable PTY writer: %s", exc)
        self._writer_active = False

    def _flush_pending_input(self) -> None:
        """Drain queued input without blocking the event loop."""
        if self.pty.closed:
            self._pending_input.clear()
            self._disable_writer()
            return
        if not self._pending_input:
            self._disable_writer()
            return
        bytes_flushed = 0
        while self._pending_input and bytes_flushed < PTY_WRITE_FLUSH_MAX_BYTES:
            limit = min(len(self._pending_input), PTY_WRITE_CHUNK_BYTES)
            chunk = bytes(self._pending_input[:limit])
            try:
                written = os.write(self.pty.fd, chunk)
            except BlockingIOError:
                self._enable_writer()
                return
            except InterruptedError:
                continue
            except OSError:
                self.close()
                return
            if written <= 0:
                break
            del self._pending_input[:written]
            bytes_flushed += written
            self.pty.last_active = time.time()
        if self._pending_input:
            self._enable_writer()
        else:
            self._disable_writer()

    def _read_callback(self):
        try:
            if self.pty.closed:
                return
            try:
                data = os.read(self.pty.fd, 4096)
            except BlockingIOError:
                return
            if data:
                self._update_alt_screen_state(data)
                now = time.time()
                self.pty.last_active = now
                self.last_output_at = now
                self._output_since_idle = True
                self._idle_notified_at = None
                self.buffer.append(data)
                self._buffer_bytes += len(data)
                while self._buffer_bytes > self._buffer_max_bytes and self.buffer:
                    dropped = self.buffer.popleft()
                    self._buffer_bytes -= len(dropped)
                for queue in list(self.subscribers):
                    try:
                        queue.put_nowait(data)
                    except asyncio.QueueFull:
                        logger.debug(
                            "Subscriber queue full, dropping data for session %s",
                            self.id,
                        )
            else:
                self.close()
        except OSError:
            self.close()

    def add_subscriber(
        self, *, include_replay_end: bool = True
    ) -> asyncio.Queue[object]:
        q: asyncio.Queue[object] = asyncio.Queue()
        for chunk in self.buffer:
            q.put_nowait(chunk)
        if include_replay_end:
            q.put_nowait(REPLAY_END)
        self.subscribers.add(q)
        return q

    def refresh_alt_screen_state(self) -> None:
        state = self._alt_screen_active
        tail = b""
        for chunk in self.buffer:
            state, tail = self._scan_alt_screen_chunk(chunk, state, tail)
        self._alt_screen_active = state
        self._alt_screen_tail = tail

    @property
    def alt_screen_active(self) -> bool:
        return self._alt_screen_active

    def get_buffer_stats(self) -> tuple[int, int]:
        return self._buffer_bytes, len(self.buffer)

    def _scan_alt_screen_chunk(
        self, data: bytes, state: bool, tail: bytes
    ) -> tuple[bool, bytes]:
        if not data:
            return state, tail
        haystack = tail + data
        last_pos = -1
        last_state: Optional[bool] = None
        for seq, next_state in ALT_SCREEN_SEQS:
            pos = haystack.rfind(seq)
            if pos > last_pos:
                last_pos = pos
                last_state = next_state
        if last_state is not None:
            state = last_state
        if ALT_SCREEN_MAX_LEN > 1:
            tail = haystack[-(ALT_SCREEN_MAX_LEN - 1) :]
        else:
            tail = b""
        return state, tail

    def _update_alt_screen_state(self, data: bytes) -> None:
        self._alt_screen_active, self._alt_screen_tail = self._scan_alt_screen_chunk(
            data, self._alt_screen_active, self._alt_screen_tail
        )

    def remove_subscriber(self, q: asyncio.Queue[object]):
        self.subscribers.discard(q)

    def close(self):
        try:
            self._disable_writer()
        except (OSError, IOError) as exc:
            logger.debug("Failed to disable writer during close: %s", exc)
        self._pending_input.clear()
        if not self.pty.closed:
            try:
                self.loop.remove_reader(self.pty.fd)
            except (OSError, IOError) as exc:
                logger.debug("Failed to remove reader during close: %s", exc)
            try:
                self.pty.terminate()
            except (OSError, IOError) as exc:
                logger.debug("Failed to terminate PTY during close: %s", exc)
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self.subscribers.clear()

    def mark_input_activity(self) -> None:
        now = time.time()
        self.last_input_at = now
        self._output_since_idle = False
        self._idle_notified_at = None

    def should_notify_idle(self, idle_seconds: float) -> bool:
        if idle_seconds <= 0:
            return False
        if not self._output_since_idle:
            return False
        if self._idle_notified_at is not None:
            return False
        if time.time() - self.last_output_at < idle_seconds:
            return False
        self._idle_notified_at = time.time()
        self._output_since_idle = False
        return True

    async def wait_closed(self, timeout: float = 5.0):
        """Wait for the underlying PTY process to terminate."""
        start = time.time()
        while time.time() - start < timeout:
            if not self.pty.isalive():
                return
            await asyncio.sleep(0.1)
