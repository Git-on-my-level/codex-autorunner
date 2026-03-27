from __future__ import annotations

import logging
from typing import Optional

from ...core.config import HubConfig, RepoConfig
from ...core.utils import resolve_executable
from ..managed_runtime import RuntimeLaunchMode, RuntimePreflightResult

_logger = logging.getLogger(__name__)

HERMES_RUNTIME_ID = "hermes"
HERMES_ACP_COMMAND = "acp"
HERMES_SESSION_STATE_FILE_MODE: RuntimeLaunchMode = "session_state_file"


class HermesSupervisorError(RuntimeError):
    pass


class HermesSessionHandle:
    pass


class HermesSupervisor:
    pass


def build_hermes_supervisor_from_config(
    config: RepoConfig | HubConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[HermesSupervisor]:
    _ = config, logger
    return None


def hermes_binary_available(config: Optional[RepoConfig | HubConfig]) -> bool:
    if config is None:
        return False
    try:
        binary = config.agent_binary("hermes").strip()
    except Exception:
        return False
    if not binary:
        return False
    return resolve_executable(binary) is not None


def hermes_runtime_preflight(
    config: Optional[RepoConfig | HubConfig],
) -> RuntimePreflightResult:
    if config is None:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix="Set agents.hermes.binary in the repo or hub config.",
        )
    try:
        binary = config.agent_binary("hermes").strip()
    except Exception:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix="Set agents.hermes.binary in the repo or hub config.",
        )
    if not binary:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix="Set agents.hermes.binary in the repo or hub config.",
        )
    binary_path = resolve_executable(binary)
    if binary_path is None:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message=f"Hermes binary '{binary}' is not available on PATH.",
            fix="Install Hermes or update agents.hermes.binary to a working executable path.",
        )
    import subprocess

    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        version = None
    try:
        result = subprocess.run(
            [binary, HERMES_ACP_COMMAND, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode not in (0, 1) or not result.stdout:
            return RuntimePreflightResult(
                runtime_id=HERMES_RUNTIME_ID,
                status="incompatible",
                version=version,
                launch_mode=None,
                message="Hermes ACP mode is not supported by this binary.",
                fix="Install a Hermes build that supports the `hermes acp` command.",
            )
        help_text = result.stdout + result.stderr
        if "--session-state-file" not in help_text:
            return RuntimePreflightResult(
                runtime_id=HERMES_RUNTIME_ID,
                status="incompatible",
                version=version,
                launch_mode=None,
                message="Hermes does not advertise --session-state-file support in `hermes acp --help`.",
                fix="Install a Hermes build that supports durable session state file launches.",
            )
    except Exception as exc:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="incompatible",
            version=version,
            launch_mode=None,
            message=f"Failed to probe Hermes ACP support: {exc}",
            fix="Ensure Hermes binary is executable and supports `hermes acp` command.",
        )
    return RuntimePreflightResult(
        runtime_id=HERMES_RUNTIME_ID,
        status="ready",
        version=version,
        launch_mode=HERMES_SESSION_STATE_FILE_MODE,
        message=f"Hermes {version or 'version unknown'} supports durable ACP sessions.",
    )


__all__ = [
    "HERMES_ACP_COMMAND",
    "HERMES_RUNTIME_ID",
    "HERMES_SESSION_STATE_FILE_MODE",
    "HermesSessionHandle",
    "HermesSupervisor",
    "HermesSupervisorError",
    "build_hermes_supervisor_from_config",
    "hermes_binary_available",
    "hermes_runtime_preflight",
]
