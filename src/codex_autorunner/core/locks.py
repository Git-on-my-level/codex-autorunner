import errno
import json
import os
import socket
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterable, Iterator, Optional

from .utils import atomic_write


@dataclass
class LockInfo:
    pid: Optional[int]
    started_at: Optional[str]
    host: Optional[str]


@dataclass
class LockAssessment:
    freeable: bool
    reason: Optional[str]
    pid: Optional[int]
    host: Optional[str]


DEFAULT_RUNNER_CMD_HINTS = ("codex_autorunner.cli", "codex-autorunner", "car ")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def process_is_zombie(pid: int) -> bool:
    if os.name == "nt":
        return False
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    return "Z" in result.stdout.strip()


def process_command(pid: int) -> Optional[str]:
    if os.name == "nt":
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    command = result.stdout.strip()
    return command or None


def process_is_active(pid: int) -> bool:
    return process_alive(pid) and not process_is_zombie(pid)


def assess_lock(
    lock_path: Path,
    *,
    expected_cmd_substrings: Optional[Iterable[str]] = None,
    require_host_match: bool = True,
) -> LockAssessment:
    if not lock_path.exists():
        return LockAssessment(freeable=False, reason=None, pid=None, host=None)
    info = read_lock_info(lock_path)
    pid = info.pid
    if not pid:
        return LockAssessment(
            freeable=True,
            reason="Lock has no pid; safe to clear.",
            pid=None,
            host=info.host,
        )
    if not process_alive(pid):
        return LockAssessment(
            freeable=True,
            reason="Lock pid is not running; safe to clear.",
            pid=pid,
            host=info.host,
        )
    if process_is_zombie(pid):
        return LockAssessment(
            freeable=True,
            reason="Lock pid is a zombie process; safe to clear.",
            pid=pid,
            host=info.host,
        )
    if require_host_match and info.host and info.host != socket.gethostname():
        return LockAssessment(
            freeable=False,
            reason="Lock belongs to another host.",
            pid=pid,
            host=info.host,
        )
    if expected_cmd_substrings:
        command = process_command(pid)
        if command is None:
            return LockAssessment(
                freeable=False,
                reason="Unable to inspect lock pid command.",
                pid=pid,
                host=info.host,
            )
        if not any(fragment in command for fragment in expected_cmd_substrings):
            return LockAssessment(
                freeable=True,
                reason="Lock pid command does not match autorunner; safe to clear.",
                pid=pid,
                host=info.host,
            )
    return LockAssessment(
        freeable=False,
        reason=None,
        pid=pid,
        host=info.host,
    )


try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX default
    msvcrt = None  # type: ignore[assignment]


class FileLockError(Exception):
    """Raised when a file lock fails unexpectedly."""


class FileLockBusy(FileLockError):
    """Raised when a file lock is already held by another process."""


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Optional[IO[str]] = None

    def acquire(self, *, blocking: bool = True) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(lock_file.fileno(), flags)
            elif msvcrt is not None:
                lock_file.seek(0)
                if not blocking and not hasattr(msvcrt, "LK_NBLCK"):
                    raise FileLockBusy("File lock already held")
                mode = msvcrt.LK_LOCK
                if not blocking:
                    mode = msvcrt.LK_NBLCK
                msvcrt.locking(lock_file.fileno(), mode, 1)
            self._file = lock_file
        except OSError as exc:
            lock_file.close()
            if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                raise FileLockBusy("File lock already held") from exc
            if not blocking and msvcrt is not None:
                raise FileLockBusy("File lock already held") from exc
            raise FileLockError(f"Failed to acquire lock: {exc}") from exc

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock_file.close()
            self._file = None

    @property
    def file(self) -> IO[str]:
        if self._file is None:
            raise FileLockError("Lock is not acquired")
        return self._file

    def write_text(self, content: str) -> None:
        lock_file = self.file
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(content)
        lock_file.flush()
        os.fsync(lock_file.fileno())

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    lock = FileLock(path)
    lock.acquire(blocking=blocking)
    try:
        yield
    finally:
        lock.release()


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


def write_lock_info(
    lock_path: Path,
    pid: int,
    *,
    started_at: str,
    lock_file: Optional[IO[str]] = None,
) -> None:
    payload = {
        "pid": pid,
        "started_at": started_at,
        "host": socket.gethostname(),
    }
    content = json.dumps(payload) + "\n"
    if lock_file is not None:
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(content)
        lock_file.flush()
        os.fsync(lock_file.fileno())
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(lock_path, content)
