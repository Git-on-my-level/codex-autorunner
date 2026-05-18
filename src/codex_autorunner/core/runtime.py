"""Runtime context module.

Provides RuntimeContext as a minimal runtime helper for ticket flows.
Doctor check implementations live in :mod:`codex_autorunner.core.diagnostics`.
"""

import logging
from pathlib import Path
from typing import Any, Optional

from .config import RepoConfig, load_repo_config
from .diagnostics.hermes import hermes_doctor_checks
from .diagnostics.hub import hub_destination_doctor_checks, hub_worktree_doctor_checks
from .diagnostics.opencode import summarize_opencode_lifecycle
from .diagnostics.pma import pma_doctor_checks
from .diagnostics.repository import doctor
from .diagnostics.types import DoctorCheck, DoctorReport
from .locks import DEFAULT_RUNNER_CMD_HINTS, assess_lock
from .notifications import NotificationManager
from .runner_state import LockError, RunnerStateManager
from .state_roots import resolve_repo_state_root
from .utils import RepoNotFoundError, find_repo_root

_logger = logging.getLogger(__name__)


def clear_stale_lock(repo_root: Path) -> bool:
    """Clear stale runner lock if present.

    Returns:
        True if lock was cleared, False if lock was active or absent.
    """
    lock_path = repo_root / ".codex-autorunner" / "lock"
    if not lock_path.exists():
        return False

    assessment = assess_lock(
        lock_path, expected_cmd_substrings=DEFAULT_RUNNER_CMD_HINTS
    )
    if not assessment.freeable:
        return False

    lock_path.unlink(missing_ok=True)
    return True


class RuntimeContext:
    """Minimal runtime context for ticket flows.

    Provides config, state paths, logging, and lock management utilities.
    Does NOT include orchestration logic (use ticket_flow/TicketRunner instead).
    """

    def __init__(
        self,
        repo_root: Path,
        config: Optional[RepoConfig] = None,
        backend_orchestrator: Optional[Any] = None,
    ):
        self._config = config or load_repo_config(repo_root)
        self.repo_root = self._config.root
        self._backend_orchestrator = backend_orchestrator

        # Paths
        self.state_root = resolve_repo_state_root(repo_root)
        self.state_path = self.state_root / "state.sqlite3"
        self.log_path = self.state_root / "codex-autorunner.log"
        self.lock_path = self.state_root / "lock"

        # Managers
        self._state_manager = RunnerStateManager(
            repo_root=self.repo_root,
            lock_path=self.lock_path,
            state_path=self.state_path,
        )

        # Notification manager (for run-level events)
        self._notifier: Optional[NotificationManager] = None

    @classmethod
    def from_cwd(
        cls, repo: Optional[Path] = None, *, backend_orchestrator: Optional[Any] = None
    ) -> "RuntimeContext":
        """Create RuntimeContext from current working directory or given repo."""
        if repo is None:
            repo = find_repo_root()
        if not repo or not repo.exists():
            raise RepoNotFoundError(f"Repository not found: {repo}")
        return cls(repo_root=repo, backend_orchestrator=backend_orchestrator)

    @property
    def config(self) -> RepoConfig:
        """Get repository config."""
        return self._config

    @property
    def notifier(self) -> NotificationManager:
        """Get notification manager."""
        if self._notifier is None:
            self._notifier = NotificationManager(self._config)
        return self._notifier

    # Delegate to state manager
    def acquire_lock(self, force: bool = False) -> None:
        """Acquire runner lock."""
        self._state_manager.acquire_lock(force=force)

    def release_lock(self) -> None:
        """Release runner lock."""
        self._state_manager.release_lock()

    def repo_busy_reason(self) -> Optional[str]:
        """Return a reason why the repo is busy, or None if not busy."""
        return self._state_manager.repo_busy_reason()

    def request_stop(self) -> None:
        """Request a stop by writing to the stop path."""
        self._state_manager.request_stop()

    def clear_stop_request(self) -> None:
        """Clear a stop request."""
        self._state_manager.clear_stop_request()

    def stop_requested(self) -> bool:
        """Check if a stop has been requested."""
        return self._state_manager.stop_requested()

    def kill_running_process(self) -> Optional[int]:
        """Force-kill process holding the lock, if any. Returns pid if killed."""
        return self._state_manager.kill_running_process()

    def runner_pid(self) -> Optional[int]:
        """Get PID of the running runner."""
        return self._state_manager.runner_pid()

    # Logging utilities
    def tail_log(self, tail: int = 50) -> str:
        """Tail the log file."""
        if not self.log_path.exists():
            return ""
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                return "".join(lines[-tail:])
        except OSError as e:
            _logger.warning("Failed to tail log %s: %s", self.log_path, e)
            return ""


__all__ = [
    "DoctorCheck",
    "DoctorReport",
    "LockError",
    "RuntimeContext",
    "clear_stale_lock",
    "doctor",
    "hermes_doctor_checks",
    "hub_destination_doctor_checks",
    "hub_worktree_doctor_checks",
    "pma_doctor_checks",
    "summarize_opencode_lifecycle",
]
