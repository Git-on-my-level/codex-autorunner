from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Optional

from ..car_context import CarContextProfile, normalize_car_context_profile
from ..text_utils import _json_loads_object, _normalize_optional_text

TargetCapability = Literal[
    "durable_threads",
    "message_turns",
    "interrupt",
    "active_thread_discovery",
    "transcript_history",
    "review",
    "model_listing",
    "event_streaming",
    "structured_event_streaming",
    "approvals",
]
TargetKind = Literal["thread", "flow"]
NativeTargetKind = Literal["ticket_flow", "pma"]
MessageRequestKind = Literal["message", "review"]
BusyThreadPolicy = Literal["queue", "interrupt", "reject"]
OrchestrationTableRole = Literal["authoritative", "mirror", "projection", "ops"]


def _normalize_message_request_kind(value: Any) -> MessageRequestKind:
    normalized = _normalize_optional_text(value)
    if normalized == "review":
        return "review"
    return "message"


def _normalize_target_kind(value: Any) -> TargetKind:
    normalized = _normalize_optional_text(value)
    if normalized == "flow":
        return "flow"
    return "thread"


def _normalize_busy_thread_policy(value: Any) -> BusyThreadPolicy:
    normalized = _normalize_optional_text(value)
    if normalized == "interrupt":
        return "interrupt"
    if normalized == "reject":
        return "reject"
    return "queue"


def _normalize_input_items(value: Any) -> Optional[list[dict[str, Any]]]:
    if not isinstance(value, list):
        return None
    normalized_items = [dict(item) for item in value if isinstance(item, dict)]
    return normalized_items or None


def normalize_resource_owner_fields(
    *,
    resource_kind: Any = None,
    resource_id: Any = None,
    repo_id: Any = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    normalized_resource_kind = _normalize_optional_text(resource_kind)
    normalized_resource_id = _normalize_optional_text(resource_id)
    normalized_repo_id = _normalize_optional_text(repo_id)

    if normalized_resource_kind is None and normalized_resource_id is None:
        if normalized_repo_id is not None:
            normalized_resource_kind = "repo"
            normalized_resource_id = normalized_repo_id
        return normalized_resource_kind, normalized_resource_id, normalized_repo_id

    if normalized_resource_kind == "repo":
        if normalized_resource_id is None:
            normalized_resource_id = normalized_repo_id
        normalized_repo_id = normalized_resource_id or normalized_repo_id
    else:
        normalized_repo_id = None
    return normalized_resource_kind, normalized_resource_id, normalized_repo_id


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
class NativeTargetDefinition:
    """Orchestration-visible CAR-native target (not a runtime-backed agent).

    Native targets are durable, addressable CAR-native services that participate
    in orchestration routing. They are distinct from runtime-backed agents which
    are managed through the agent registry.

    Examples:
    - ticket_flow: CAR-native flow execution engine
    - pma: CAR-native thread management (generalized orchestration client)
    """

    target_id: str
    target_kind: NativeTargetKind
    display_name: str
    description: Optional[str] = None
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThreadTarget:
    """Orchestration-visible durable managed thread."""

    thread_target_id: str
    agent_id: str
    agent_profile: Optional[str] = None
    backend_thread_id: Optional[str] = None
    backend_runtime_instance_id: Optional[str] = None
    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    workspace_root: Optional[str] = None
    display_name: Optional[str] = None
    status: Optional[str] = None
    lifecycle_status: Optional[str] = None
    status_reason: Optional[str] = None
    status_changed_at: Optional[str] = None
    status_terminal: bool = False
    status_turn_id: Optional[str] = None
    last_execution_id: Optional[str] = None
    last_message_preview: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    compact_seed: Optional[str] = None
    thread_kind: Optional[str] = None
    context_profile: Optional[CarContextProfile] = None
    approval_mode: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTarget":
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        thread_target_id = _normalize_optional_text(
            data.get("managed_thread_id") or data.get("thread_target_id")
        )
        if thread_target_id is None:
            raise ValueError("ThreadTarget requires an orchestration-owned thread id")
        agent = (
            _normalize_optional_text(
                data.get("agent_id")
                or data.get("agent")
                or metadata.get("agent_id")
                or metadata.get("agent")
            )
            or "unknown"
        )
        resource_kind, resource_id, repo_id = normalize_resource_owner_fields(
            resource_kind=data.get("resource_kind"),
            resource_id=data.get("resource_id"),
            repo_id=data.get("repo_id"),
        )
        return cls(
            thread_target_id=thread_target_id,
            agent_id=agent,
            agent_profile=_normalize_optional_text(
                data.get("agent_profile") or metadata.get("agent_profile")
            ),
            backend_thread_id=_normalize_optional_text(data.get("backend_thread_id")),
            backend_runtime_instance_id=_normalize_optional_text(
                data.get("backend_runtime_instance_id")
                or metadata.get("backend_runtime_instance_id")
            ),
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_root=_normalize_optional_text(data.get("workspace_root")),
            display_name=_normalize_optional_text(
                data.get("name") or data.get("display_name")
            ),
            status=_normalize_optional_text(
                data.get("normalized_status") or data.get("status")
            ),
            lifecycle_status=_normalize_optional_text(
                data.get("lifecycle_status") or data.get("status")
            ),
            status_reason=_normalize_optional_text(
                data.get("status_reason") or data.get("status_reason_code")
            ),
            status_changed_at=_normalize_optional_text(
                data.get("status_changed_at") or data.get("status_updated_at")
            ),
            status_terminal=bool(data.get("status_terminal")),
            status_turn_id=_normalize_optional_text(data.get("status_turn_id")),
            last_execution_id=_normalize_optional_text(
                data.get("last_execution_id") or data.get("last_turn_id")
            ),
            last_message_preview=_normalize_optional_text(
                data.get("last_message_preview")
            ),
            created_at=_normalize_optional_text(data.get("created_at")),
            updated_at=_normalize_optional_text(data.get("updated_at")),
            compact_seed=_normalize_optional_text(data.get("compact_seed")),
            thread_kind=_normalize_optional_text(
                data.get("thread_kind") or metadata.get("thread_kind")
            ),
            context_profile=normalize_car_context_profile(
                data.get("context_profile") or metadata.get("context_profile")
            ),
            approval_mode=_normalize_optional_text(
                data.get("approval_mode") or metadata.get("approval_mode")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "agent": self.agent_id,
            "agent_id": self.agent_id,
            "agent_profile": self.agent_profile,
            "backend_thread_id": self.backend_thread_id,
            "backend_runtime_instance_id": self.backend_runtime_instance_id,
            "repo_id": self.repo_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "workspace_root": self.workspace_root,
            "name": self.display_name,
            "display_name": self.display_name,
            "status": self.status,
            "lifecycle_status": self.lifecycle_status,
            "status_reason": self.status_reason,
            "status_reason_code": self.status_reason,
            "status_changed_at": self.status_changed_at,
            "status_updated_at": self.status_changed_at,
            "status_terminal": self.status_terminal,
            "status_turn_id": self.status_turn_id,
            "last_execution_id": self.last_execution_id,
            "last_turn_id": self.last_execution_id,
            "last_message_preview": self.last_message_preview,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "compact_seed": self.compact_seed,
            "thread_kind": self.thread_kind,
            "context_profile": self.context_profile,
            "approval_mode": self.approval_mode,
        }


@dataclass(frozen=True)
class MessageRequest:
    """One requested action against an orchestration target."""

    target_id: str
    target_kind: TargetKind
    message_text: str
    kind: MessageRequestKind = "message"
    busy_policy: BusyThreadPolicy = "queue"
    agent_profile: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    approval_mode: Optional[str] = None
    input_items: Optional[list[dict[str, Any]]] = None
    context_profile: Optional[CarContextProfile] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        default_target_id: Optional[str] = None,
        busy_policy: Optional[BusyThreadPolicy] = None,
    ) -> "MessageRequest":
        target_id = _normalize_optional_text(
            data.get("target_id")
        ) or _normalize_optional_text(default_target_id)
        if target_id is None:
            raise ValueError("MessageRequest requires a target_id")
        message_text = _normalize_optional_text(data.get("message_text"))
        if message_text is None:
            raise ValueError("MessageRequest requires message_text")
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            target_id=target_id,
            target_kind=_normalize_target_kind(data.get("target_kind")),
            message_text=message_text,
            kind=_normalize_message_request_kind(data.get("kind")),
            busy_policy=busy_policy
            or _normalize_busy_thread_policy(data.get("busy_policy")),
            agent_profile=_normalize_optional_text(data.get("agent_profile")),
            model=_normalize_optional_text(data.get("model")),
            reasoning=_normalize_optional_text(data.get("reasoning")),
            approval_mode=_normalize_optional_text(data.get("approval_mode")),
            input_items=_normalize_input_items(data.get("input_items")),
            context_profile=normalize_car_context_profile(data.get("context_profile")),
            metadata=dict(metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QueuedExecutionRequest:
    """Typed queued execution envelope reconstructed from durable payloads."""

    request: MessageRequest
    client_request_id: Optional[str] = None
    sandbox_policy: Optional[Any] = None

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        thread_target_id: str,
    ) -> "QueuedExecutionRequest":
        request_data = payload.get("request")
        if not isinstance(request_data, Mapping):
            raise ValueError("Queued execution payload is missing request data")
        request = MessageRequest.from_mapping(
            request_data,
            default_target_id=thread_target_id,
            busy_policy="queue",
        )
        return cls(
            request=request,
            client_request_id=_normalize_optional_text(
                payload.get("client_request_id")
            ),
            sandbox_policy=payload.get("sandbox_policy"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "client_request_id": self.client_request_id,
            "sandbox_policy": self.sandbox_policy,
        }


@dataclass(frozen=True)
class ExecutionRecord:
    """One orchestration execution attempt against a thread or flow target."""

    execution_id: str
    target_id: str
    target_kind: TargetKind
    status: str
    request_kind: MessageRequestKind = "message"
    backend_id: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    output_text: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionRecord":
        execution_id = _normalize_optional_text(
            data.get("managed_turn_id") or data.get("execution_id")
        )
        target_id = _normalize_optional_text(
            data.get("managed_thread_id")
            or data.get("thread_target_id")
            or data.get("target_id")
        )
        if execution_id is None or target_id is None:
            raise ValueError(
                "ExecutionRecord requires execution_id/managed_turn_id and target_id/managed_thread_id"
            )
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = _json_loads_object(data.get("metadata_json"))
        return cls(
            execution_id=execution_id,
            target_id=target_id,
            target_kind="thread",
            status=_normalize_optional_text(data.get("status")) or "",
            request_kind=_normalize_message_request_kind(data.get("request_kind")),
            backend_id=_normalize_optional_text(
                data.get("backend_id") or data.get("backend_turn_id")
            ),
            started_at=_normalize_optional_text(data.get("started_at")),
            finished_at=_normalize_optional_text(data.get("finished_at")),
            error=_normalize_optional_text(data.get("error")),
            output_text=_normalize_optional_text(
                data.get("output_text") or data.get("assistant_text")
            ),
            metadata=dict(metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThreadStopOutcome:
    """Result of stopping a managed thread's active/queued execution state."""

    thread_target_id: str
    cancelled_queued: int = 0
    execution: Optional[ExecutionRecord] = None
    interrupted_active: bool = False
    recovered_lost_backend: bool = False

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
class FlowRunTarget:
    """Orchestration-visible flow-run status backed by CAR's native flow engine."""

    run_id: str
    flow_target_id: str
    flow_type: str
    status: str
    current_step: Optional[str] = None
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_message: Optional[str] = None
    state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

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
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
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
            data.get("thread_target_id")
            or data.get("target_id")
            or data.get("thread_id")
        )
        if binding_id is None or surface_kind is None or surface_key is None:
            raise ValueError(
                "Binding requires binding_id, surface_kind, and surface_key"
            )
        if thread_target_id is None:
            raise ValueError("Binding requires a thread target id")
        agent = _normalize_optional_text(data.get("agent_id") or data.get("agent"))
        resource_kind, resource_id, repo_id = normalize_resource_owner_fields(
            resource_kind=data.get("resource_kind"),
            resource_id=data.get("resource_id"),
            repo_id=data.get("repo_id"),
        )
        return cls(
            binding_id=binding_id,
            surface_kind=surface_kind,
            surface_key=surface_key,
            thread_target_id=thread_target_id,
            agent_id=agent,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
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
    "BusyThreadPolicy",
    "ExecutionRecord",
    "FlowRunTarget",
    "FlowTarget",
    "MessageRequest",
    "NativeTargetDefinition",
    "NativeTargetKind",
    "OrchestrationTableDefinition",
    "OrchestrationTableRole",
    "TargetCapability",
    "TargetKind",
    "ThreadTarget",
]
