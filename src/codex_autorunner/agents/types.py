from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AgentId = Literal["codex", "opencode"]


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
    agent: AgentId
    id: str


@dataclass(frozen=True)
class TurnRef:
    conversation_id: str
    turn_id: str


__all__ = [
    "AgentId",
    "ConversationRef",
    "ModelCatalog",
    "ModelSpec",
    "TurnRef",
]
