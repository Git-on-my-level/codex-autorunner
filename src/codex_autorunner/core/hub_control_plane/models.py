from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping, Optional

from ..orchestration.models import Binding, ThreadTarget

HandshakeCompatibilityState = Literal["compatible", "incompatible"]

ControlPlaneCapability = Literal[
    "compatibility_handshake",
    "notification_records",
    "notification_reply_targets",
    "notification_continuations",
    "notification_delivery_ack",
    "surface_bindings",
    "thread_targets",
    "compact_seed_updates",
    "agent_workspaces",
    "workspace_setup_commands",
    "automation_requests",
]


def _normalize_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_required_text(value: Any, *, field_name: str) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def _normalize_optional_identifier(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        normalized = str(value).strip()
        return normalized or None
    return None


def _normalize_required_identifier(value: Any, *, field_name: str) -> str:
    normalized = _normalize_optional_identifier(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def _copy_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return dict(value)


def _normalize_string_set(value: Iterable[Any] | None) -> tuple[str, ...]:
    if value is None or isinstance(value, (str, bytes)):
        return ()
    normalized = {
        item.strip() for item in value if isinstance(item, str) and item.strip()
    }
    return tuple(sorted(normalized))


def _coerce_int(value: Any, *, field_name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if normalized < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return normalized


@dataclass(frozen=True, order=True)
class ControlPlaneVersion:
    """Semver-like API version used by the hub control plane."""

    major: int
    minor: int = 0
    patch: int = 0

    @classmethod
    def parse(cls, value: "ControlPlaneVersion | str") -> "ControlPlaneVersion":
        if isinstance(value, ControlPlaneVersion):
            return value
        normalized = _normalize_required_text(value, field_name="version")
        tokens = normalized.split(".")
        if len(tokens) > 3:
            raise ValueError("version must contain at most three dot-separated parts")
        parts: list[int] = []
        for token in tokens:
            if not token.isdigit():
                raise ValueError("version parts must be numeric")
            parts.append(int(token))
        while len(parts) < 3:
            parts.append(0)
        return cls(*parts[:3])

    def to_string(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __str__(self) -> str:
        return self.to_string()


@dataclass(frozen=True)
class HandshakeRequest:
    client_name: str
    client_api_version: str
    client_version: Optional[str] = None
    expected_schema_generation: Optional[int] = None
    supported_capabilities: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HandshakeRequest":
        raw_schema_generation = data.get("expected_schema_generation")
        return cls(
            client_name=_normalize_required_text(
                data.get("client_name"), field_name="client_name"
            ),
            client_api_version=str(
                ControlPlaneVersion.parse(
                    _normalize_required_text(
                        data.get("client_api_version"),
                        field_name="client_api_version",
                    )
                )
            ),
            client_version=_normalize_optional_text(data.get("client_version")),
            expected_schema_generation=(
                _coerce_int(
                    raw_schema_generation,
                    field_name="expected_schema_generation",
                )
                if raw_schema_generation is not None
                else None
            ),
            supported_capabilities=_normalize_string_set(
                data.get("supported_capabilities")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_name": self.client_name,
            "client_api_version": self.client_api_version,
            "client_version": self.client_version,
            "expected_schema_generation": self.expected_schema_generation,
            "supported_capabilities": list(self.supported_capabilities),
        }


@dataclass(frozen=True)
class HandshakeResponse:
    api_version: str
    minimum_client_api_version: str
    schema_generation: int
    capabilities: tuple[str, ...]
    hub_build_version: Optional[str] = None
    hub_asset_version: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HandshakeResponse":
        return cls(
            api_version=str(
                ControlPlaneVersion.parse(
                    _normalize_required_text(
                        data.get("api_version"),
                        field_name="api_version",
                    )
                )
            ),
            minimum_client_api_version=str(
                ControlPlaneVersion.parse(
                    _normalize_required_text(
                        data.get("minimum_client_api_version"),
                        field_name="minimum_client_api_version",
                    )
                )
            ),
            schema_generation=_coerce_int(
                data.get("schema_generation"),
                field_name="schema_generation",
            ),
            capabilities=_normalize_string_set(data.get("capabilities")),
            hub_build_version=_normalize_optional_text(data.get("hub_build_version")),
            hub_asset_version=_normalize_optional_text(data.get("hub_asset_version")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "minimum_client_api_version": self.minimum_client_api_version,
            "schema_generation": self.schema_generation,
            "capabilities": list(self.capabilities),
            "hub_build_version": self.hub_build_version,
            "hub_asset_version": self.hub_asset_version,
        }


@dataclass(frozen=True)
class HandshakeCompatibility:
    state: HandshakeCompatibilityState
    reason: Optional[str] = None
    server_api_version: Optional[str] = None
    client_api_version: Optional[str] = None
    server_schema_generation: Optional[int] = None
    expected_schema_generation: Optional[int] = None

    @property
    def compatible(self) -> bool:
        return self.state == "compatible"


def evaluate_handshake_compatibility(
    response: HandshakeResponse,
    *,
    client_api_version: str,
    expected_schema_generation: Optional[int] = None,
) -> HandshakeCompatibility:
    client_version = ControlPlaneVersion.parse(client_api_version)
    server_version = ControlPlaneVersion.parse(response.api_version)
    minimum_client_version = ControlPlaneVersion.parse(
        response.minimum_client_api_version
    )
    if client_version.major != server_version.major:
        return HandshakeCompatibility(
            state="incompatible",
            reason="control-plane API major version mismatch",
            server_api_version=response.api_version,
            client_api_version=str(client_version),
            server_schema_generation=response.schema_generation,
            expected_schema_generation=expected_schema_generation,
        )
    if client_version < minimum_client_version:
        return HandshakeCompatibility(
            state="incompatible",
            reason="client API version is older than the hub minimum",
            server_api_version=response.api_version,
            client_api_version=str(client_version),
            server_schema_generation=response.schema_generation,
            expected_schema_generation=expected_schema_generation,
        )
    if (
        expected_schema_generation is not None
        and response.schema_generation != expected_schema_generation
    ):
        return HandshakeCompatibility(
            state="incompatible",
            reason="orchestration schema generation mismatch",
            server_api_version=response.api_version,
            client_api_version=str(client_version),
            server_schema_generation=response.schema_generation,
            expected_schema_generation=expected_schema_generation,
        )
    return HandshakeCompatibility(
        state="compatible",
        server_api_version=response.api_version,
        client_api_version=str(client_version),
        server_schema_generation=response.schema_generation,
        expected_schema_generation=expected_schema_generation,
    )


@dataclass(frozen=True)
class NotificationRecord:
    notification_id: str
    correlation_id: str
    source_kind: str
    delivery_mode: str
    surface_kind: str
    surface_key: str
    delivery_record_id: str
    delivered_message_id: Optional[str] = None
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    run_id: Optional[str] = None
    managed_thread_id: Optional[str] = None
    continuation_thread_target_id: Optional[str] = None
    context: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "NotificationRecord":
        return cls(
            notification_id=_normalize_required_text(
                data.get("notification_id"),
                field_name="notification_id",
            ),
            correlation_id=_normalize_required_text(
                data.get("correlation_id"),
                field_name="correlation_id",
            ),
            source_kind=_normalize_required_text(
                data.get("source_kind"),
                field_name="source_kind",
            ),
            delivery_mode=_normalize_required_text(
                data.get("delivery_mode"),
                field_name="delivery_mode",
            ),
            surface_kind=_normalize_required_text(
                data.get("surface_kind"),
                field_name="surface_kind",
            ),
            surface_key=_normalize_required_text(
                data.get("surface_key"),
                field_name="surface_key",
            ),
            delivery_record_id=_normalize_required_text(
                data.get("delivery_record_id"),
                field_name="delivery_record_id",
            ),
            delivered_message_id=_normalize_optional_text(
                data.get("delivered_message_id")
            ),
            repo_id=_normalize_optional_text(data.get("repo_id")),
            workspace_root=_normalize_optional_text(data.get("workspace_root")),
            run_id=_normalize_optional_text(data.get("run_id")),
            managed_thread_id=_normalize_optional_text(data.get("managed_thread_id")),
            continuation_thread_target_id=_normalize_optional_text(
                data.get("continuation_thread_target_id")
            ),
            context=_copy_mapping(data.get("context")),
            created_at=_normalize_optional_text(data.get("created_at")),
            updated_at=_normalize_optional_text(data.get("updated_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "correlation_id": self.correlation_id,
            "source_kind": self.source_kind,
            "delivery_mode": self.delivery_mode,
            "surface_kind": self.surface_kind,
            "surface_key": self.surface_key,
            "delivery_record_id": self.delivery_record_id,
            "delivered_message_id": self.delivered_message_id,
            "repo_id": self.repo_id,
            "workspace_root": self.workspace_root,
            "run_id": self.run_id,
            "managed_thread_id": self.managed_thread_id,
            "continuation_thread_target_id": self.continuation_thread_target_id,
            "context": dict(self.context),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class NotificationLookupRequest:
    notification_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "NotificationLookupRequest":
        return cls(
            notification_id=_normalize_required_text(
                data.get("notification_id"),
                field_name="notification_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"notification_id": self.notification_id}


@dataclass(frozen=True)
class NotificationReplyTargetLookupRequest:
    surface_kind: str
    surface_key: str
    delivered_message_id: str

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any]
    ) -> "NotificationReplyTargetLookupRequest":
        return cls(
            surface_kind=_normalize_required_text(
                data.get("surface_kind"),
                field_name="surface_kind",
            ),
            surface_key=_normalize_required_text(
                data.get("surface_key"),
                field_name="surface_key",
            ),
            delivered_message_id=_normalize_required_identifier(
                data.get("delivered_message_id"),
                field_name="delivered_message_id",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface_kind": self.surface_kind,
            "surface_key": self.surface_key,
            "delivered_message_id": self.delivered_message_id,
        }


@dataclass(frozen=True)
class NotificationDeliveryMarkRequest:
    delivery_record_id: str
    delivered_message_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "NotificationDeliveryMarkRequest":
        return cls(
            delivery_record_id=_normalize_required_text(
                data.get("delivery_record_id"),
                field_name="delivery_record_id",
            ),
            delivered_message_id=_normalize_required_identifier(
                data.get("delivered_message_id"),
                field_name="delivered_message_id",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_record_id": self.delivery_record_id,
            "delivered_message_id": self.delivered_message_id,
        }


@dataclass(frozen=True)
class NotificationContinuationBindRequest:
    notification_id: str
    thread_target_id: str

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any]
    ) -> "NotificationContinuationBindRequest":
        return cls(
            notification_id=_normalize_required_text(
                data.get("notification_id"),
                field_name="notification_id",
            ),
            thread_target_id=_normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "thread_target_id": self.thread_target_id,
        }


@dataclass(frozen=True)
class NotificationRecordResponse:
    record: Optional[NotificationRecord]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "NotificationRecordResponse":
        raw_record = data.get("record")
        record = (
            NotificationRecord.from_mapping(raw_record)
            if isinstance(raw_record, Mapping)
            else None
        )
        return cls(record=record)

    def to_dict(self) -> dict[str, Any]:
        return {"record": None if self.record is None else self.record.to_dict()}


@dataclass(frozen=True)
class SurfaceBindingLookupRequest:
    surface_kind: str
    surface_key: str
    include_disabled: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SurfaceBindingLookupRequest":
        return cls(
            surface_kind=_normalize_required_text(
                data.get("surface_kind"),
                field_name="surface_kind",
            ),
            surface_key=_normalize_required_text(
                data.get("surface_key"),
                field_name="surface_key",
            ),
            include_disabled=bool(data.get("include_disabled")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface_kind": self.surface_kind,
            "surface_key": self.surface_key,
            "include_disabled": self.include_disabled,
        }


@dataclass(frozen=True)
class SurfaceBindingUpsertRequest:
    surface_kind: str
    surface_key: str
    thread_target_id: str
    agent_id: Optional[str] = None
    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    mode: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SurfaceBindingUpsertRequest":
        return cls(
            surface_kind=_normalize_required_text(
                data.get("surface_kind"),
                field_name="surface_kind",
            ),
            surface_key=_normalize_required_text(
                data.get("surface_key"),
                field_name="surface_key",
            ),
            thread_target_id=_normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            agent_id=_normalize_optional_text(data.get("agent_id")),
            repo_id=_normalize_optional_text(data.get("repo_id")),
            resource_kind=_normalize_optional_text(data.get("resource_kind")),
            resource_id=_normalize_optional_text(data.get("resource_id")),
            mode=_normalize_optional_text(data.get("mode")),
            metadata=_copy_mapping(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface_kind": self.surface_kind,
            "surface_key": self.surface_key,
            "thread_target_id": self.thread_target_id,
            "agent_id": self.agent_id,
            "repo_id": self.repo_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "mode": self.mode,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SurfaceBindingResponse:
    binding: Optional[Binding]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SurfaceBindingResponse":
        raw_binding = data.get("binding")
        binding = (
            Binding.from_mapping(raw_binding)
            if isinstance(raw_binding, Mapping)
            else None
        )
        return cls(binding=binding)

    def to_dict(self) -> dict[str, Any]:
        return {"binding": None if self.binding is None else self.binding.to_dict()}


@dataclass(frozen=True)
class ThreadTargetLookupRequest:
    thread_target_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTargetLookupRequest":
        return cls(
            thread_target_id=_normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"thread_target_id": self.thread_target_id}


@dataclass(frozen=True)
class ThreadTargetListRequest:
    agent_id: Optional[str] = None
    lifecycle_status: Optional[str] = None
    runtime_status: Optional[str] = None
    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    limit: int = 200

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTargetListRequest":
        return cls(
            agent_id=_normalize_optional_text(data.get("agent_id")),
            lifecycle_status=_normalize_optional_text(data.get("lifecycle_status")),
            runtime_status=_normalize_optional_text(data.get("runtime_status")),
            repo_id=_normalize_optional_text(data.get("repo_id")),
            resource_kind=_normalize_optional_text(data.get("resource_kind")),
            resource_id=_normalize_optional_text(data.get("resource_id")),
            limit=max(1, _coerce_int(data.get("limit", 200), field_name="limit")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "lifecycle_status": self.lifecycle_status,
            "runtime_status": self.runtime_status,
            "repo_id": self.repo_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class ThreadTargetResumeRequest:
    thread_target_id: str
    backend_thread_id: Optional[str] = None
    backend_runtime_instance_id: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTargetResumeRequest":
        return cls(
            thread_target_id=_normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            backend_thread_id=_normalize_optional_text(data.get("backend_thread_id")),
            backend_runtime_instance_id=_normalize_optional_text(
                data.get("backend_runtime_instance_id")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "backend_thread_id": self.backend_thread_id,
            "backend_runtime_instance_id": self.backend_runtime_instance_id,
        }


@dataclass(frozen=True)
class ThreadTargetArchiveRequest:
    thread_target_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTargetArchiveRequest":
        return cls(
            thread_target_id=_normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"thread_target_id": self.thread_target_id}


@dataclass(frozen=True)
class ThreadCompactSeedUpdateRequest:
    thread_target_id: str
    compact_seed: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadCompactSeedUpdateRequest":
        return cls(
            thread_target_id=_normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            compact_seed=_normalize_required_text(
                data.get("compact_seed"),
                field_name="compact_seed",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "compact_seed": self.compact_seed,
        }


@dataclass(frozen=True)
class ThreadTargetResponse:
    thread: Optional[ThreadTarget]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTargetResponse":
        raw_thread = data.get("thread")
        thread = (
            ThreadTarget.from_mapping(raw_thread)
            if isinstance(raw_thread, Mapping)
            else None
        )
        return cls(thread=thread)

    def to_dict(self) -> dict[str, Any]:
        return {"thread": None if self.thread is None else self.thread.to_dict()}


@dataclass(frozen=True)
class ThreadTargetListResponse:
    threads: tuple[ThreadTarget, ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTargetListResponse":
        raw_threads = data.get("threads")
        if not isinstance(raw_threads, list):
            return cls(threads=())
        return cls(
            threads=tuple(
                ThreadTarget.from_mapping(item)
                for item in raw_threads
                if isinstance(item, Mapping)
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"threads": [thread.to_dict() for thread in self.threads]}


@dataclass(frozen=True)
class AgentWorkspaceDescriptor:
    workspace_id: str
    runtime_kind: str
    workspace_root: str
    display_name: str
    enabled: bool
    exists_on_disk: bool
    resource_kind: str = "agent_workspace"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentWorkspaceDescriptor":
        return cls(
            workspace_id=_normalize_required_text(
                data.get("workspace_id") or data.get("id"),
                field_name="workspace_id",
            ),
            runtime_kind=_normalize_required_text(
                data.get("runtime_kind") or data.get("runtime"),
                field_name="runtime_kind",
            ),
            workspace_root=_normalize_required_text(
                data.get("workspace_root") or data.get("path"),
                field_name="workspace_root",
            ),
            display_name=_normalize_required_text(
                data.get("display_name"),
                field_name="display_name",
            ),
            enabled=bool(data.get("enabled", True)),
            exists_on_disk=bool(data.get("exists_on_disk", True)),
            resource_kind=(
                _normalize_optional_text(data.get("resource_kind")) or "agent_workspace"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "runtime_kind": self.runtime_kind,
            "workspace_root": self.workspace_root,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "exists_on_disk": self.exists_on_disk,
            "resource_kind": self.resource_kind,
        }


@dataclass(frozen=True)
class AgentWorkspaceLookupRequest:
    workspace_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentWorkspaceLookupRequest":
        return cls(
            workspace_id=_normalize_required_text(
                data.get("workspace_id"),
                field_name="workspace_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"workspace_id": self.workspace_id}


@dataclass(frozen=True)
class AgentWorkspaceListRequest:
    include_disabled: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentWorkspaceListRequest":
        return cls(include_disabled=bool(data.get("include_disabled", True)))

    def to_dict(self) -> dict[str, Any]:
        return {"include_disabled": self.include_disabled}


@dataclass(frozen=True)
class AgentWorkspaceResponse:
    workspace: Optional[AgentWorkspaceDescriptor]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentWorkspaceResponse":
        raw_workspace = data.get("workspace")
        workspace = (
            AgentWorkspaceDescriptor.from_mapping(raw_workspace)
            if isinstance(raw_workspace, Mapping)
            else None
        )
        return cls(workspace=workspace)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": (None if self.workspace is None else self.workspace.to_dict())
        }


@dataclass(frozen=True)
class AgentWorkspaceListResponse:
    workspaces: tuple[AgentWorkspaceDescriptor, ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentWorkspaceListResponse":
        raw_workspaces = data.get("workspaces")
        if not isinstance(raw_workspaces, list):
            return cls(workspaces=())
        return cls(
            workspaces=tuple(
                AgentWorkspaceDescriptor.from_mapping(item)
                for item in raw_workspaces
                if isinstance(item, Mapping)
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"workspaces": [workspace.to_dict() for workspace in self.workspaces]}


@dataclass(frozen=True)
class WorkspaceSetupCommandRequest:
    workspace_root: str
    repo_id_hint: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "WorkspaceSetupCommandRequest":
        return cls(
            workspace_root=_normalize_required_text(
                data.get("workspace_root"),
                field_name="workspace_root",
            ),
            repo_id_hint=_normalize_optional_text(data.get("repo_id_hint")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_root,
            "repo_id_hint": self.repo_id_hint,
        }


@dataclass(frozen=True)
class WorkspaceSetupCommandResult:
    workspace_root: str
    repo_id_hint: Optional[str] = None
    setup_command_count: int = 0

    @property
    def executed(self) -> bool:
        return self.setup_command_count > 0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "WorkspaceSetupCommandResult":
        return cls(
            workspace_root=_normalize_required_text(
                data.get("workspace_root"),
                field_name="workspace_root",
            ),
            repo_id_hint=_normalize_optional_text(data.get("repo_id_hint")),
            setup_command_count=_coerce_int(
                data.get("setup_command_count", 0),
                field_name="setup_command_count",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_root,
            "repo_id_hint": self.repo_id_hint,
            "setup_command_count": self.setup_command_count,
            "executed": self.executed,
        }


@dataclass(frozen=True)
class AutomationRequest:
    operation: str
    surface_kind: Optional[str] = None
    surface_key: Optional[str] = None
    thread_target_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationRequest":
        return cls(
            operation=_normalize_required_text(
                data.get("operation"),
                field_name="operation",
            ),
            surface_kind=_normalize_optional_text(data.get("surface_kind")),
            surface_key=_normalize_optional_text(data.get("surface_key")),
            thread_target_id=_normalize_optional_text(data.get("thread_target_id")),
            payload=_copy_mapping(data.get("payload")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "surface_kind": self.surface_kind,
            "surface_key": self.surface_key,
            "thread_target_id": self.thread_target_id,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class AutomationResult:
    operation: str
    accepted: bool
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationResult":
        return cls(
            operation=_normalize_required_text(
                data.get("operation"),
                field_name="operation",
            ),
            accepted=bool(data.get("accepted")),
            payload=_copy_mapping(data.get("payload")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "accepted": self.accepted,
            "payload": dict(self.payload),
        }


__all__ = [
    "AgentWorkspaceDescriptor",
    "AgentWorkspaceListRequest",
    "AgentWorkspaceListResponse",
    "AgentWorkspaceLookupRequest",
    "AgentWorkspaceResponse",
    "AutomationRequest",
    "AutomationResult",
    "Binding",
    "ControlPlaneCapability",
    "ControlPlaneVersion",
    "HandshakeCompatibility",
    "HandshakeCompatibilityState",
    "HandshakeRequest",
    "HandshakeResponse",
    "NotificationContinuationBindRequest",
    "NotificationDeliveryMarkRequest",
    "NotificationLookupRequest",
    "NotificationReplyTargetLookupRequest",
    "NotificationRecord",
    "NotificationRecordResponse",
    "SurfaceBindingLookupRequest",
    "SurfaceBindingResponse",
    "SurfaceBindingUpsertRequest",
    "ThreadCompactSeedUpdateRequest",
    "ThreadTarget",
    "ThreadTargetArchiveRequest",
    "ThreadTargetListRequest",
    "ThreadTargetListResponse",
    "ThreadTargetLookupRequest",
    "ThreadTargetResponse",
    "ThreadTargetResumeRequest",
    "WorkspaceSetupCommandRequest",
    "WorkspaceSetupCommandResult",
    "evaluate_handshake_compatibility",
]
