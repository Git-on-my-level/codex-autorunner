"""Runtime context module.

Provides RuntimeContext as a minimal runtime helper for ticket flows.
This replaces Engine as the runtime authority while preserving utility functions.
"""

import importlib
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Optional, Union

from ..manifest import load_manifest, load_manifest_with_issues
from ..voice.config import VoiceConfig
from ..voice.provider_catalog import (
    local_voice_provider_spec,
    missing_local_voice_runtime_commands,
)
from .config import HubConfig, RepoConfig, load_repo_config
from .destinations import (
    probe_docker_readiness,
    resolve_effective_agent_workspace_destination,
    resolve_effective_repo_destination,
)
from .locks import DEFAULT_RUNNER_CMD_HINTS, assess_lock, process_command_matches
from .managed_processes import ProcessRecord, list_process_records
from .notifications import NotificationManager
from .optional_dependencies import missing_optional_dependencies
from .runner_state import LockError, RunnerStateManager
from .state import now_iso
from .state_roots import (
    REPO_STATE_DIR,
    resolve_global_state_root,
    resolve_repo_state_root,
)
from .utils import RepoNotFoundError, find_repo_root, resolve_executable

_logger = logging.getLogger(__name__)

PMA_STATE_FILE = f"{REPO_STATE_DIR}/pma/state.json"
PMA_QUEUE_DIR = f"{REPO_STATE_DIR}/pma/queue"
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


def _zeroclaw_runtime_preflight(hub_config: HubConfig):
    module = importlib.import_module("codex_autorunner.agents.zeroclaw.supervisor")
    return module.zeroclaw_runtime_preflight(hub_config)


def zeroclaw_runtime_preflight(hub_config: HubConfig):
    return _zeroclaw_runtime_preflight(hub_config)


def _hermes_runtime_preflight(hub_config: HubConfig, *, agent_id: str = "hermes"):
    module = importlib.import_module("codex_autorunner.agents.hermes.supervisor")
    return module.hermes_runtime_preflight(hub_config, agent_id=agent_id)


def hermes_runtime_preflight(hub_config: HubConfig, *, agent_id: str = "hermes"):
    return _hermes_runtime_preflight(hub_config, agent_id=agent_id)


def _run_hermes_runtime_preflight(hub_config: HubConfig, *, agent_id: str = "hermes"):
    try:
        return hermes_runtime_preflight(hub_config, agent_id=agent_id)
    except TypeError as exc:
        if "agent_id" not in str(exc):
            raise
        return hermes_runtime_preflight(hub_config)


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
    except (subprocess.SubprocessError, OSError, RuntimeError) as e:
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
            repo_config = load_repo_config(repo_root)
            checks.append(
                DoctorCheck(
                    name="Config file",
                    passed=True,
                    message="OK",
                    severity="info",
                    check_id=check_id,
                )
            )
            _append_local_voice_dependency_check(
                checks, repo_config=repo_config, check_id=check_id
            )
            _append_opencode_lifecycle_checks(
                checks,
                repo_root=repo_root,
                repo_config=repo_config,
                backend_orchestrator=backend_orchestrator,
                check_id=check_id,
            )
        except (ValueError, TypeError, OSError, RuntimeError) as e:
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

    _append_render_dependency_checks(checks, check_id=check_id)

    return DoctorReport(checks)


def summarize_opencode_lifecycle(
    repo_root: Path,
    *,
    repo_config: Optional[RepoConfig] = None,
    backend_orchestrator: Optional[Any] = None,
) -> dict[str, Any]:
    if repo_config is None:
        repo_config = load_repo_config(repo_root)

    agent_cfg = repo_config.agents.get("opencode")
    external_base_url = agent_cfg.base_url if agent_cfg else None
    server_scope = getattr(repo_config.opencode, "server_scope", "workspace")
    registry_repo_root = (
        resolve_global_state_root().resolve()
        if server_scope == "global"
        else repo_root.resolve()
    )

    try:
        records = list_process_records(registry_repo_root, kind="opencode")
    except (RuntimeError, OSError, ValueError, TypeError):
        records = []

    deduped_records: list[ProcessRecord] = []
    records_by_pid: dict[int, ProcessRecord] = {}
    for record in records:
        if record.pid is None:
            deduped_records.append(record)
            continue
        existing = records_by_pid.get(record.pid)
        if existing is None or (
            existing.workspace_id is None and record.workspace_id is not None
        ):
            records_by_pid[record.pid] = record

    seen_pid_records = {id(record) for record in records_by_pid.values()}
    deduped_records.extend(
        record
        for record in records
        if record.pid is not None and id(record) in seen_pid_records
    )

    managed_servers: list[dict[str, Any]] = []
    for record in deduped_records:
        status = "active" if _opencode_record_is_running(record) else "stale"
        metadata = record.metadata if isinstance(record.metadata, dict) else {}
        managed_servers.append(
            {
                "workspace_id": record.workspace_id,
                "pid": record.pid,
                "pgid": record.pgid,
                "base_url": record.base_url,
                "owner_pid": record.owner_pid,
                "owner_alive": _pid_exists(record.owner_pid),
                "status": status,
                "workspace_root": metadata.get("workspace_root"),
                "server_scope": metadata.get("server_scope") or server_scope,
                "process_origin": metadata.get("process_origin") or "unknown",
                "last_attach_mode": metadata.get("last_attach_mode") or "unknown",
            }
        )

    live_handles: list[dict[str, Any]] = []
    supervisor = _existing_opencode_supervisor(backend_orchestrator)
    if supervisor is not None:
        snapshot = getattr(supervisor, "observability_snapshot", None)
        if callable(snapshot):
            try:
                payload = snapshot()
            except (RuntimeError, OSError, ValueError, TypeError):
                payload = {}
            if isinstance(payload, dict):
                raw_handles = payload.get("handles")
                if isinstance(raw_handles, list):
                    live_handles = [
                        handle for handle in raw_handles if isinstance(handle, dict)
                    ]

    counts = {
        "active": sum(1 for record in managed_servers if record["status"] == "active"),
        "stale": sum(1 for record in managed_servers if record["status"] == "stale"),
        "spawned_local": sum(
            1
            for record in managed_servers
            if record["process_origin"] == "spawned_local"
        ),
        "registry_reuse": sum(
            1
            for record in managed_servers
            if record["last_attach_mode"] == "registry_reuse"
        ),
    }

    return {
        "server_scope": server_scope,
        "external_base_url": external_base_url,
        "registry_root": str(registry_repo_root),
        "counts": counts,
        "managed_servers": managed_servers,
        "live_handles": live_handles,
    }


def _append_opencode_lifecycle_checks(
    checks: list[DoctorCheck],
    *,
    repo_root: Path,
    repo_config: RepoConfig,
    backend_orchestrator: Optional[Any],
    check_id: Optional[str],
) -> None:
    summary = summarize_opencode_lifecycle(
        repo_root,
        repo_config=repo_config,
        backend_orchestrator=backend_orchestrator,
    )
    lifecycle_check_id = check_id or "opencode.lifecycle.registry"
    external_check_id = check_id or "opencode.lifecycle.external"
    handles_check_id = check_id or "opencode.lifecycle.handles"

    external_base_url = summary.get("external_base_url")
    if isinstance(external_base_url, str) and external_base_url:
        checks.append(
            DoctorCheck(
                name="OpenCode external mode",
                passed=True,
                message=(
                    f"External OpenCode base_url configured: {external_base_url}. "
                    "No CAR-managed server teardown is expected for this path."
                ),
                severity="info",
                check_id=external_check_id,
            )
        )

    managed_servers = summary.get("managed_servers") or []
    if not managed_servers:
        checks.append(
            DoctorCheck(
                name="OpenCode lifecycle",
                passed=True,
                message="No CAR-managed OpenCode server records found.",
                severity="info",
                check_id=lifecycle_check_id,
            )
        )
    else:
        counts = summary.get("counts") or {}
        stale_records = int(counts.get("stale") or 0)
        sample = ", ".join(
            "{workspace_id}:{pid}:{mode}".format(
                workspace_id=record.get("workspace_id") or "pid-only",
                pid=record.get("pid") or "n/a",
                mode=record.get("last_attach_mode") or record.get("process_origin"),
            )
            for record in managed_servers[:3]
        )
        checks.append(
            DoctorCheck(
                name="OpenCode lifecycle",
                passed=stale_records == 0,
                message=(
                    "Managed OpenCode server records: "
                    f"active={counts.get('active', 0)} stale={stale_records} "
                    f"spawned_local={counts.get('spawned_local', 0)} "
                    f"registry_reuse={counts.get('registry_reuse', 0)} "
                    f"(registry={summary.get('registry_root')}). "
                    f"Sample: {sample}"
                ),
                severity="warning" if stale_records else "info",
                check_id=lifecycle_check_id,
                fix=(
                    None
                    if stale_records == 0
                    else "Inspect `car doctor processes --save` and clear stale OpenCode records/processes."
                ),
            )
        )

    live_handles = summary.get("live_handles") or []
    if live_handles:
        handle_counts: dict[str, int] = {}
        for handle in live_handles:
            mode = str(handle.get("mode") or "unknown")
            handle_counts[mode] = handle_counts.get(mode, 0) + 1
        checks.append(
            DoctorCheck(
                name="OpenCode live handles",
                passed=True,
                message=(
                    "Live OpenCode handles: "
                    + ", ".join(
                        f"{mode}={handle_counts[mode]}"
                        for mode in sorted(handle_counts)
                    )
                ),
                severity="info",
                check_id=handles_check_id,
            )
        )


def _pid_exists(pid: Optional[int]) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _opencode_record_is_running(record: ProcessRecord) -> bool:
    pid = record.pid
    if pid is not None and _pid_exists(pid):
        cmd_matches = process_command_matches(pid, record.command)
        if cmd_matches is not False:
            return True
    pgid = record.pgid
    if pgid is None or os.name == "nt" or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False
    return True


def _existing_opencode_supervisor(
    backend_orchestrator: Optional[Any],
) -> Optional[Any]:
    if backend_orchestrator is None:
        return None

    active_backend = getattr(backend_orchestrator, "_active_backend", None)
    supervisor = getattr(active_backend, "_supervisor", None)
    if supervisor is not None:
        return supervisor

    factory_getter = getattr(backend_orchestrator, "_agent_backend_factory", None)
    if callable(factory_getter):
        try:
            factory = factory_getter()
        except (RuntimeError, OSError, ValueError, TypeError):
            factory = None
        supervisor = getattr(factory, "_opencode_supervisor", None)
        if supervisor is not None:
            return supervisor

    return None


def _append_local_voice_dependency_check(
    checks: list[DoctorCheck],
    *,
    repo_config: RepoConfig,
    check_id: Optional[str],
) -> None:
    voice_cfg = VoiceConfig.from_raw(repo_config.voice, env=os.environ)
    if not voice_cfg.enabled:
        return

    provider_spec = local_voice_provider_spec(voice_cfg.provider)
    if provider_spec is None:
        return
    provider, deps, extra = provider_spec

    missing_local_voice = missing_optional_dependencies(deps)
    missing_runtime_commands = missing_local_voice_runtime_commands(provider)
    if missing_local_voice:
        missing_desc = ", ".join(missing_local_voice)
        runtime_hint = ""
        if missing_runtime_commands:
            missing_runtime_desc = ", ".join(missing_runtime_commands)
            runtime_hint = (
                " Required runtime command(s) are also missing from PATH: "
                f"{missing_runtime_desc}."
            )
        checks.append(
            DoctorCheck(
                name="Voice dependencies",
                passed=False,
                message=(
                    f"Voice is enabled with {provider} but {missing_desc} is "
                    f"not installed.{runtime_hint}"
                ),
                severity="error",
                check_id=check_id or "voice.dependencies",
                fix=(
                    f"Install with `pip install codex-autorunner[{extra}]`."
                    + (
                        " Install ffmpeg and ensure it is on PATH (for macOS: "
                        "`brew install ffmpeg`)."
                        if "ffmpeg" in missing_runtime_commands
                        else ""
                    )
                ),
            )
        )
        return

    if missing_runtime_commands:
        missing_runtime_desc = ", ".join(missing_runtime_commands)
        checks.append(
            DoctorCheck(
                name="Voice dependencies",
                passed=False,
                message=(
                    f"Voice is enabled with {provider} but required runtime "
                    f"command(s) are missing from PATH: {missing_runtime_desc}."
                ),
                severity="error",
                check_id=check_id or "voice.dependencies",
                fix=(
                    "Install ffmpeg and ensure it is on PATH (for macOS: "
                    "`brew install ffmpeg`)."
                ),
            )
        )
        return

    checks.append(
        DoctorCheck(
            name="Voice dependencies",
            passed=True,
            message=f"Voice local dependencies for {provider} are installed.",
            severity="info",
            check_id=check_id or "voice.dependencies",
        )
    )


def _append_render_dependency_checks(
    checks: list[DoctorCheck],
    *,
    check_id: Optional[str],
) -> None:
    render_check_id = check_id or "render.browser.dependencies"
    has_playwright = find_spec("playwright") is not None
    if not has_playwright:
        checks.append(
            DoctorCheck(
                name="Render browser dependencies",
                passed=True,
                message=(
                    "Playwright Python package is not installed; browser render "
                    "commands are unavailable."
                ),
                severity="warning",
                check_id=render_check_id,
                fix=(
                    "Install with `pip install codex-autorunner[browser]` (or "
                    "`pip install -e .[browser]` for local dev), then run "
                    "`python -m playwright install chromium`."
                ),
            )
        )
    else:
        chromium_path: Optional[str] = None
        chromium_error: Optional[str] = None
        try:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            try:
                chromium_path = str(playwright.chromium.executable_path or "").strip()
            finally:
                playwright.stop()
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            chromium_error = str(exc).strip() or repr(exc)

        if chromium_path and Path(chromium_path).exists():
            checks.append(
                DoctorCheck(
                    name="Render browser dependencies",
                    passed=True,
                    message=f"Playwright and Chromium are available ({chromium_path}).",
                    severity="info",
                    check_id=render_check_id,
                )
            )
        else:
            detail = (
                f" ({chromium_error})"
                if chromium_error
                else " (Chromium browser binary missing)"
            )
            checks.append(
                DoctorCheck(
                    name="Render browser dependencies",
                    passed=True,
                    message=(
                        "Playwright is installed but Chromium runtime is unavailable."
                        f"{detail}"
                    ),
                    severity="warning",
                    check_id=render_check_id,
                    fix=(
                        "Install browser runtime with "
                        "`python -m playwright install chromium`."
                    ),
                )
            )

    mmdc = resolve_executable("mmdc")
    if mmdc:
        checks.append(
            DoctorCheck(
                name="Render markdown dependencies",
                passed=True,
                message=f"Mermaid CLI available at {mmdc}.",
                severity="info",
                check_id=check_id or "render.markdown.dependencies",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Render markdown dependencies",
                passed=True,
                message=(
                    "Mermaid CLI (mmdc) is not installed; `car render markdown` "
                    "diagram exports are unavailable."
                ),
                severity="warning",
                check_id=check_id or "render.markdown.dependencies",
                fix="Install Mermaid CLI (`npm i -g @mermaid-js/mermaid-cli`).",
            )
        )

    pandoc = resolve_executable("pandoc")
    if pandoc:
        checks.append(
            DoctorCheck(
                name="Render markdown dependencies",
                passed=True,
                message=f"Pandoc available at {pandoc}.",
                severity="info",
                check_id=check_id or "render.markdown.dependencies",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Render markdown dependencies",
                passed=True,
                message=(
                    "Pandoc is not installed; `car render markdown` document "
                    "exports are unavailable."
                ),
                severity="warning",
                check_id=check_id or "render.markdown.dependencies",
                fix="Install Pandoc and ensure it is on PATH.",
            )
        )


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
    from ..integrations.agents import get_registered_agents

    registered_agents = get_registered_agents()
    if default_agent not in registered_agents:
        available = ", ".join(sorted(registered_agents.keys()))
        checks.append(
            DoctorCheck(
                name="PMA default agent",
                passed=False,
                message=f"Invalid PMA default_agent: {default_agent}",
                check_id="pma.default_agent",
                fix=f"Set pma.default_agent to a registered agent. Available: {available}",
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
                    "but are not in the hub manifest. "
                    "Orphaned worktrees are not auto-deleted per PMA policy; "
                    "use explicit cleanup commands to remove them."
                ),
                severity="warning",
                fix=f"Run: car hub scan --path {hub_config.root} to register them, "
                "or use: car hub worktree cleanup <repo_id> --archive to archive and remove.",
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


def hub_destination_doctor_checks(hub_config: HubConfig) -> list[DoctorCheck]:
    """Report effective destination status and validation issues for hub resources."""
    checks: list[DoctorCheck] = []

    try:
        manifest, manifest_issues = load_manifest_with_issues(
            hub_config.manifest_path, hub_config.root
        )
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        checks.append(
            DoctorCheck(
                name="Hub destination configuration",
                passed=False,
                message=f"Failed to load hub manifest for destination checks: {exc}",
                severity="warning",
                check_id="hub.destination",
                fix=f"Validate manifest at {hub_config.manifest_path}",
            )
        )
        return checks

    repos_by_id = {repo.id: repo for repo in manifest.repos}
    known_repo_ids = set(repos_by_id.keys())
    issues_by_repo: dict[str, list[str]] = {}
    for issue in manifest_issues:
        issues_by_repo.setdefault(issue.repo_id, []).append(issue.message)

    if not manifest.repos and not manifest.agent_workspaces:
        checks.append(
            DoctorCheck(
                name="Hub destination configuration",
                passed=True,
                message="No managed resources in hub manifest.",
                severity="info",
                check_id="hub.destination",
            )
        )
        return checks

    docker_targets: list[str] = []

    for repo in manifest.repos:
        resolution = resolve_effective_repo_destination(repo, repos_by_id)
        kind = resolution.destination.kind
        source = resolution.source
        if kind == "docker":
            docker_targets.append(f"repo:{repo.id}")
        checks.append(
            DoctorCheck(
                name=f"Hub destination ({repo.id})",
                passed=True,
                message=f"{repo.id}: effective destination '{kind}' (source={source})",
                severity="info",
                check_id="hub.destination",
            )
        )

        issue_messages: list[str] = []
        issue_messages.extend(list(resolution.issues))
        issue_messages.extend(issues_by_repo.get(repo.id, []))
        # preserve order while de-duping repeated issue strings
        deduped_messages = list(dict.fromkeys(issue_messages))
        for message in deduped_messages:
            checks.append(
                DoctorCheck(
                    name=f"Hub destination ({repo.id})",
                    passed=False,
                    message=f"{repo.id}: {message}",
                    severity="warning",
                    check_id="hub.destination",
                    fix=(
                        "Update destination config for this repo in "
                        f"{hub_config.manifest_path}"
                    ),
                )
            )

    for workspace in manifest.agent_workspaces:
        resolution = resolve_effective_agent_workspace_destination(workspace)
        kind = resolution.destination.kind
        source = resolution.source
        if kind == "docker":
            docker_targets.append(f"agent_workspace:{workspace.id}")
        checks.append(
            DoctorCheck(
                name=f"Hub destination ({workspace.id})",
                passed=True,
                message=(
                    f"{workspace.id}: effective destination '{kind}' (source={source})"
                ),
                severity="info",
                check_id="hub.destination",
            )
        )

        workspace_issue_messages: list[str] = []
        workspace_issue_messages.extend(list(resolution.issues))
        workspace_issue_messages.extend(issues_by_repo.get(workspace.id, []))
        deduped_messages = list(dict.fromkeys(workspace_issue_messages))
        for message in deduped_messages:
            checks.append(
                DoctorCheck(
                    name=f"Hub destination ({workspace.id})",
                    passed=False,
                    message=f"{workspace.id}: {message}",
                    severity="warning",
                    check_id="hub.destination",
                    fix=(
                        "Update destination config for this agent workspace in "
                        f"{hub_config.manifest_path}"
                    ),
                )
            )

    for repo_id, messages in sorted(issues_by_repo.items()):
        if (
            repo_id in known_repo_ids
            or manifest.get_agent_workspace(repo_id) is not None
        ):
            continue
        for message in list(dict.fromkeys(messages)):
            checks.append(
                DoctorCheck(
                    name=f"Hub destination ({repo_id})",
                    passed=False,
                    message=f"{repo_id}: {message}",
                    severity="warning",
                    check_id="hub.destination",
                    fix=f"Update malformed manifest repo entry in {hub_config.manifest_path}",
                )
            )

    if docker_targets:
        readiness = probe_docker_readiness()
        resource_targets = ", ".join(sorted(docker_targets))
        checks.append(
            DoctorCheck(
                name="Hub destination (docker binary)",
                passed=readiness.binary_available,
                message=(
                    "Docker CLI is available."
                    if readiness.binary_available
                    else f"Docker CLI unavailable: {readiness.detail}"
                ),
                severity="info" if readiness.binary_available else "warning",
                check_id="hub.destination.docker.binary",
                fix=(
                    None
                    if readiness.binary_available
                    else "Install Docker CLI and ensure it is in PATH."
                ),
            )
        )
        checks.append(
            DoctorCheck(
                name="Hub destination (docker daemon)",
                passed=readiness.daemon_reachable,
                message=(
                    f"Docker daemon reachable for resources: {resource_targets}. "
                    f"{readiness.detail}"
                    if readiness.daemon_reachable
                    else (
                        "Docker daemon unreachable for resources: "
                        f"{resource_targets}. {readiness.detail or 'Run `docker info` for details.'}"
                    )
                ),
                severity="info" if readiness.daemon_reachable else "warning",
                check_id="hub.destination.docker.daemon",
                fix=(
                    None
                    if readiness.daemon_reachable
                    else (
                        "Start Docker daemon/Desktop and rerun `docker info`. "
                        "Destination kind=docker requires daemon connectivity."
                    )
                ),
            )
        )

    return checks


def zeroclaw_doctor_checks(hub_config: HubConfig) -> list[DoctorCheck]:
    """Report ZeroClaw runtime compatibility when managed ZeroClaw usage exists."""
    checks: list[DoctorCheck] = []
    try:
        manifest = load_manifest(hub_config.manifest_path, hub_config.root)
    except (ValueError, TypeError, OSError, RuntimeError, AttributeError):
        manifest = None

    enabled_workspaces: list[str] = []
    if manifest is not None:
        enabled_workspaces = sorted(
            workspace.id
            for workspace in manifest.agent_workspaces
            if workspace.enabled and workspace.runtime.strip().lower() == "zeroclaw"
        )

    try:
        configured_binary = hub_config.agent_binary("zeroclaw").strip()
    except (ValueError, TypeError, OSError, RuntimeError, AttributeError):
        configured_binary = ""

    explicit_binary_override = bool(
        configured_binary and configured_binary != "zeroclaw"
    )
    if not enabled_workspaces and not explicit_binary_override:
        return checks

    workspace_suffix = ""
    if enabled_workspaces:
        workspace_suffix = f" for enabled workspaces: {', '.join(enabled_workspaces)}"
    result = zeroclaw_runtime_preflight(hub_config)
    severity = (
        "info"
        if result.status == "ready"
        else ("error" if enabled_workspaces else "warning")
    )
    fix = result.fix
    message = result.message
    if workspace_suffix:
        message = f"{message.rstrip('.')}{workspace_suffix}."
    if result.status == "ready":
        detail_parts = []
        if result.version:
            detail_parts.append(result.version)
        if result.launch_mode:
            detail_parts.append(f"launch_mode={result.launch_mode}")
        suffix = f" ({', '.join(detail_parts)})" if detail_parts else ""
        checks.append(
            DoctorCheck(
                name="ZeroClaw runtime availability",
                passed=True,
                message=f"{message.rstrip('.')}{suffix}.",
                severity="info",
                check_id="hub.zeroclaw.binary",
            )
        )
        return checks

    checks.append(
        DoctorCheck(
            name="ZeroClaw runtime availability",
            passed=False,
            message=message,
            severity=severity,
            check_id="hub.zeroclaw.binary",
            fix=fix,
        )
    )
    return checks


def hermes_doctor_checks(hub_config: HubConfig) -> list[DoctorCheck]:
    """Report Hermes runtime compatibility when managed Hermes usage exists."""
    checks: list[DoctorCheck] = []
    try:
        manifest = load_manifest(hub_config.manifest_path, hub_config.root)
    except (ValueError, TypeError, OSError, RuntimeError, AttributeError):
        manifest = None

    enabled_workspaces: list[str] = []
    if manifest is not None:
        enabled_workspaces = sorted(
            workspace.id
            for workspace in manifest.agent_workspaces
            if workspace.enabled and workspace.runtime.strip().lower() == "hermes"
        )

    workspace_suffix = ""
    if enabled_workspaces:
        workspace_suffix = f" for enabled workspaces: {', '.join(enabled_workspaces)}"

    configured_hermes_agents: list[str] = []
    hermes_agent_ids_seen: set[str] = set()
    for agent_id in sorted(getattr(hub_config, "agents", {}).keys()):
        try:
            backend_id = hub_config.agent_backend(agent_id)
        except (ValueError, TypeError, OSError, RuntimeError, AttributeError):
            backend_id = agent_id
        if str(backend_id or "").strip().lower() == "hermes":
            configured_hermes_agents.append(agent_id)
            hermes_agent_ids_seen.add(agent_id)

    for agent_id in sorted(getattr(hub_config, "agents", {}).keys()):
        if agent_id in hermes_agent_ids_seen:
            continue
        normalized_id = str(agent_id or "").strip().lower()
        if not (
            normalized_id.startswith("hermes-") or normalized_id.startswith("hermes_")
        ):
            continue
        try:
            agent_cfg = hub_config.agents.get(agent_id)
            explicit_backend = getattr(agent_cfg, "backend", None)
            if (
                isinstance(explicit_backend, str)
                and explicit_backend.strip()
                and explicit_backend.strip().lower() != "hermes"
            ):
                continue
        except (AttributeError, TypeError, KeyError):
            pass
        configured_hermes_agents.append(agent_id)
        hermes_agent_ids_seen.add(agent_id)
        checks.append(
            DoctorCheck(
                name=f"Hermes alias metadata ({agent_id})",
                passed=False,
                message=(
                    f"Agent {agent_id!r} looks like a Hermes alias (id prefix) but has no "
                    "explicit backend: hermes metadata."
                ),
                severity="warning",
                check_id=f"hub.hermes.alias_metadata.{agent_id}",
                fix="Set backend: hermes for this agent in hub configuration.",
            )
        )

    if not configured_hermes_agents:
        configured_hermes_agents = ["hermes"]

    should_report = bool(enabled_workspaces)
    agent_ids_to_check: list[str] = []
    for agent_id in configured_hermes_agents:
        try:
            configured_binary = hub_config.agent_binary(agent_id).strip()
        except (ValueError, TypeError, OSError, RuntimeError, AttributeError):
            configured_binary = ""
        is_alias = agent_id != "hermes"
        explicit_binary_override = bool(
            configured_binary and configured_binary != "hermes"
        )
        if enabled_workspaces or is_alias or explicit_binary_override:
            should_report = True
            agent_ids_to_check.append(agent_id)

    if not should_report:
        return checks
    if not agent_ids_to_check:
        agent_ids_to_check = configured_hermes_agents

    for agent_id in agent_ids_to_check:
        result = _run_hermes_runtime_preflight(hub_config, agent_id=agent_id)
        check_name = (
            "Hermes runtime availability"
            if agent_id == "hermes"
            else f"Hermes runtime availability ({agent_id})"
        )
        check_id = (
            "hub.hermes.binary"
            if agent_id == "hermes"
            else f"hub.hermes.binary.{agent_id}"
        )
        severity = (
            "info"
            if result.status == "ready"
            else ("error" if enabled_workspaces else "warning")
        )
        fix = result.fix
        message = result.message
        if workspace_suffix:
            message = f"{message.rstrip('.')}{workspace_suffix}."
        if result.status == "ready":
            detail_parts = []
            if result.version:
                detail_parts.append(result.version)
            if result.launch_mode:
                detail_parts.append(f"launch_mode={result.launch_mode}")
            suffix = f" ({', '.join(detail_parts)})" if detail_parts else ""
            checks.append(
                DoctorCheck(
                    name=check_name,
                    passed=True,
                    message=f"{message.rstrip('.')}{suffix}.",
                    severity="info",
                    check_id=check_id,
                )
            )
            continue

        checks.append(
            DoctorCheck(
                name=check_name,
                passed=False,
                message=message,
                severity=severity,
                check_id=check_id,
                fix=fix,
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
                    fix=f"Run 'car pma interrupt' for stuck lanes or check logs at {queue_dir}",
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

    def log_line(self, run_id: int, message: str) -> None:
        """Append a line to the legacy per-run log file (deprecated)."""
        run_log_path = self._run_log_path(run_id)
        run_log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = now_iso()
        with open(run_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")

    def _run_log_path(self, run_id: int) -> Path:
        """Get path to legacy run log file (deprecated)."""
        return self.state_root / "runs" / str(run_id) / "run.log"

    def read_run_block(self, run_id: int) -> Optional[str]:
        """Read a legacy run log block for a given run ID (deprecated)."""
        run_log_path = self._run_log_path(run_id)
        if not run_log_path.exists():
            return None
        try:
            with open(run_log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as e:
            _logger.warning("Failed to read run log block for run %s: %s", run_id, e)
            return None


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
    "zeroclaw_doctor_checks",
]
