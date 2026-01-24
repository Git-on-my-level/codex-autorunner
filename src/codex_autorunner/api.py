"""Stable public API for Codex Autorunner plugins.

Everything else in the codebase should be treated as internal unless documented.
"""

from __future__ import annotations

from .agents.base import AgentHarness
from .agents.registry import AgentCapability, AgentDescriptor, reload_agents
from .agents.types import AgentId, ConversationRef, ModelCatalog, ModelSpec, TurnRef
from .plugin_api import CAR_AGENT_ENTRYPOINT_GROUP, CAR_PLUGIN_API_VERSION

__all__ = [
    "AgentCapability",
    "AgentDescriptor",
    "AgentHarness",
    "AgentId",
    "ConversationRef",
    "ModelCatalog",
    "ModelSpec",
    "TurnRef",
    "CAR_AGENT_ENTRYPOINT_GROUP",
    "CAR_PLUGIN_API_VERSION",
    "reload_agents",
]
