from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Protocol

from .models import AgentDefinition, TargetCapability

RuntimeCapability = str


class RuntimeAgentDescriptor(Protocol):
    id: str
    name: str
    capabilities: frozenset[RuntimeCapability]


_CAPABILITY_MAP: dict[RuntimeCapability, TargetCapability] = {
    "threads": "durable_threads",
    "turns": "message_turns",
    "review": "review",
    "model_listing": "model_listing",
    "event_streaming": "event_streaming",
    "approvals": "approvals",
}


def map_agent_capabilities(
    capabilities: Iterable[RuntimeCapability],
) -> frozenset[TargetCapability]:
    return frozenset(
        _CAPABILITY_MAP[capability]
        for capability in capabilities
        if capability in _CAPABILITY_MAP
    )


def build_agent_definition(
    descriptor: RuntimeAgentDescriptor,
    *,
    repo_id: Optional[str] = None,
    workspace_root: Optional[str] = None,
    default_model: Optional[str] = None,
    description: Optional[str] = None,
    available: bool = True,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=descriptor.id,
        display_name=descriptor.name,
        runtime_kind=descriptor.id,
        capabilities=map_agent_capabilities(descriptor.capabilities),
        repo_id=repo_id,
        workspace_root=workspace_root,
        default_model=default_model,
        description=description,
        available=available,
    )


def list_agent_definitions(
    descriptors: Mapping[str, RuntimeAgentDescriptor],
    *,
    repo_id: Optional[str] = None,
    workspace_root: Optional[str] = None,
    availability: Optional[Mapping[str, bool]] = None,
) -> list[AgentDefinition]:
    definitions = [
        build_agent_definition(
            descriptor,
            repo_id=repo_id,
            workspace_root=workspace_root,
            available=availability.get(agent_id, True) if availability else True,
        )
        for agent_id, descriptor in descriptors.items()
    ]
    return sorted(definitions, key=lambda definition: definition.display_name.lower())


def get_agent_definition(
    descriptors: Mapping[str, RuntimeAgentDescriptor],
    agent_id: str,
    *,
    repo_id: Optional[str] = None,
    workspace_root: Optional[str] = None,
    availability: Optional[Mapping[str, bool]] = None,
) -> Optional[AgentDefinition]:
    descriptor = descriptors.get(agent_id)
    if descriptor is None:
        return None
    return build_agent_definition(
        descriptor,
        repo_id=repo_id,
        workspace_root=workspace_root,
        available=availability.get(agent_id, True) if availability else True,
    )


@dataclass(frozen=True)
class MappingAgentDefinitionCatalog:
    """Thin catalog that projects registry-shaped descriptors into orchestration nouns."""

    descriptors: Mapping[str, RuntimeAgentDescriptor]
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    availability: Optional[Mapping[str, bool]] = None

    def list_definitions(self) -> list[AgentDefinition]:
        return list_agent_definitions(
            self.descriptors,
            repo_id=self.repo_id,
            workspace_root=self.workspace_root,
            availability=self.availability,
        )

    def get_definition(self, agent_id: str) -> Optional[AgentDefinition]:
        return get_agent_definition(
            self.descriptors,
            agent_id,
            repo_id=self.repo_id,
            workspace_root=self.workspace_root,
            availability=self.availability,
        )


__all__ = [
    "MappingAgentDefinitionCatalog",
    "RuntimeAgentDescriptor",
    "build_agent_definition",
    "get_agent_definition",
    "list_agent_definitions",
    "map_agent_capabilities",
]
