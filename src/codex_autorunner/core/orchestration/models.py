from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Optional

TargetCapability = Literal[
    "durable_threads",
    "message_turns",
    "review",
    "model_listing",
    "event_streaming",
    "approvals",
]
TargetKind = Literal["thread", "flow"]
MessageRequestKind = Literal["message", "review"]
OrchestrationTableRole = Literal["authoritative", "mirror", "projection", "ops"]


def _normalize_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


@dataclass(frozen=True)
class AgentDefinition:
    """Orchestration-visible logical agent identity."""

    agent_id: str
    display_name: str
    runtime_kind: str
    capabilities: frozenset[TargetCapability] = field(default_factory=frozenset)
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    default_model: Optional[str] = None
    description: Optional[str] = None
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThreadTarget:
    """Orchestration-visible durable runtime thread/session."""

    thread_target_id: str
    agent_id: str
    backend_thread_id: Optional[str] = None
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    display_name: Optional[str] = None
    status: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTarget":
        thread_target_id = _normalize_optional_text(
            data.get("managed_thread_id") or data.get("thread_target_id")
        )
        if thread_target_id is None:
            raise ValueError("ThreadTarget requires an orchestration-owned thread id")
        agent = _normalize_optional_text(data.get("agent")) or "unknown"
        return cls(
            thread_target_id=thread_target_id,
            agent_id=agent,
            backend_thread_id=_normalize_optional_text(data.get("backend_thread_id")),
            repo_id=_normalize_optional_text(data.get("repo_id")),
            workspace_root=_normalize_optional_text(data.get("workspace_root")),
            display_name=_normalize_optional_text(data.get("name")),
            status=_normalize_optional_text(
                data.get("normalized_status") or data.get("status")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MessageRequest:
    """One requested action against an orchestration target."""

    target_id: str
    target_kind: TargetKind
    message_text: str
    kind: MessageRequestKind = "message"
    model: Optional[str] = None
    reasoning: Optional[str] = None
    approval_mode: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionRecord:
    """One orchestration execution attempt against a thread or flow target."""

    execution_id: str
    target_id: str
    target_kind: TargetKind
    status: str
    backend_id: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    output_text: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrchestrationTableDefinition:
    """Schema metadata for one orchestration SQLite table."""

    name: str
    role: OrchestrationTableRole
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FlowTarget:
    """Orchestration-visible CAR-native flow target."""

    flow_target_id: str
    flow_type: str
    display_name: str
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    description: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Binding:
    """Durable association between a surface context and a thread target."""

    binding_id: str
    surface_kind: str
    surface_key: str
    thread_target_id: str
    agent_id: Optional[str] = None
    repo_id: Optional[str] = None
    mode: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    disabled_at: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Binding":
        binding_id = _normalize_optional_text(data.get("binding_id"))
        surface_kind = _normalize_optional_text(data.get("surface_kind"))
        surface_key = _normalize_optional_text(data.get("surface_key"))
        thread_target_id = _normalize_optional_text(
            data.get("thread_target_id") or data.get("thread_id")
        )
        if binding_id is None or surface_kind is None or surface_key is None:
            raise ValueError(
                "Binding requires binding_id, surface_kind, and surface_key"
            )
        if thread_target_id is None:
            raise ValueError("Binding requires a thread target id")
        agent = _normalize_optional_text(data.get("agent_id") or data.get("agent"))
        return cls(
            binding_id=binding_id,
            surface_kind=surface_kind,
            surface_key=surface_key,
            thread_target_id=thread_target_id,
            agent_id=agent,
            repo_id=_normalize_optional_text(data.get("repo_id")),
            mode=_normalize_optional_text(data.get("mode")),
            created_at=_normalize_optional_text(data.get("created_at")),
            updated_at=_normalize_optional_text(data.get("updated_at")),
            disabled_at=_normalize_optional_text(data.get("disabled_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "AgentDefinition",
    "Binding",
    "ExecutionRecord",
    "FlowTarget",
    "MessageRequest",
    "OrchestrationTableDefinition",
    "OrchestrationTableRole",
    "TargetCapability",
    "TargetKind",
    "ThreadTarget",
]
