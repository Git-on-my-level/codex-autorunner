"""Shared agent catalog and agent-switch policy for chat surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Optional

DEFAULT_CHAT_AGENT = "codex"
VALID_CHAT_AGENT_VALUES = ("codex", "opencode")
DEFAULT_CHAT_AGENT_MODELS = MappingProxyType(
    {
        "codex": "gpt-5.3-codex",
        "opencode": "zai-coding-plan/glm-5",
    }
)

AgentModelResetMode = Literal["clear", "agent_default"]


@dataclass(frozen=True)
class ChatAgentSwitchState:
    """Normalized runtime state that should apply after an agent switch."""

    agent: str
    model: Optional[str]
    effort: Optional[str]


def normalize_chat_agent(
    value: object, *, default: Optional[str] = None
) -> Optional[str]:
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower()
    compact = "".join(ch for ch in normalized if ch.isalnum())
    if normalized in VALID_CHAT_AGENT_VALUES:
        return normalized
    if compact in VALID_CHAT_AGENT_VALUES:
        return compact
    return default


def chat_agent_supports_effort(agent: object) -> bool:
    return normalize_chat_agent(agent, default=DEFAULT_CHAT_AGENT) == "codex"


def default_chat_model_for_agent(agent: object) -> Optional[str]:
    normalized = normalize_chat_agent(agent, default=DEFAULT_CHAT_AGENT)
    if normalized is None:
        return None
    return DEFAULT_CHAT_AGENT_MODELS.get(normalized)


def build_agent_switch_state(
    agent: object, *, model_reset: AgentModelResetMode
) -> ChatAgentSwitchState:
    normalized = normalize_chat_agent(agent, default=DEFAULT_CHAT_AGENT)
    if normalized is None:
        normalized = DEFAULT_CHAT_AGENT
    model = (
        default_chat_model_for_agent(normalized)
        if model_reset == "agent_default"
        else None
    )
    return ChatAgentSwitchState(
        agent=normalized,
        model=model,
        effort=None,
    )
