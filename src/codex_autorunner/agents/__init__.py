"""Agent harness abstractions."""

from .hermes_identity import (
    CanonicalHermesIdentity,
    canonicalize_hermes_identity,
    is_hermes_alias_agent,
)
from .registry import (
    AgentCapability,
    AgentDescriptor,
    get_agent_descriptor,
    get_available_agents,
    get_registered_agents,
    has_capability,
    validate_agent_id,
)
from .types import RuntimeCapability

__all__ = [
    "AgentCapability",
    "CanonicalHermesIdentity",
    "AgentDescriptor",
    "RuntimeCapability",
    "canonicalize_hermes_identity",
    "get_agent_descriptor",
    "get_available_agents",
    "get_registered_agents",
    "has_capability",
    "is_hermes_alias_agent",
    "validate_agent_id",
]
