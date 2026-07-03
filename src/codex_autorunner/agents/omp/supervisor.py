from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

from ...core.config import HubConfig, RepoConfig
from ...core.utils import resolve_executable
from ..acp.client import build_missing_session_matcher
from ..acp.runtime_supervisor import (
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ACPRuntimeSupervisor,
    ACPRuntimeSupervisorError,
    ACPSessionHandle,
    ApprovalHandler,
    RuntimePreflightResult,
)

_logger = logging.getLogger(__name__)

OMP_RUNTIME_ID = "omp"
OMP_ACP_COMMAND = "acp"
OMP_APPROVAL_TIMEOUT_SECONDS = DEFAULT_APPROVAL_TIMEOUT_SECONDS


class OMPSupervisorError(ACPRuntimeSupervisorError):
    """OMP-specific ACP runtime supervisor error."""


# Backward-compatible alias; OMP sessions are plain ACP session handles.
OMPSessionHandle = ACPSessionHandle


def _omp_missing_session_matcher():
    # OMP surfaces a missing session as -32603 + "session not found" rather than
    # the canonical -32004. Match on the descriptive data so a genuine internal
    # error (-32603 without that signature) is never misclassified.
    return build_missing_session_matcher(data_contains="session not found")


class OMPSupervisor(ACPRuntimeSupervisor):
    """OMP (Oh My Pi) ACP runtime supervisor.

    A thin specialization of :class:`ACPRuntimeSupervisor`. The generic ACP turn
    lifecycle, event buffering, and approval flow are inherited; this class binds
    the OMP runtime identity, wires the G1 missing-session matcher, and keeps
    OMP-native durable sessions under OMP's own store (``~/.omp/agent``), scoped
    naturally by the workspace ``cwd`` passed to ``session/new``.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        base_env: Optional[Mapping[str, str]] = None,
        initialize_params: Optional[dict[str, Any]] = None,
        request_timeout: Optional[float] = None,
        approval_handler: Optional[ApprovalHandler] = None,
        default_approval_decision: str = "cancel",
        approval_timeout_seconds: float = OMP_APPROVAL_TIMEOUT_SECONDS,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__(
            command,
            runtime_id=OMP_RUNTIME_ID,
            runtime_label="OMP",
            error_cls=OMPSupervisorError,
            base_env=dict(base_env or {}),
            initialize_params=initialize_params,
            request_timeout=request_timeout,
            missing_session_matcher=_omp_missing_session_matcher(),
            approval_handler=approval_handler,
            default_approval_decision=default_approval_decision,
            approval_timeout_seconds=approval_timeout_seconds,
            logger=logger,
        )


def _configured_omp_binary(
    config: RepoConfig | HubConfig,
    *,
    agent_id: str = OMP_RUNTIME_ID,
) -> Optional[str]:
    try:
        try:
            return config.agent_binary(agent_id).strip()
        except TypeError as exc:
            if "profile" not in str(exc):
                raise
            return config.agent_binary(agent_id, profile=None).strip()
    except (
        KeyError,
        AttributeError,
        ValueError,
        TypeError,
        RuntimeError,
    ):  # intentional: config lookup may raise various errors
        return None


def _resolve_omp_launch(
    config: RepoConfig | HubConfig,
    *,
    agent_id: str = OMP_RUNTIME_ID,
) -> tuple[list[str], str]:
    configured_binary = _configured_omp_binary(config, agent_id=agent_id)
    if not configured_binary:
        raise KeyError(agent_id)
    return [configured_binary, OMP_ACP_COMMAND], configured_binary


def build_omp_supervisor_from_config(
    config: RepoConfig | HubConfig,
    *,
    agent_id: str = OMP_RUNTIME_ID,
    approval_handler: Optional[ApprovalHandler] = None,
    default_approval_decision: str = "cancel",
    logger: Optional[logging.Logger] = None,
) -> Optional[OMPSupervisor]:
    try:
        command, _binary = _resolve_omp_launch(config, agent_id=agent_id)
    except (
        KeyError,
        AttributeError,
        ValueError,
        TypeError,
        RuntimeError,
    ):  # intentional: config lookup may raise various errors
        return None
    return OMPSupervisor(
        command,
        approval_handler=approval_handler,
        default_approval_decision=default_approval_decision,
        logger=logger,
    )


def omp_binary_available(
    config: Optional[RepoConfig | HubConfig],
    *,
    agent_id: str = OMP_RUNTIME_ID,
) -> bool:
    if config is None:
        return False
    try:
        _command, binary = _resolve_omp_launch(config, agent_id=agent_id)
    except (KeyError, AttributeError, ValueError, TypeError, RuntimeError):
        return False
    if not binary:
        return False
    return resolve_executable(binary) is not None


def omp_runtime_preflight(
    config: Optional[RepoConfig | HubConfig],
    *,
    agent_id: str = OMP_RUNTIME_ID,
) -> RuntimePreflightResult:
    normalized_agent_id = str(agent_id or "").strip().lower() or OMP_RUNTIME_ID
    binary_key = f"agents.{normalized_agent_id}.binary"
    if config is None:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="OMP binary is not configured.",
            fix=f"Set {binary_key} in the repo or hub config.",
        )
    try:
        command, binary = _resolve_omp_launch(config, agent_id=normalized_agent_id)
    except (
        KeyError,
        AttributeError,
        ValueError,
        TypeError,
        RuntimeError,
    ):  # intentional: config lookup may raise various errors
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="OMP binary is not configured.",
            fix=f"Set {binary_key} in the repo or hub config.",
        )
    if not binary:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="OMP binary is not configured.",
            fix=f"Set {binary_key} in the repo or hub config.",
        )
    binary_path = resolve_executable(binary)
    if binary_path is None:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message=f"OMP binary '{binary}' is not available on PATH.",
            fix=f"Install OMP or update {binary_key} to a working executable path.",
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
    except (OSError, subprocess.TimeoutExpired):
        version = None
    try:
        result = subprocess.run(
            [*command, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        help_text = result.stdout + result.stderr
        if result.returncode not in (0, 1) or not help_text.strip():
            return RuntimePreflightResult(
                runtime_id=normalized_agent_id,
                status="incompatible",
                version=version,
                launch_mode=None,
                message="OMP ACP mode is not supported by this binary.",
                fix="Install an OMP build that supports the `omp acp` command.",
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="incompatible",
            version=version,
            launch_mode=None,
            message=f"Failed to probe OMP ACP support: {exc}",
            fix="Ensure OMP binary is executable and supports `omp acp` command.",
        )
    return RuntimePreflightResult(
        runtime_id=normalized_agent_id,
        status="ready",
        version=version,
        launch_mode=None,
        message=(
            f"OMP {version or 'version unknown'} supports ACP mode and uses "
            "OMP-native durable sessions."
        ),
        fix="",
    )


__all__ = [
    "OMP_ACP_COMMAND",
    "OMP_RUNTIME_ID",
    "OMPSessionHandle",
    "OMPSupervisor",
    "OMPSupervisorError",
    "build_omp_supervisor_from_config",
    "omp_binary_available",
    "omp_runtime_preflight",
]
