from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Optional, Sequence

from ...core.destinations import (
    Destination,
    DockerDestination,
    LocalDestination,
    parse_destination_config,
)
from ...core.utils import is_within
from ...workspace import workspace_id_for_path
from ..docker.runtime import DockerRuntime, build_docker_container_spec

logger = logging.getLogger("codex_autorunner.integrations.agents.destination_wrapping")


@dataclasses.dataclass(frozen=True)
class WrappedCommand:
    command: list[str]
    state_root_override: Optional[Path] = None


def resolve_destination_from_config(raw_destination: object) -> Destination:
    parsed = parse_destination_config(raw_destination, context="effective destination")
    if not parsed.valid:
        for err in parsed.errors:
            logger.warning("Invalid effective destination config; using local: %s", err)
    return parsed.destination


def _default_container_name(repo_root: Path) -> str:
    return f"car-ws-{workspace_id_for_path(repo_root)}"


def wrap_command_for_destination(
    *,
    command: Sequence[str],
    destination: Destination,
    repo_root: Path,
    docker_runtime: Optional[DockerRuntime] = None,
) -> WrappedCommand:
    if not command:
        raise ValueError("command must not be empty")

    if isinstance(destination, LocalDestination):
        return WrappedCommand(command=[str(part) for part in command])
    if not isinstance(destination, DockerDestination):
        return WrappedCommand(command=[str(part) for part in command])

    runtime = docker_runtime or DockerRuntime()
    repo_abs = repo_root.resolve()
    container_name = destination.container_name or _default_container_name(repo_abs)

    spec = build_docker_container_spec(
        name=container_name,
        image=destination.image,
        repo_root=repo_abs,
        mounts=destination.mounts,
        env_passthrough_patterns=destination.env_passthrough,
        explicit_env=(
            destination.extra.get("env")
            if isinstance(destination.extra, dict)
            else None
        ),
        workdir=destination.workdir,
    )
    runtime.ensure_container_running(spec)
    wrapped = runtime.build_exec_command(
        container_name,
        command,
        workdir=spec.workdir,
        env=spec.env,
    )

    # Docker destination runs inside repo mount, so keep supervisor state under repo state root.
    state_root = repo_abs / ".codex-autorunner" / "app_server_workspaces"
    if not is_within(repo_abs, state_root):
        state_root = repo_abs / ".codex-autorunner" / "app_server_workspaces"
    return WrappedCommand(command=wrapped, state_root_override=state_root)


__all__ = [
    "WrappedCommand",
    "resolve_destination_from_config",
    "wrap_command_for_destination",
]
