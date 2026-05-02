from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Protocol

from ...runtime_capabilities import RuntimeCapability, normalize_runtime_capabilities
from .models import (
    AgentDefinition,
    TargetCapability,
)


class RuntimeAgentDescriptor(Protocol):
    id: str
    name: str
    runtime_kind: str
    capabilities: frozenset[RuntimeCapability]


KNOWN_CAPABILITIES: frozenset[TargetCapability] = frozenset(
    {
        "durable_threads",
        "message_turns",
        "interrupt",
        "active_thread_discovery",
        "transcript_history",
        "review",
        "model_listing",
        "event_streaming",
        "approvals",
    }
)


def map_agent_capabilities(
    capabilities: Iterable[RuntimeCapability],
) -> frozenset[TargetCapability]:
    normalized = normalize_runtime_capabilities(capabilities)
    return frozenset(
        capability for capability in KNOWN_CAPABILITIES if capability in normalized
    )


def merge_agent_capabilities(
    static_capabilities: Iterable[RuntimeCapability],
    runtime_capabilities: Optional[Iterable[RuntimeCapability]] = None,
) -> frozenset[TargetCapability]:
    merged = set(normalize_runtime_capabilities(static_capabilities))
    if runtime_capabilities is not None:
        merged.update(normalize_runtime_capabilities(runtime_capabilities))
    return map_agent_capabilities(merged)


def build_agent_definition(
    descriptor: RuntimeAgentDescriptor,
    *,
    repo_id: Optional[str] = None,
    workspace_root: Optional[str] = None,
    default_model: Optional[str] = None,
    description: Optional[str] = None,
    available: bool = True,
    runtime_capabilities: Optional[Iterable[RuntimeCapability]] = None,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=descriptor.id,
        display_name=descriptor.name,
        runtime_kind=getattr(descriptor, "runtime_kind", descriptor.id),
        capabilities=merge_agent_capabilities(
            descriptor.capabilities,
            runtime_capabilities,
        ),
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
    runtime_capability_reports: Optional[
        Mapping[str, Iterable[RuntimeCapability]]
    ] = None,
) -> list[AgentDefinition]:
    definitions = [
        build_agent_definition(
            descriptor,
            repo_id=repo_id,
            workspace_root=workspace_root,
            available=availability.get(agent_id, True) if availability else True,
            runtime_capabilities=(
                runtime_capability_reports.get(agent_id)
                if runtime_capability_reports
                else None
            ),
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
    runtime_capability_reports: Optional[
        Mapping[str, Iterable[RuntimeCapability]]
    ] = None,
) -> Optional[AgentDefinition]:
    descriptor = descriptors.get(agent_id)
    if descriptor is None:
        return None
    return build_agent_definition(
        descriptor,
        repo_id=repo_id,
        workspace_root=workspace_root,
        available=availability.get(agent_id, True) if availability else True,
        runtime_capabilities=(
            runtime_capability_reports.get(agent_id)
            if runtime_capability_reports
            else None
        ),
    )


@dataclass(frozen=True)
class MappingAgentDefinitionCatalog:
    """Thin catalog that projects registry-shaped descriptors into orchestration nouns."""

    descriptors: Mapping[str, RuntimeAgentDescriptor]
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    availability: Optional[Mapping[str, bool]] = None
    runtime_capability_reports: Optional[Mapping[str, Iterable[RuntimeCapability]]] = (
        None
    )

    def list_definitions(self) -> list[AgentDefinition]:
        return list_agent_definitions(
            self.descriptors,
            repo_id=self.repo_id,
            workspace_root=self.workspace_root,
            availability=self.availability,
            runtime_capability_reports=self.runtime_capability_reports,
        )

    def get_definition(self, agent_id: str) -> Optional[AgentDefinition]:
        return get_agent_definition(
            self.descriptors,
            agent_id,
            repo_id=self.repo_id,
            workspace_root=self.workspace_root,
            availability=self.availability,
            runtime_capability_reports=self.runtime_capability_reports,
        )


__all__ = [
    "MappingAgentDefinitionCatalog",
    "RuntimeAgentDescriptor",
    "build_agent_definition",
    "get_agent_definition",
    "list_agent_definitions",
    "map_agent_capabilities",
    "merge_agent_capabilities",
]
