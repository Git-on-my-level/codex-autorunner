from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from os.path import basename
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ...core.config import HubConfig, RepoConfig
from ...core.text_utils import _normalize_optional_text
from ...core.utils import resolve_executable
from ..acp.runtime_supervisor import (
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ACPRuntimeSupervisor,
    ACPRuntimeSupervisorError,
    ACPSessionHandle,
    ApprovalHandler,
)

_logger = logging.getLogger(__name__)

HERMES_RUNTIME_ID = "hermes"
HERMES_ACP_COMMAND = "acp"
HERMES_APPROVAL_TIMEOUT_SECONDS = DEFAULT_APPROVAL_TIMEOUT_SECONDS


@dataclass(frozen=True)
class RuntimePreflightResult:
    runtime_id: str
    status: str
    version: Optional[str]
    launch_mode: Optional[str]
    message: str
    fix: str


def _prepend_path_entries(entries: Sequence[str], path: str) -> str:
    merged: list[str] = []
    for value in entries:
        if value and value not in merged:
            merged.append(value)
    for value in path.split(os.pathsep):
        if value and value not in merged:
            merged.append(value)
    return os.pathsep.join(merged)


def _hermes_launch_path_entries(command: Sequence[str]) -> list[str]:
    if not command:
        return []
    binary = str(command[0] or "").strip()
    if not binary:
        return []
    resolved = resolve_executable(binary)
    candidate: Optional[Path] = Path(resolved) if resolved else None
    if candidate is None:
        raw_candidate = Path(binary).expanduser()
        if raw_candidate.is_absolute() and raw_candidate.exists():
            candidate = raw_candidate
    if candidate is None or not candidate.exists():
        return []
    return [str(candidate.parent)]


def _build_hermes_base_env(
    command: Sequence[str],
    *,
    base_env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    extra_paths = _hermes_launch_path_entries(command)
    if not base_env and not extra_paths:
        return {}
    env = os.environ.copy()
    if base_env:
        env.update({str(key): str(value) for key, value in base_env.items()})
    if extra_paths:
        env["PATH"] = _prepend_path_entries(extra_paths, env.get("PATH", ""))
    return env


class HermesSupervisorError(ACPRuntimeSupervisorError):
    """Hermes-specific ACP runtime supervisor error (backward compatible)."""


# Backward-compatible alias; Hermes sessions are plain ACP session handles.
HermesSessionHandle = ACPSessionHandle


class HermesSupervisor(ACPRuntimeSupervisor):
    """Hermes ACP runtime supervisor.

    A thin specialization of :class:`ACPRuntimeSupervisor`. The generic ACP
    turn lifecycle, event buffering, and approval flow are inherited; this
    class adds Hermes profile-PATH env building and binds Hermes' error type
    and display label. Hermes-native durable session storage remains Hermes'
    responsibility under the shared ``HERMES_HOME``.
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
        approval_timeout_seconds: float = HERMES_APPROVAL_TIMEOUT_SECONDS,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__(
            command,
            runtime_id=HERMES_RUNTIME_ID,
            runtime_label="Hermes",
            error_cls=HermesSupervisorError,
            base_env=_build_hermes_base_env(command, base_env=base_env),
            initialize_params=initialize_params,
            request_timeout=request_timeout,
            approval_handler=approval_handler,
            default_approval_decision=default_approval_decision,
            approval_timeout_seconds=approval_timeout_seconds,
            logger=logger,
        )


def _configured_hermes_binary(
    config: RepoConfig | HubConfig,
    *,
    agent_id: str,
    profile: Optional[str] = None,
) -> Optional[str]:
    try:
        try:
            return config.agent_binary(agent_id, profile=profile).strip()
        except TypeError as exc:
            if "profile" not in str(exc):
                raise
            return config.agent_binary(agent_id).strip()
    except (
        KeyError,
        AttributeError,
        ValueError,
        TypeError,
        RuntimeError,
    ):  # intentional: config lookup may raise various errors
        return None


def _resolve_hermes_launch(
    config: RepoConfig | HubConfig,
    *,
    agent_id: str,
    profile: Optional[str] = None,
) -> tuple[list[str], str]:
    normalized_agent_id = str(agent_id or "").strip().lower() or HERMES_RUNTIME_ID
    normalized_profile = _normalize_optional_text(profile)
    configured_binary = _configured_hermes_binary(
        config,
        agent_id=normalized_agent_id,
        profile=normalized_profile,
    )
    configured_name = basename(configured_binary) if configured_binary else ""
    if (
        normalized_agent_id == HERMES_RUNTIME_ID
        and normalized_profile is not None
        and configured_binary
    ):
        base_binary = _configured_hermes_binary(config, agent_id=HERMES_RUNTIME_ID)
        if base_binary and configured_binary == base_binary:
            return [
                configured_binary,
                "-p",
                normalized_profile,
                HERMES_ACP_COMMAND,
            ], configured_binary
    if (
        normalized_agent_id != HERMES_RUNTIME_ID
        and normalized_profile is None
        and configured_name == normalized_agent_id
    ):
        base_binary = _configured_hermes_binary(config, agent_id=HERMES_RUNTIME_ID)
        if base_binary:
            return [
                base_binary,
                "-p",
                normalized_agent_id,
                HERMES_ACP_COMMAND,
            ], base_binary
    if configured_binary:
        return [configured_binary, HERMES_ACP_COMMAND], configured_binary
    raise KeyError(normalized_agent_id)


def build_hermes_supervisor_from_config(
    config: RepoConfig | HubConfig,
    *,
    agent_id: str = "hermes",
    profile: Optional[str] = None,
    approval_handler: Optional[ApprovalHandler] = None,
    default_approval_decision: str = "cancel",
    logger: Optional[logging.Logger] = None,
) -> Optional[HermesSupervisor]:
    try:
        command, _binary = _resolve_hermes_launch(
            config,
            agent_id=agent_id,
            profile=profile,
        )
    except (
        KeyError,
        AttributeError,
        ValueError,
        TypeError,
        RuntimeError,
    ):  # intentional: config lookup may raise various errors
        return None
    return HermesSupervisor(
        command,
        approval_handler=approval_handler,
        default_approval_decision=default_approval_decision,
        logger=logger,
    )


def hermes_binary_available(
    config: Optional[RepoConfig | HubConfig],
    *,
    agent_id: str = "hermes",
    profile: Optional[str] = None,
) -> bool:
    if config is None:
        return False
    try:
        _command, binary = _resolve_hermes_launch(
            config,
            agent_id=agent_id,
            profile=profile,
        )
    except (KeyError, AttributeError, ValueError, TypeError, RuntimeError):
        return False
    if not binary:
        return False
    return resolve_executable(binary) is not None


def hermes_runtime_preflight(
    config: Optional[RepoConfig | HubConfig],
    *,
    agent_id: str = "hermes",
    profile: Optional[str] = None,
) -> RuntimePreflightResult:
    normalized_agent_id = str(agent_id or "").strip().lower() or HERMES_RUNTIME_ID
    normalized_profile = str(profile or "").strip().lower()
    binary_key = (
        f"agents.{normalized_agent_id}.profiles.{normalized_profile}.binary"
        if normalized_profile
        else f"agents.{normalized_agent_id}.binary"
    )
    if config is None:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix=f"Set {binary_key} in the repo or hub config.",
        )
    try:
        command, binary = _resolve_hermes_launch(
            config,
            agent_id=normalized_agent_id,
            profile=normalized_profile or None,
        )
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
            message="Hermes binary is not configured.",
            fix=f"Set {binary_key} in the repo or hub config.",
        )
    if not binary:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix=f"Set {binary_key} in the repo or hub config.",
        )
    binary_path = resolve_executable(binary)
    if binary_path is None:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message=f"Hermes binary '{binary}' is not available on PATH.",
            fix=f"Install Hermes or update {binary_key} to a working executable path.",
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
                message="Hermes ACP mode is not supported by this binary.",
                fix="Install a Hermes build that supports the `hermes acp` command.",
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RuntimePreflightResult(
            runtime_id=normalized_agent_id,
            status="incompatible",
            version=version,
            launch_mode=None,
            message=f"Failed to probe Hermes ACP support: {exc}",
            fix="Ensure Hermes binary is executable and supports `hermes acp` command.",
        )
    return RuntimePreflightResult(
        runtime_id=normalized_agent_id,
        status="ready",
        version=version,
        launch_mode=None,
        message=(
            f"Hermes {version or 'version unknown'} supports ACP mode and "
            "uses Hermes-native durable sessions."
        ),
        fix="",
    )


__all__ = [
    "HERMES_ACP_COMMAND",
    "HERMES_RUNTIME_ID",
    "HermesSessionHandle",
    "HermesSupervisor",
    "HermesSupervisorError",
    "RuntimePreflightResult",
    "build_hermes_supervisor_from_config",
    "hermes_binary_available",
    "hermes_runtime_preflight",
]
