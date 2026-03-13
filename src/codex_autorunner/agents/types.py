from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, NewType

# When adding agents, update core/config.py agents defaults + validation (config-driven).
AgentId = NewType("AgentId", str)
RuntimeCapability = NewType("RuntimeCapability", str)

_RUNTIME_CAPABILITY_ALIASES = {
    "threads": "durable_threads",
    "turns": "message_turns",
}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    display_name: str
    supports_reasoning: bool
    reasoning_options: list[str]


@dataclass(frozen=True)
class ModelCatalog:
    default_model: str
    models: list[ModelSpec]


@dataclass(frozen=True)
class ConversationRef:
    """Runtime-native durable conversation/session handle."""

    agent: AgentId
    id: str


@dataclass(frozen=True)
class TurnRef:
    """Runtime-native execution handle within a conversation/session."""

    conversation_id: str
    turn_id: str


@dataclass(frozen=True)
class RuntimeCapabilityReport:
    """Optional runtime-reported capability supplement for one agent."""

    capabilities: frozenset[RuntimeCapability] = field(default_factory=frozenset)


def normalize_runtime_capabilities(
    capabilities: Iterable[str],
) -> frozenset[RuntimeCapability]:
    normalized: set[RuntimeCapability] = set()
    for capability in capabilities:
        text = str(capability or "").strip().lower()
        if not text:
            continue
        text = _RUNTIME_CAPABILITY_ALIASES.get(text, text)
        normalized.add(RuntimeCapability(text))
    return frozenset(normalized)


__all__ = [
    "AgentId",
    "ConversationRef",
    "ModelCatalog",
    "ModelSpec",
    "RuntimeCapability",
    "RuntimeCapabilityReport",
    "TurnRef",
    "normalize_runtime_capabilities",
]
