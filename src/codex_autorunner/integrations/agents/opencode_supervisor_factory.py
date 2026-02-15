from __future__ import annotations

import logging
from pathlib import Path
from typing import MutableMapping, Optional, cast

from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...core.config import RepoConfig
from ...core.utils import build_opencode_supervisor


def build_opencode_supervisor_from_repo_config(
    config: RepoConfig,
    *,
    workspace_root: Path,
    logger: logging.Logger,
    base_env: Optional[MutableMapping[str, str]] = None,
) -> Optional[OpenCodeSupervisor]:
    opencode_command = config.agent_serve_command("opencode")
    opencode_binary = None
    try:
        opencode_binary = config.agent_binary("opencode")
    except Exception:
        opencode_binary = None

    agent_cfg = config.agents.get("opencode")
    subagent_models = agent_cfg.subagent_models if agent_cfg else None

    supervisor = build_opencode_supervisor(
        opencode_command=opencode_command,
        opencode_binary=opencode_binary,
        workspace_root=workspace_root,
        logger=logger,
        request_timeout=config.app_server.request_timeout,
        max_handles=config.app_server.max_handles,
        idle_ttl_seconds=config.app_server.idle_ttl_seconds,
        server_scope=config.opencode.server_scope,
        session_stall_timeout_seconds=config.opencode.session_stall_timeout_seconds,
        max_text_chars=config.opencode.max_text_chars,
        base_env=base_env,
        subagent_models=subagent_models,
    )
    return cast(Optional[OpenCodeSupervisor], supervisor)
