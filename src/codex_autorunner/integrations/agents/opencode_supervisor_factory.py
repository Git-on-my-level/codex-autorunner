from __future__ import annotations

import logging
from pathlib import Path
from typing import MutableMapping, Optional, cast

from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...core.config import RepoConfig
from ...core.destinations import DockerDestination
from ...core.utils import build_opencode_supervisor
from .destination_wrapping import (
    resolve_destination_from_config,
    wrap_command_for_destination,
)


def build_opencode_supervisor_from_repo_config(
    config: RepoConfig,
    *,
    workspace_root: Path,
    logger: logging.Logger,
    base_env: Optional[MutableMapping[str, str]] = None,
    command_override: Optional[list[str]] = None,
) -> Optional[OpenCodeSupervisor]:
    opencode_command = command_override or config.agent_serve_command("opencode")
    opencode_binary = None
    try:
        opencode_binary = config.agent_binary("opencode")
    except Exception:
        opencode_binary = None

    if command_override is None:
        destination = resolve_destination_from_config(
            getattr(config, "effective_destination", {"kind": "local"})
        )
        if isinstance(destination, DockerDestination):
            wrapped_source = list(opencode_command or [])
            if not wrapped_source and opencode_binary:
                wrapped_source = [
                    opencode_binary,
                    "serve",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    "0",
                ]
            if wrapped_source:
                wrapped = wrap_command_for_destination(
                    command=wrapped_source,
                    destination=destination,
                    repo_root=workspace_root,
                )
                opencode_command = wrapped.command

    agent_cfg = config.agents.get("opencode")
    subagent_models = agent_cfg.subagent_models if agent_cfg else None

    supervisor = build_opencode_supervisor(
        opencode_command=opencode_command,
        opencode_binary=opencode_binary,
        workspace_root=workspace_root,
        logger=logger,
        request_timeout=config.app_server.request_timeout,
        max_handles=config.opencode.max_handles,
        idle_ttl_seconds=config.opencode.idle_ttl_seconds,
        server_scope=config.opencode.server_scope,
        session_stall_timeout_seconds=config.opencode.session_stall_timeout_seconds,
        max_text_chars=config.opencode.max_text_chars,
        base_env=base_env,
        subagent_models=subagent_models,
    )
    return cast(Optional[OpenCodeSupervisor], supervisor)
