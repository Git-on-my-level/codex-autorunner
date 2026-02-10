"""Runtime context module.

Provides RuntimeContext as a minimal runtime helper for ticket flows.
This replaces Engine as the runtime authority while preserving utility functions.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from ..manifest import load_manifest
from .config import HubConfig, RepoConfig, load_repo_config
from .locks import DEFAULT_RUNNER_CMD_HINTS, assess_lock
from .notifications import NotificationManager
from .run_index import RunIndexStore
from .runner_state import LockError, RunnerStateManager
from .state import now_iso
from .utils import RepoNotFoundError, find_repo_root

_logger = logging.getLogger(__name__)

PMA_STATE_FILE = ".codex-autorunner/pma/state.json"
PMA_QUEUE_DIR = ".codex-autorunner/pma/queue"
STUCK_LANE_THRESHOLD_MINUTES = 60


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
    config_path = repo_root / ".codex-autorunner" / "config.yml"
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

    # Check for workspace → contextspace migration
    workspace_dir = state_root / "workspace"
    if workspace_dir.exists() and workspace_dir.is_dir():
        workspace_content = (
            list(workspace_dir.iterdir()) if workspace_dir.is_dir() else []
        )
        if workspace_content:
            checks.append(
                DoctorCheck(
                    name="Workspace migration",
                    passed=False,
                    message="Old workspace directory still has content; run migration script",
                    severity="warning",
                    check_id=check_id,
                    fix=f"./scripts/migrate-workspace-to-contextspace.sh --repo {repo_root}",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Workspace migration",
                    passed=True,
                    message="Workspace exists but is empty; consider removing",
                    severity="info",
                    check_id=check_id,
                    fix=f"rmdir {workspace_dir}",
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


def pma_doctor_checks(
    config: Union[HubConfig, RepoConfig, dict[str, Any]],
    repo_root: Optional[Path] = None,
) -> list[DoctorCheck]:
    """Run PMA-specific doctor checks.

    Returns a list of DoctorCheck objects for PMA integration.
    Works with HubConfig, RepoConfig, or raw dict.

    Args:
        config: HubConfig, RepoConfig, or raw dict
        repo_root: Optional repo root path for state and queue checks
    """
    checks: list[DoctorCheck] = []

    pma_cfg = None
    if isinstance(config, dict):
        pma_cfg = config.get("pma")
    elif hasattr(config, "raw"):
        pma_cfg = config.raw.get("pma") if isinstance(config.raw, dict) else None

    if not isinstance(pma_cfg, dict):
        checks.append(
            DoctorCheck(
                name="PMA config",
                passed=False,
                message="PMA configuration not found",
                check_id="pma.config",
                severity="info",
            )
        )
        return checks

    enabled = pma_cfg.get("enabled", True)
    if not enabled:
        checks.append(
            DoctorCheck(
                name="PMA enabled",
                passed=True,
                message="PMA is disabled in config",
                check_id="pma.enabled",
                severity="info",
            )
        )
        return checks

    checks.append(
        DoctorCheck(
            name="PMA enabled",
            passed=True,
            message="PMA is enabled",
            check_id="pma.enabled",
            severity="info",
        )
    )

    default_agent = pma_cfg.get("default_agent", "codex")
    if default_agent not in ("codex", "opencode"):
        checks.append(
            DoctorCheck(
                name="PMA default agent",
                passed=False,
                message=f"Invalid PMA default_agent: {default_agent}",
                check_id="pma.default_agent",
                fix="Set pma.default_agent to 'codex' or 'opencode' in config.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="PMA default agent",
                passed=True,
                message=f"Default agent: {default_agent}",
                check_id="pma.default_agent",
                severity="info",
            )
        )

    model = pma_cfg.get("model")
    if model:
        checks.append(
            DoctorCheck(
                name="PMA model",
                passed=True,
                message=f"Model configured: {model}",
                check_id="pma.model",
                severity="info",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="PMA model",
                passed=True,
                message="Using default model (none specified)",
                check_id="pma.model",
                severity="info",
            )
        )

    if repo_root:
        _check_pma_state_file(checks, repo_root)
        _check_pma_queue(checks, repo_root)
        _check_pma_artifacts(checks, repo_root)

    return checks


def hub_worktree_doctor_checks(hub_config: HubConfig) -> list[DoctorCheck]:
    """Check for unregistered worktrees under the hub worktrees root."""
    checks: list[DoctorCheck] = []
    worktrees_root = hub_config.worktrees_root
    manifest = load_manifest(hub_config.manifest_path, hub_config.root)
    manifest_paths = {
        (hub_config.root / repo.path).resolve() for repo in manifest.repos
    }

    orphans: list[Path] = []
    if worktrees_root.exists():
        try:
            entries = list(worktrees_root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink():
                continue
            if not (entry / ".git").exists():
                continue
            resolved = entry.resolve()
            if resolved not in manifest_paths:
                orphans.append(resolved)

    if orphans:
        checks.append(
            DoctorCheck(
                name="Hub worktrees registered",
                passed=False,
                message=(
                    f"{len(orphans)} worktree(s) exist under {worktrees_root} "
                    "but are not in the hub manifest"
                ),
                severity="warning",
                fix=f"Run: car hub scan --path {hub_config.root}",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Hub worktrees registered",
                passed=True,
                message="OK",
                severity="warning",
            )
        )
    return checks


def _check_pma_state_file(checks: list[DoctorCheck], repo_root: Path) -> None:
    """Check PMA state file."""
    state_path = repo_root / PMA_STATE_FILE
    if not state_path.exists():
        checks.append(
            DoctorCheck(
                name="PMA state file",
                passed=False,
                message=f"PMA state file not found: {state_path}",
                check_id="pma.state_file",
                severity="warning",
                fix="Run a PMA command to initialize state file.",
            )
        )
        return

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            checks.append(
                DoctorCheck(
                    name="PMA state file",
                    passed=False,
                    message=f"PMA state file is not a valid JSON object: {state_path}",
                    check_id="pma.state_file",
                    fix="Delete corrupt state file and reinitialize.",
                )
            )
            return

        version = state.get("version")
        active = state.get("active", False)
        updated_at = state.get("updated_at")

        checks.append(
            DoctorCheck(
                name="PMA state file",
                passed=True,
                message=f"State file OK (version={version}, active={active})",
                check_id="pma.state_file",
                severity="info",
            )
        )

        if active and updated_at:
            try:
                updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - updated_dt
                if age > timedelta(minutes=STUCK_LANE_THRESHOLD_MINUTES):
                    checks.append(
                        DoctorCheck(
                            name="PMA activity",
                            passed=False,
                            message=f"PMA appears stuck (last update {age.total_seconds() / 60:.0f}m ago)",
                            check_id="pma.activity",
                            fix="Check PMA logs and consider running a reset command.",
                        )
                    )
            except (ValueError, TypeError):
                pass
    except (json.JSONDecodeError, OSError) as exc:
        checks.append(
            DoctorCheck(
                name="PMA state file",
                passed=False,
                message=f"Failed to read PMA state file: {exc}",
                check_id="pma.state_file",
                severity="error",
                fix="Check file permissions or delete corrupt state file.",
            )
        )


def _check_pma_queue(checks: list[DoctorCheck], repo_root: Path) -> None:
    """Check PMA queue for stuck items."""
    queue_dir = repo_root / PMA_QUEUE_DIR
    if not queue_dir.exists():
        checks.append(
            DoctorCheck(
                name="PMA queue",
                passed=True,
                message="PMA queue directory not created yet",
                check_id="pma.queue",
                severity="info",
            )
        )
        return

    try:
        lane_files = list(queue_dir.glob("*.jsonl"))
        total_lanes = len(lane_files)

        if total_lanes == 0:
            checks.append(
                DoctorCheck(
                    name="PMA queue",
                    passed=True,
                    message="No active PMA lanes",
                    check_id="pma.queue",
                    severity="info",
                )
            )
            return

        threshold = datetime.now(timezone.utc) - timedelta(
            minutes=STUCK_LANE_THRESHOLD_MINUTES
        )
        stuck_lanes = []

        for lane_file in lane_files:
            try:
                with open(lane_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                            state = item.get("state")
                            started_at = item.get("started_at")
                            if state == "running" and started_at:
                                try:
                                    started_dt = datetime.fromisoformat(
                                        started_at.replace("Z", "+00:00")
                                    )
                                    if started_dt.tzinfo is None:
                                        started_dt = started_dt.replace(
                                            tzinfo=timezone.utc
                                        )
                                    if started_dt < threshold:
                                        lane_id = item.get("lane_id", "unknown")
                                        stuck_lanes.append(lane_id)
                                        break
                                except (ValueError, TypeError):
                                    continue
                        except (json.JSONDecodeError, TypeError):
                            continue
            except OSError:
                continue

        if stuck_lanes:
            checks.append(
                DoctorCheck(
                    name="PMA queue",
                    passed=False,
                    message=f"Found {len(stuck_lanes)} stuck lane(s): {', '.join(stuck_lanes)}",
                    check_id="pma.queue",
                    fix=f"Run 'car pma stop' for stuck lanes or check logs at {queue_dir}",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="PMA queue",
                    passed=True,
                    message=f"PMA queue OK ({total_lanes} active lane(s))",
                    check_id="pma.queue",
                    severity="info",
                )
            )
    except OSError as exc:
        checks.append(
            DoctorCheck(
                name="PMA queue",
                passed=False,
                message=f"Failed to check PMA queue: {exc}",
                check_id="pma.queue",
                severity="warning",
                fix="Check permissions on queue directory.",
            )
        )


def _check_pma_artifacts(checks: list[DoctorCheck], repo_root: Path) -> None:
    """Check PMA artifact integrity."""
    pma_dir = repo_root / ".codex-autorunner" / "pma"
    if not pma_dir.exists():
        checks.append(
            DoctorCheck(
                name="PMA artifacts",
                passed=True,
                message="PMA directory not created yet",
                check_id="pma.artifacts",
                severity="info",
            )
        )
        return

    state_file = pma_dir / "state.json"
    queue_dir = pma_dir / "queue"
    lifecycle_dir = pma_dir / "lifecycle"

    artifacts_ok = True
    missing = []

    if not state_file.exists():
        missing.append("state.json")
        artifacts_ok = False

    if not queue_dir.exists():
        missing.append("queue/")
        artifacts_ok = False

    if not lifecycle_dir.exists():
        missing.append("lifecycle/")
        artifacts_ok = False

    if artifacts_ok:
        checks.append(
            DoctorCheck(
                name="PMA artifacts",
                passed=True,
                message=f"PMA artifacts OK at {pma_dir}",
                check_id="pma.artifacts",
                severity="info",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="PMA artifacts",
                passed=False,
                message=f"Missing PMA artifacts: {', '.join(missing)}",
                check_id="pma.artifacts",
                fix="Run a PMA command to initialize artifacts.",
            )
        )


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
        # Historical runs are stored under numeric directories like `runs/123/`.
        # Be defensive: other artifacts (UUID directories, stray files) can exist and
        # should not break reconciliation.
        parsed: list[tuple[int, Path]] = []
        try:
            entries = list(runs_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                run_id = int(entry.name)
            except ValueError:
                continue
            parsed.append((run_id, entry))
        for run_id, _ in sorted(parsed, key=lambda pair: pair[0]):
            self._merge_run_index_entry(run_id, {})

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
    "hub_worktree_doctor_checks",
    "pma_doctor_checks",
]
