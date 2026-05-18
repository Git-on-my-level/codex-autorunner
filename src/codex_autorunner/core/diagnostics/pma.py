from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from ..config import HubConfig, RepoConfig
from ..state_roots import REPO_STATE_DIR
from .agent_registry import (
    get_agent_descriptor as _get_agent_descriptor,
)
from .agent_registry import (
    get_registered_agents as _get_registered_agents,
)
from .agent_registry import (
    run_agent_runtime_preflight as _run_agent_runtime_preflight,
)
from .agent_registry import (
    validate_agent_id as _validate_agent_id,
)
from .types import DoctorCheck

PMA_STATE_FILE = f"{REPO_STATE_DIR}/pma/state.json"
PMA_QUEUE_DIR = f"{REPO_STATE_DIR}/pma/queue"
STUCK_LANE_THRESHOLD_MINUTES = 60


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

    default_agent = (
        str(pma_cfg.get("default_agent", "codex") or "codex").strip().lower()
    )
    configured_agents = _configured_agent_ids(config)
    try:
        if not isinstance(config, dict):
            default_agent = _validate_agent_id(default_agent, config)
            default_agent_valid = True
        else:
            default_agent_valid = default_agent in configured_agents
    except ValueError:
        default_agent_valid = False
    if not default_agent_valid:
        available = ", ".join(sorted(configured_agents))
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
        if not isinstance(config, dict):
            descriptor = _get_agent_descriptor(default_agent, config)
            if descriptor is not None and descriptor.runtime_preflight is not None:
                result = _run_agent_runtime_preflight(default_agent, context=config)
                status = str(getattr(result, "status", "") or "").strip().lower()
                checks.append(
                    DoctorCheck(
                        name="PMA default agent readiness",
                        passed=status in {"", "ready", "deferred"},
                        message=str(
                            getattr(result, "message", None)
                            or f"{default_agent} runtime ready."
                        ),
                        check_id="pma.default_agent.runtime",
                        severity=(
                            "info" if status in {"", "ready", "deferred"} else "error"
                        ),
                        fix=getattr(result, "fix", None),
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


def _configured_agent_ids(
    config: Union[HubConfig, RepoConfig, dict[str, Any]],
) -> set[str]:
    if not isinstance(config, dict):
        return set(_get_registered_agents(config))
    agents = getattr(config, "agents", None)
    if isinstance(agents, dict):
        return {
            str(agent_id).strip().lower()
            for agent_id in agents
            if str(agent_id).strip()
        }
    if isinstance(config, dict):
        raw_agents = config.get("agents")
        if isinstance(raw_agents, dict):
            return {
                str(agent_id).strip().lower()
                for agent_id in raw_agents
                if str(agent_id).strip()
            }
    return {"codex", "opencode", "hermes"}


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


__all__ = ["pma_doctor_checks"]
