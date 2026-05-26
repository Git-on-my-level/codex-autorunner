from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NewType, Optional

from ..runtime_capabilities import (
    RuntimeCapability,
    normalize_runtime_capabilities,
)

# When adding agents, update core/config.py agents defaults + validation (config-driven).
AgentId = NewType("AgentId", str)


@dataclass(frozen=True)
class ModelSpec:
    id: str
    display_name: str
    supports_reasoning: bool
    reasoning_options: list[str]

    def __post_init__(self) -> None:
        options = [
            option.strip()
            for option in self.reasoning_options
            if isinstance(option, str) and option.strip()
        ]
        if not self.supports_reasoning:
            options = []
        object.__setattr__(self, "reasoning_options", list(dict.fromkeys(options)))


@dataclass(frozen=True)
class ModelCatalog:
    default_model: str
    models: list[ModelSpec]


@dataclass(frozen=True)
class ConversationRef:
    """Runtime-native durable conversation/session handle."""

    agent: AgentId
    id: str
    title: Optional[str] = None
    summary: Optional[str] = None


@dataclass(frozen=True)
class TurnRef:
    """Runtime-native execution handle within a conversation/session."""

    conversation_id: str
    turn_id: str


@dataclass(frozen=True)
class TerminalTurnResult:
    """Plain-text terminal outcome returned by a durable runtime turn helper."""

    status: Optional[str]
    assistant_text: str
    errors: list[str] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    effective_runtime: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class TranscriptEntry:
    """Plain-text transcript row mirrored out of a runtime-owned history surface."""

    role: str
    text: str
    turn_id: Optional[str] = None
    created_at: Optional[str] = None


@dataclass(frozen=True)
class RuntimeCapabilityReport:
    """Optional runtime-reported capability supplement for one agent."""

    capabilities: frozenset[RuntimeCapability] = field(default_factory=frozenset)


__all__ = [
    "AgentId",
    "ConversationRef",
    "ModelCatalog",
    "ModelSpec",
    "RuntimeCapability",
    "RuntimeCapabilityReport",
    "TerminalTurnResult",
    "TranscriptEntry",
    "TurnRef",
    "normalize_runtime_capabilities",
]
