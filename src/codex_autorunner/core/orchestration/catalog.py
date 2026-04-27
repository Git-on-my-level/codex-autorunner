from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Protocol

from ...runtime_capabilities import RuntimeCapability, normalize_runtime_capabilities
from .models import (
    AgentDefinition,
    NativeTargetDefinition,
    NativeTargetKind,
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


NATIVE_TARGET_REGISTRY: dict[NativeTargetKind, dict[str, str]] = {
    "ticket_flow": {
        "display_name": "Ticket Flow",
        "description": "CAR-native ticket-driven workflow execution engine for deterministic multi-step delivery work.",
    },
    "pma": {
        "display_name": "PMA",
        "description": "CAR-native thread management and orchestration client for durable conversation threads.",
    },
}


def build_native_target_definition(
    target_kind: NativeTargetKind,
    *,
    repo_id: Optional[str] = None,
    workspace_root: Optional[str] = None,
    available: bool = True,
) -> NativeTargetDefinition:
    """Build a native target definition from the registry."""
    registry_entry = NATIVE_TARGET_REGISTRY.get(target_kind)
    if registry_entry is None:
        raise ValueError(f"Unknown native target kind: {target_kind}")
    return NativeTargetDefinition(
        target_id=target_kind,
        target_kind=target_kind,
        display_name=registry_entry["display_name"],
        description=registry_entry["description"],
        repo_id=repo_id,
        workspace_root=workspace_root,
        available=available,
    )


def list_native_target_definitions(
    *,
    repo_id: Optional[str] = None,
    workspace_root: Optional[str] = None,
    availability: Optional[Mapping[NativeTargetKind, bool]] = None,
) -> list[NativeTargetDefinition]:
    """List all registered CAR-native target definitions.

    Native targets are durable, addressable CAR-native services that participate
    in orchestration routing. They are distinct from runtime-backed agents which
    are managed through the agent registry.

    Note: Helper subsystems such as dispatch interception, reactive debounce logic,
    and event projection services are NOT standalone orchestration targets. They
    remain internal plumbing rather than user-addressable target identities.
    """
    definitions = [
        build_native_target_definition(
            target_kind,
            repo_id=repo_id,
            workspace_root=workspace_root,
            available=availability.get(target_kind, True) if availability else True,
        )
        for target_kind in NATIVE_TARGET_REGISTRY
    ]
    return sorted(definitions, key=lambda d: d.display_name.lower())


def get_native_target_definition(
    target_kind: NativeTargetKind,
    *,
    repo_id: Optional[str] = None,
    workspace_root: Optional[str] = None,
    availability: Optional[Mapping[NativeTargetKind, bool]] = None,
) -> Optional[NativeTargetDefinition]:
    """Get a specific CAR-native target definition by kind."""
    if target_kind not in NATIVE_TARGET_REGISTRY:
        return None
    return build_native_target_definition(
        target_kind,
        repo_id=repo_id,
        workspace_root=workspace_root,
        available=availability.get(target_kind, True) if availability else True,
    )


def is_native_target(target_id: str) -> bool:
    """Check if a target ID refers to a CAR-native target."""
    return target_id in NATIVE_TARGET_REGISTRY


def is_helper_subsystem(identifier: str) -> bool:
    """Check if an identifier refers to a helper subsystem (not an orchestration target).

    Helper subsystems are internal plumbing components that should not be exposed
    as user-addressable orchestration targets. Examples include:
    - Dispatch interception handlers
    - Reactive debounce logic
    - Event projection services
    - Transcript mirroring services
    """
    helper_patterns = (
        "dispatch",
        "reactive",
        "debounce",
        "projection",
        "transcript_mirror",
        "event_sink",
    )
    return any(pattern in identifier.lower() for pattern in helper_patterns)


@dataclass(frozen=True)
class NativeTargetCatalog:
    """Catalog of CAR-native orchestration targets.

    This catalog exposes durable, addressable CAR-native services that participate
    in orchestration routing. It is distinct from the agent registry which handles
    runtime-backed agents.
    """

    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    availability: Optional[Mapping[NativeTargetKind, bool]] = None

    def list_definitions(self) -> list[NativeTargetDefinition]:
        return list_native_target_definitions(
            repo_id=self.repo_id,
            workspace_root=self.workspace_root,
            availability=self.availability,
        )

    def get_definition(
        self, target_kind: NativeTargetKind
    ) -> Optional[NativeTargetDefinition]:
        return get_native_target_definition(
            target_kind,
            repo_id=self.repo_id,
            workspace_root=self.workspace_root,
            availability=self.availability,
        )


__all__ = [
    "MappingAgentDefinitionCatalog",
    "NativeTargetCatalog",
    "RuntimeAgentDescriptor",
    "build_agent_definition",
    "build_native_target_definition",
    "get_agent_definition",
    "get_native_target_definition",
    "is_helper_subsystem",
    "is_native_target",
    "list_agent_definitions",
    "list_native_target_definitions",
    "map_agent_capabilities",
    "merge_agent_capabilities",
]
