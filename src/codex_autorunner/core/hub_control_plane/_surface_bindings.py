from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..orchestration.models import Binding
from ._normalizers import (
    coerce_int,
    copy_mapping,
    normalize_optional_text,
    normalize_required_text,
)


@dataclass(frozen=True)
class SurfaceBindingLookupRequest:
    surface_kind: str
    surface_key: str
    include_disabled: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SurfaceBindingLookupRequest":
        return cls(
            surface_kind=normalize_required_text(
                data.get("surface_kind"),
                field_name="surface_kind",
            ),
            surface_key=normalize_required_text(
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
            surface_kind=normalize_required_text(
                data.get("surface_kind"),
                field_name="surface_kind",
            ),
            surface_key=normalize_required_text(
                data.get("surface_key"),
                field_name="surface_key",
            ),
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            agent_id=normalize_optional_text(data.get("agent_id")),
            repo_id=normalize_optional_text(data.get("repo_id")),
            resource_kind=normalize_optional_text(data.get("resource_kind")),
            resource_id=normalize_optional_text(data.get("resource_id")),
            mode=normalize_optional_text(data.get("mode")),
            metadata=copy_mapping(data.get("metadata")),
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
class SurfaceBindingListRequest:
    thread_target_id: Optional[str] = None
    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    agent_id: Optional[str] = None
    surface_kind: Optional[str] = None
    include_disabled: bool = False
    limit: int = 200

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SurfaceBindingListRequest":
        return cls(
            thread_target_id=normalize_optional_text(data.get("thread_target_id")),
            repo_id=normalize_optional_text(data.get("repo_id")),
            resource_kind=normalize_optional_text(data.get("resource_kind")),
            resource_id=normalize_optional_text(data.get("resource_id")),
            agent_id=normalize_optional_text(data.get("agent_id")),
            surface_kind=normalize_optional_text(data.get("surface_kind")),
            include_disabled=bool(data.get("include_disabled")),
            limit=max(1, coerce_int(data.get("limit", 200), field_name="limit")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "repo_id": self.repo_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "agent_id": self.agent_id,
            "surface_kind": self.surface_kind,
            "include_disabled": self.include_disabled,
            "limit": self.limit,
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
class SurfaceBindingListResponse:
    bindings: tuple[Binding, ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SurfaceBindingListResponse":
        raw_bindings = data.get("bindings")
        if not isinstance(raw_bindings, list):
            return cls(bindings=())
        return cls(
            bindings=tuple(
                Binding.from_mapping(item)
                for item in raw_bindings
                if isinstance(item, Mapping)
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"bindings": [binding.to_dict() for binding in self.bindings]}


__all__ = [
    "SurfaceBindingListRequest",
    "SurfaceBindingListResponse",
    "SurfaceBindingLookupRequest",
    "SurfaceBindingResponse",
    "SurfaceBindingUpsertRequest",
]
