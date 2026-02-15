from __future__ import annotations

from ...core.config import RepoConfig
from ...tickets import AgentPool
from .agent_pool_impl import DefaultAgentPool


def build_agent_pool(config: RepoConfig) -> AgentPool:
    return DefaultAgentPool(config)
