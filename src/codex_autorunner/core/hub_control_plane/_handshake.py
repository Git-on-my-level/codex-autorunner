from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional

from ._normalizers import (
    coerce_int,
    normalize_optional_text,
    normalize_required_text,
    normalize_string_set,
)

HandshakeCompatibilityState = Literal["compatible", "incompatible"]

ControlPlaneCapability = Literal[
    "compatibility_handshake",
    "notification_records",
    "notification_reply_targets",
    "notification_continuations",
    "notification_delivery_ack",
    "pma_snapshot",
    "surface_bindings",
    "thread_execution_lifecycle",
    "thread_activity_updates",
    "thread_backend_updates",
    "thread_target_creation",
    "thread_targets",
    "transcript_history",
    "compact_seed_updates",
    "agent_workspaces",
    "workspace_setup_commands",
    "automation_requests",
    "execution_timeline_persistence",
    "execution_cold_trace_finalization",
    "transcript_writes",
]


@dataclass(frozen=True, order=True)
class ControlPlaneVersion:
    major: int
    minor: int = 0
    patch: int = 0

    @classmethod
    def parse(cls, value: "ControlPlaneVersion | str") -> "ControlPlaneVersion":
        if isinstance(value, ControlPlaneVersion):
            return value
        normalized = normalize_required_text(value, field_name="version")
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
            client_name=normalize_required_text(
                data.get("client_name"), field_name="client_name"
            ),
            client_api_version=str(
                ControlPlaneVersion.parse(
                    normalize_required_text(
                        data.get("client_api_version"),
                        field_name="client_api_version",
                    )
                )
            ),
            client_version=normalize_optional_text(data.get("client_version")),
            expected_schema_generation=(
                coerce_int(
                    raw_schema_generation,
                    field_name="expected_schema_generation",
                )
                if raw_schema_generation is not None
                else None
            ),
            supported_capabilities=normalize_string_set(
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
                    normalize_required_text(
                        data.get("api_version"),
                        field_name="api_version",
                    )
                )
            ),
            minimum_client_api_version=str(
                ControlPlaneVersion.parse(
                    normalize_required_text(
                        data.get("minimum_client_api_version"),
                        field_name="minimum_client_api_version",
                    )
                )
            ),
            schema_generation=coerce_int(
                data.get("schema_generation"),
                field_name="schema_generation",
            ),
            capabilities=normalize_string_set(data.get("capabilities")),
            hub_build_version=normalize_optional_text(data.get("hub_build_version")),
            hub_asset_version=normalize_optional_text(data.get("hub_asset_version")),
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


__all__ = [
    "ControlPlaneCapability",
    "ControlPlaneVersion",
    "HandshakeCompatibility",
    "HandshakeCompatibilityState",
    "HandshakeRequest",
    "HandshakeResponse",
    "evaluate_handshake_compatibility",
]
