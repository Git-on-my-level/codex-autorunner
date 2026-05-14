from __future__ import annotations

from typing import Any, Optional

from ...core.config import RepoConfig
from ...tickets import AgentPool
from .agent_pool_impl import DefaultAgentPool


def build_agent_pool(
    config: RepoConfig,
    *,
    runtime_services: Optional[Any] = None,
) -> AgentPool:
    return DefaultAgentPool(config, runtime_services=runtime_services)
