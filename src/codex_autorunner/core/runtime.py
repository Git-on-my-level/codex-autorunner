"""Runtime context module.

Provides RuntimeContext as a minimal runtime helper for ticket flows.
This replaces Engine as the runtime authority while preserving utility functions.
"""

import logging
from pathlib import Path
from typing import Any, Optional

from .config import RepoConfig, load_repo_config
from .locks import DEFAULT_RUNNER_CMD_HINTS, assess_lock
from .notifications import NotificationManager
from .run_index import RunIndexStore
from .runner_state import LockError, RunnerStateManager
from .state import now_iso
from .utils import RepoNotFoundError, find_repo_root

_logger = logging.getLogger(__name__)


class DoctorCheck:
    """Health check result."""

    def __init__(
        self,
        name: str,
        passed: bool,
        message: str,
        severity: str = "error",
        check_id: Optional[str] = None,
        fix: Optional[str] = None,
    ):
        self.name = name
        self.passed = passed
        self.message = message
        self.severity = severity
        self.check_id = check_id
        self.status = "ok" if passed else "error"
        self.fix = fix

    def __repr__(self) -> str:
        status = "✓" if self.passed else "✗"
        return f"{status} {self.name}: {self.message}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
            "severity": self.severity,
            "check_id": self.check_id,
            "status": self.status,
            "fix": self.fix,
        }


class DoctorReport:
    """Report from running health checks."""

    def __init__(self, checks: list[DoctorCheck]):
        self.checks = checks

    @property
    def all_passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def has_errors(self) -> bool:
        return any(check.status == "error" for check in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": sum(1 for check in self.checks if check.status == "ok"),
            "warnings": sum(1 for check in self.checks if check.status == "warning"),
            "errors": sum(1 for check in self.checks if check.status == "error"),
            "checks": [check.to_dict() for check in self.checks],
        }

    def print_report(self) -> None:
        for check in self.checks:
            if check.severity == "error":
                print(check)
        for check in self.checks:
            if check.severity == "warning":
                print(check)
        for check in self.checks:
            if check.passed and check.severity != "info":
                print(check)


def doctor(
    repo_root: Path,
    backend_orchestrator: Optional[Any] = None,
    check_id: Optional[str] = None,
) -> DoctorReport:
    """Run health checks on the repository.

    Args:
        repo_root: Repository root path.
        backend_orchestrator: Optional backend orchestrator for agent checks.
        check_id: Optional ID for specific check.

    Returns:
        DoctorReport with check results.
    """
    checks: list[DoctorCheck] = []

    # Check if in git repo
    try:
        from .git_utils import run_git

        result = run_git(["rev-parse", "--is-inside-work-tree"], repo_root, check=False)
        if result.returncode != 0:
            checks.append(
                DoctorCheck(
                    name="Git repository",
                    passed=False,
                    message="Not a git repository",
                    check_id=check_id,
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Git repository",
                    passed=True,
                    message="OK",
                    severity="info",
                    check_id=check_id,
                )
            )
    except Exception as e:
        checks.append(
            DoctorCheck(
                name="Git repository",
                passed=False,
                message=f"Failed to check git: {e}",
                check_id=check_id,
            )
        )

    # Check config file
    config_path = repo_root / "codex-autorunner.yml"
    if not config_path.exists():
        checks.append(
            DoctorCheck(
                name="Config file",
                passed=False,
                message=f"Config file not found: {config_path}",
                check_id=check_id,
            )
        )
    else:
        try:
            load_repo_config(repo_root)
            checks.append(
                DoctorCheck(
                    name="Config file",
                    passed=True,
                    message="OK",
                    severity="info",
                    check_id=check_id,
                )
            )
        except Exception as e:
            checks.append(
                DoctorCheck(
                    name="Config file",
                    passed=False,
                    message=f"Failed to load: {e}",
                    check_id=check_id,
                )
            )

    # Check state directory
    state_root = repo_root / ".codex-autorunner"
    if not state_root.exists():
        checks.append(
            DoctorCheck(
                name="State directory",
                passed=False,
                message=f"State directory not found: {state_root}",
                severity="warning",
                check_id=check_id,
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="State directory",
                passed=True,
                message="OK",
                severity="info",
                check_id=check_id,
            )
        )

    # Check for stale locks
    lock_path = state_root / "lock"
    if lock_path.exists():
        assessment = assess_lock(
            lock_path, expected_cmd_substrings=DEFAULT_RUNNER_CMD_HINTS
        )
        if assessment.freeable:
            checks.append(
                DoctorCheck(
                    name="Runner lock",
                    passed=False,
                    message="Stale lock detected; run `car clear-stale-lock`",
                    severity="warning",
                    check_id=check_id,
                )
            )
        elif assessment.pid:
            checks.append(
                DoctorCheck(
                    name="Runner lock",
                    passed=True,
                    message=f"Active (pid={assessment.pid})",
                    severity="info",
                    check_id=check_id,
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Runner lock",
                    passed=True,
                    message="OK",
                    severity="info",
                    check_id=check_id,
                )
            )

    return DoctorReport(checks)


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
        self.state_root = repo_root / ".codex-autorunner"
        self.state_path = self.state_root / "state.sqlite3"
        self.log_path = self.state_root / "codex-autorunner.log"
        self.lock_path = self.state_root / "lock"

        # Managers
        self._state_manager = RunnerStateManager(
            repo_root=self.repo_root,
            lock_path=self.lock_path,
            state_path=self.state_path,
        )

        # Run index store
        self._run_index_store: Optional[RunIndexStore] = None

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
    def run_index_store(self) -> RunIndexStore:
        """Get run index store."""
        if self._run_index_store is None:
            self._run_index_store = RunIndexStore(self.state_path)
        return self._run_index_store

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
        except Exception:
            return ""

    def log_line(self, run_id: int, message: str) -> None:
        """Append a line to the run log."""
        run_log_path = self._run_log_path(run_id)
        run_log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = now_iso()
        with open(run_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")

    def _run_log_path(self, run_id: int) -> Path:
        """Get path to run log file."""
        return self.state_root / "runs" / str(run_id) / "run.log"

    def read_run_block(self, run_id: int) -> Optional[str]:
        """Read the run log block for a given run ID."""
        run_log_path = self._run_log_path(run_id)
        if not run_log_path.exists():
            return None
        try:
            with open(run_log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return None

    def reconcile_run_index(self) -> None:
        """Reconcile run index with run directories."""
        runs_dir = self.state_root / "runs"
        if not runs_dir.exists():
            return

        for run_dir in sorted(runs_dir.iterdir(), key=lambda p: int(p.name)):
            try:
                run_id = int(run_dir.name)
                self._merge_run_index_entry(run_id, {})
            except (ValueError, OSError):
                continue

    def _merge_run_index_entry(self, run_id: int, extra: dict[str, Any]) -> None:
        """Merge extra data into run index entry."""
        # Ensure timestamp if missing
        if "timestamp" not in extra:
            extra["timestamp"] = now_iso()

        self.run_index_store.merge_entry(run_id, extra)


__all__ = [
    "RuntimeContext",
    "LockError",
    "doctor",
    "DoctorCheck",
    "DoctorReport",
    "clear_stale_lock",
]
