"""Runner state and lock management utilities.

This module provides runner state operations extracted from Engine.
"""

import os
import signal
from pathlib import Path
from typing import Optional

from .locks import (
    DEFAULT_RUNNER_CMD_HINTS,
    FileLock,
    FileLockBusy,
    assess_lock,
    process_alive,
    read_lock_info,
    write_lock_info,
)
from .state import load_state, now_iso


class LockError(Exception):
    """Raised when a runner lock cannot be acquired."""


def _timestamp() -> str:
    return now_iso()


class RunnerStateManager:
    """Manages runner state and locks for ticket flows."""

    def __init__(
        self,
        repo_root: Path,
        lock_path: Optional[Path] = None,
        state_path: Optional[Path] = None,
    ):
        self.repo_root = repo_root
        self.lock_path = lock_path or (repo_root / ".codex-autorunner" / "lock")
        self.state_path = state_path or (
            repo_root / ".codex-autorunner" / "state.sqlite3"
        )
        self.stop_path = repo_root / ".codex-autorunner" / "stop"
        self._lock_handle: Optional[FileLock] = None

    def acquire_lock(self, force: bool = False) -> None:
        """Acquire the runner lock."""
        self._lock_handle = FileLock(self.lock_path)
        try:
            self._lock_handle.acquire(blocking=False)
        except FileLockBusy as exc:
            info = read_lock_info(self.lock_path)
            pid = info.pid
            if pid and process_alive(pid):
                raise LockError(
                    f"Another autorunner is active (pid={pid}); stop it before continuing"
                ) from exc
            raise LockError(
                "Another autorunner is active; stop it before continuing"
            ) from exc

        info = read_lock_info(self.lock_path)
        pid = info.pid
        if pid and process_alive(pid) and not force:
            self._lock_handle.release()
            self._lock_handle = None
            raise LockError(
                f"Another autorunner is active (pid={pid}); use --force to override"
            )
        write_lock_info(
            self.lock_path,
            os.getpid(),
            started_at=_timestamp(),
            lock_file=self._lock_handle.file,
        )

    def release_lock(self) -> None:
        """Release the runner lock."""
        if self._lock_handle is not None:
            self._lock_handle.release()
            self._lock_handle = None
        if self.lock_path.exists():
            self.lock_path.unlink()

    def repo_busy_reason(self) -> Optional[str]:
        """Return a reason why the repo is busy, or None if not busy."""
        if self.lock_path.exists():
            assessment = assess_lock(
                self.lock_path,
                expected_cmd_substrings=DEFAULT_RUNNER_CMD_HINTS,
            )
            if assessment.freeable:
                return "Autorunner lock is stale; clear it before continuing."
            pid = assessment.pid
            if pid and process_alive(pid):
                host = f" on {assessment.host}" if assessment.host else ""
                return f"Autorunner is running (pid={pid}{host}); try again later."
            return "Autorunner lock present; clear or resume before continuing."

        state = load_state(self.state_path)
        if state.status == "running":
            return "Autorunner is currently running; try again later."
        return None

    def request_stop(self) -> None:
        """Request a stop by writing to the stop path."""
        self.stop_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text(f"{_timestamp()}\n")

    def clear_stop_request(self) -> None:
        """Clear a stop request."""
        self.stop_path.unlink(missing_ok=True)

    def stop_requested(self) -> bool:
        """Check if a stop has been requested."""
        return self.stop_path.exists()

    def kill_running_process(self) -> Optional[int]:
        """Force-kill the process holding the lock, if any. Returns pid if killed."""
        if not self.lock_path.exists():
            return None
        info = read_lock_info(self.lock_path)
        pid = info.pid
        if pid and process_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                return pid
            except OSError:
                return None
        # stale lock
        self.lock_path.unlink(missing_ok=True)
        return None

    def runner_pid(self) -> Optional[int]:
        """Get the PID of the running runner."""
        state = load_state(self.state_path)
        pid = state.runner_pid
        if pid and process_alive(pid):
            return pid
        info = read_lock_info(self.lock_path)
        if info.pid and process_alive(info.pid):
            return info.pid
        return None

    def todos_done(self) -> bool:
        """Check if all tickets are done."""
        from ..tickets.files import list_ticket_paths, ticket_is_done

        ticket_dir = self.repo_root / ".codex-autorunner" / "tickets"
        ticket_paths = list_ticket_paths(ticket_dir)
        if not ticket_paths:
            return False
        return all(ticket_is_done(path) for path in ticket_paths)

    def summary_finalized(self) -> bool:
        """Check if the summary is finalized (legacy, always returns True)."""
        # Legacy docs finalization no longer applies (no SUMMARY doc).
        return True
