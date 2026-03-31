"""Shared agent catalog and agent-switch policy for chat surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Optional, Tuple

DEFAULT_CHAT_AGENT = "codex"
DEFAULT_CHAT_AGENT_MODELS = MappingProxyType(
    {
        "codex": "gpt-5.4",
        "opencode": "zai-coding-plan/glm-5.1",
    }
)

AgentModelResetMode = Literal["clear", "agent_default"]
CHAT_EFFORT_CAPABILITY = "review"

VALID_CHAT_AGENT_VALUES: Tuple[str, ...] = ("codex", "opencode", "hermes", "zeroclaw")


@dataclass(frozen=True)
class ChatAgentDefinition:
    value: str
    description: str


CHAT_AGENT_DEFINITIONS: tuple[ChatAgentDefinition, ...] = (
    ChatAgentDefinition(value="codex", description="Codex"),
    ChatAgentDefinition(value="opencode", description="OpenCode"),
    ChatAgentDefinition(value="hermes", description="Hermes"),
    ChatAgentDefinition(value="zeroclaw", description="ZeroClaw"),
)


@dataclass(frozen=True)
class ChatAgentSwitchState:
    """Normalized runtime state that should apply after an agent switch."""

    agent: str
    model: Optional[str]
    effort: Optional[str]


def _valid_chat_agent_values() -> tuple[str, ...]:
    return tuple(definition.value for definition in chat_agent_definitions())


def chat_agent_definitions() -> tuple[ChatAgentDefinition, ...]:
    ordered: list[ChatAgentDefinition] = []
    seen: set[str] = set()

    try:
        from ...agents.registry import get_registered_agents

        registered = get_registered_agents()
    except Exception:
        registered = {}

    for definition in CHAT_AGENT_DEFINITIONS:
        descriptor = registered.get(definition.value)
        ordered.append(
            ChatAgentDefinition(
                value=definition.value,
                description=(
                    descriptor.name
                    if descriptor is not None
                    else definition.description
                ),
            )
        )
        seen.add(definition.value)

    for agent_id in sorted(registered):
        if agent_id in seen:
            continue
        descriptor = registered[agent_id]
        ordered.append(
            ChatAgentDefinition(
                value=agent_id,
                description=descriptor.name,
            )
        )
        seen.add(agent_id)

    return tuple(ordered)


def valid_chat_agent_values() -> tuple[str, ...]:
    return _valid_chat_agent_values()


def normalize_chat_agent(
    value: object, *, default: Optional[str] = None
) -> Optional[str]:
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower()
    compact = "".join(ch for ch in normalized if ch.isalnum())
    valid_values = _valid_chat_agent_values()
    if normalized in valid_values:
        return normalized
    if compact in valid_values:
        return compact
    return default


def chat_agent_supports_effort(agent: object) -> bool:
    normalized = normalize_chat_agent(agent, default=DEFAULT_CHAT_AGENT)
    if normalized is None:
        return False
    try:
        from ...agents.registry import get_agent_descriptor

        descriptor = get_agent_descriptor(normalized)
    except Exception:
        descriptor = None
    if descriptor is None:
        return normalized == "codex"
    return CHAT_EFFORT_CAPABILITY in descriptor.capabilities


def default_chat_model_for_agent(agent: object) -> Optional[str]:
    normalized = normalize_chat_agent(agent, default=DEFAULT_CHAT_AGENT)
    if normalized is None:
        return None
    return DEFAULT_CHAT_AGENT_MODELS.get(normalized)


def chat_agent_command_choices() -> tuple[dict[str, str], ...]:
    return tuple(
        {"name": definition.value, "value": definition.value}
        for definition in chat_agent_definitions()
    )


def chat_agent_description() -> str:
    return " or ".join(sorted(_valid_chat_agent_values()))


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
