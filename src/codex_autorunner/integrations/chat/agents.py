"""Shared agent catalog and agent-switch policy for chat surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Optional, Tuple

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


@dataclass(frozen=True)
class ChatAgentProfileOption:
    agent: str
    profile: str
    runtime_agent: str
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
    profile: Optional[str]
    model: Optional[str]
    effort: Optional[str]


def _registered_agents(context: Any = None) -> dict[str, Any]:
    try:
        from ...agents.registry import get_registered_agents

        return get_registered_agents(context)
    except Exception:
        return {}


def _agent_runtime_kind(agent_id: str, descriptor: Any) -> str:
    raw_runtime_kind = getattr(descriptor, "runtime_kind", None)
    if isinstance(raw_runtime_kind, str) and raw_runtime_kind.strip():
        return raw_runtime_kind.strip().lower()
    normalized_agent_id = str(agent_id or "").strip().lower()
    if normalized_agent_id == "hermes" or normalized_agent_id.startswith("hermes-"):
        return "hermes"
    return normalized_agent_id


def _legacy_hermes_profile_from_agent(agent_id: object) -> Optional[str]:
    if not isinstance(agent_id, str):
        return None
    normalized = agent_id.strip().lower()
    prefix = "hermes-"
    if not normalized.startswith(prefix):
        return None
    profile = normalized[len(prefix) :].strip()
    return profile or None


def _hermes_profile_from_runtime_agent(agent_id: str) -> str:
    return _legacy_hermes_profile_from_agent(agent_id) or str(agent_id).strip().lower()


def _valid_chat_agent_values(context: Any = None) -> tuple[str, ...]:
    return tuple(definition.value for definition in chat_agent_definitions(context))


def chat_agent_definitions(context: Any = None) -> tuple[ChatAgentDefinition, ...]:
    ordered: list[ChatAgentDefinition] = []
    seen: set[str] = set()
    registered = _registered_agents(context)

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
        if _agent_runtime_kind(agent_id, descriptor) == "hermes":
            continue
        ordered.append(
            ChatAgentDefinition(
                value=agent_id,
                description=descriptor.name,
            )
        )
        seen.add(agent_id)

    return tuple(ordered)


def valid_chat_agent_values(context: Any = None) -> tuple[str, ...]:
    return _valid_chat_agent_values(context)


def chat_hermes_profile_options(
    context: Any = None,
) -> tuple[ChatAgentProfileOption, ...]:
    registered = _registered_agents(context)
    options: list[ChatAgentProfileOption] = []
    seen_profiles: set[str] = set()
    for agent_id in sorted(registered):
        descriptor = registered[agent_id]
        if agent_id == "hermes":
            continue
        if _agent_runtime_kind(agent_id, descriptor) != "hermes":
            continue
        profile = _hermes_profile_from_runtime_agent(agent_id)
        if not profile or profile in seen_profiles:
            continue
        options.append(
            ChatAgentProfileOption(
                agent="hermes",
                profile=profile,
                runtime_agent=agent_id,
                description=str(getattr(descriptor, "name", "") or agent_id),
            )
        )
        seen_profiles.add(profile)
    return tuple(options)


def normalize_hermes_profile(value: object, *, context: Any = None) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    compact = "".join(ch for ch in normalized if ch.isalnum())
    for option in chat_hermes_profile_options(context):
        if normalized in {option.profile, option.runtime_agent}:
            return option.profile
        if compact in {
            "".join(ch for ch in option.profile if ch.isalnum()),
            "".join(ch for ch in option.runtime_agent if ch.isalnum()),
        }:
            return option.profile
    return _legacy_hermes_profile_from_agent(normalized)


def resolve_chat_agent_and_profile(
    agent: object,
    profile: object = None,
    *,
    default: Optional[str] = DEFAULT_CHAT_AGENT,
    context: Any = None,
) -> tuple[str, Optional[str]]:
    normalized_agent = normalize_chat_agent(agent, default=None, context=context)
    legacy_hermes_profile = None
    if normalized_agent is None:
        legacy_hermes_profile = normalize_hermes_profile(agent, context=context)
        if legacy_hermes_profile is not None:
            normalized_agent = "hermes"
    if normalized_agent is None:
        normalized_agent = default or DEFAULT_CHAT_AGENT
    normalized_profile = None
    if normalized_agent == "hermes":
        normalized_profile = normalize_hermes_profile(profile, context=context)
        if normalized_profile is None:
            normalized_profile = legacy_hermes_profile
    return normalized_agent, normalized_profile


def resolve_chat_runtime_agent(
    agent: object,
    profile: object = None,
    *,
    default: Optional[str] = DEFAULT_CHAT_AGENT,
    context: Any = None,
) -> str:
    normalized_agent, normalized_profile = resolve_chat_agent_and_profile(
        agent,
        profile,
        default=default,
        context=context,
    )
    if normalized_agent != "hermes" or normalized_profile is None:
        return normalized_agent
    for option in chat_hermes_profile_options(context):
        if option.profile == normalized_profile:
            return option.runtime_agent
    return normalized_agent


def format_chat_agent_selection(agent: object, profile: object = None) -> str:
    normalized_agent = (
        str(agent or "").strip().lower() or DEFAULT_CHAT_AGENT
        if isinstance(agent, str)
        else DEFAULT_CHAT_AGENT
    )
    if normalized_agent != "hermes":
        return normalized_agent
    if isinstance(profile, str) and profile.strip():
        return f"{normalized_agent} [{profile.strip().lower()}]"
    return normalized_agent


def normalize_chat_agent(
    value: object, *, default: Optional[str] = None, context: Any = None
) -> Optional[str]:
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower()
    compact = "".join(ch for ch in normalized if ch.isalnum())
    valid_values = _valid_chat_agent_values(context)
    if normalized in valid_values:
        return normalized
    if compact in valid_values:
        return compact
    return default


def chat_agent_supports_effort(agent: object, context: Any = None) -> bool:
    normalized = normalize_chat_agent(
        agent,
        default=DEFAULT_CHAT_AGENT,
        context=context,
    )
    if normalized is None:
        return False
    try:
        from ...agents.registry import get_agent_descriptor

        descriptor = get_agent_descriptor(normalized, context)
    except Exception:
        descriptor = None
    if descriptor is None:
        return normalized == "codex"
    return CHAT_EFFORT_CAPABILITY in descriptor.capabilities


def default_chat_model_for_agent(agent: object, context: Any = None) -> Optional[str]:
    normalized = normalize_chat_agent(
        agent,
        default=DEFAULT_CHAT_AGENT,
        context=context,
    )
    if normalized is None:
        return None
    return DEFAULT_CHAT_AGENT_MODELS.get(normalized)


def chat_agent_command_choices(context: Any = None) -> tuple[dict[str, str], ...]:
    return tuple(
        {"name": definition.value, "value": definition.value}
        for definition in chat_agent_definitions(context)
    )


def chat_agent_description(context: Any = None) -> str:
    return " or ".join(sorted(_valid_chat_agent_values(context)))


def build_agent_switch_state(
    agent: object,
    profile: object = None,
    *,
    model_reset: AgentModelResetMode,
    context: Any = None,
) -> ChatAgentSwitchState:
    normalized, normalized_profile = resolve_chat_agent_and_profile(
        agent,
        profile,
        default=DEFAULT_CHAT_AGENT,
        context=context,
    )
    model = (
        default_chat_model_for_agent(normalized, context)
        if model_reset == "agent_default"
        else None
    )
    return ChatAgentSwitchState(
        agent=normalized,
        profile=normalized_profile,
        model=model,
        effort=None,
    )
