from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..orchestration.models import ThreadTarget
from ._normalizers import (
    coerce_int,
    copy_mapping,
    normalize_optional_text,
    normalize_required_text,
)

THREAD_TARGET_LIST_LIFECYCLE_STATUSES = frozenset({"active", "archived"})


def resolve_thread_target_list_status_fields(
    *,
    status: Optional[str],
    lifecycle_status: Optional[str],
    runtime_status: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Map legacy combined ``status`` into lifecycle vs runtime filters."""
    if status and lifecycle_status is None and runtime_status is None:
        if status in THREAD_TARGET_LIST_LIFECYCLE_STATUSES:
            return status, None
        return None, status
    return lifecycle_status, runtime_status


@dataclass(frozen=True)
class ThreadTargetLookupRequest:
    thread_target_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTargetLookupRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"thread_target_id": self.thread_target_id}


@dataclass(frozen=True)
class ThreadTargetListRequest:
    agent_id: Optional[str] = None
    status: Optional[str] = None
    lifecycle_status: Optional[str] = None
    runtime_status: Optional[str] = None
    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    limit: int = 200

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTargetListRequest":
        status = normalize_optional_text(data.get("status"))
        lifecycle_status = normalize_optional_text(data.get("lifecycle_status"))
        runtime_status = normalize_optional_text(data.get("runtime_status"))
        lifecycle_status, runtime_status = resolve_thread_target_list_status_fields(
            status=status,
            lifecycle_status=lifecycle_status,
            runtime_status=runtime_status,
        )
        return cls(
            agent_id=normalize_optional_text(data.get("agent_id")),
            status=status,
            lifecycle_status=lifecycle_status,
            runtime_status=runtime_status,
            repo_id=normalize_optional_text(data.get("repo_id")),
            resource_kind=normalize_optional_text(data.get("resource_kind")),
            resource_id=normalize_optional_text(data.get("resource_id")),
            limit=max(1, coerce_int(data.get("limit", 200), field_name="limit")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
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
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            backend_thread_id=normalize_optional_text(data.get("backend_thread_id")),
            backend_runtime_instance_id=normalize_optional_text(
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
            thread_target_id=normalize_required_text(
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
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            compact_seed=normalize_required_text(
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
class ThreadBackendIdUpdateRequest:
    thread_target_id: str
    backend_thread_id: Optional[str] = None
    backend_runtime_instance_id: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadBackendIdUpdateRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            backend_thread_id=normalize_optional_text(data.get("backend_thread_id")),
            backend_runtime_instance_id=normalize_optional_text(
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
class ThreadActivityRecordRequest:
    thread_target_id: str
    execution_id: Optional[str] = None
    message_preview: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadActivityRecordRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            execution_id=normalize_optional_text(data.get("execution_id")),
            message_preview=normalize_optional_text(data.get("message_preview")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "execution_id": self.execution_id,
            "message_preview": self.message_preview,
        }


@dataclass(frozen=True)
class ThreadTargetCreateRequest:
    agent_id: str
    workspace_root: str
    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    display_name: Optional[str] = None
    backend_thread_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ThreadTargetCreateRequest":
        return cls(
            agent_id=normalize_required_text(
                data.get("agent_id"),
                field_name="agent_id",
            ),
            workspace_root=normalize_required_text(
                data.get("workspace_root"),
                field_name="workspace_root",
            ),
            repo_id=normalize_optional_text(data.get("repo_id")),
            resource_kind=normalize_optional_text(data.get("resource_kind")),
            resource_id=normalize_optional_text(data.get("resource_id")),
            display_name=normalize_optional_text(data.get("display_name")),
            backend_thread_id=normalize_optional_text(data.get("backend_thread_id")),
            metadata=copy_mapping(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "workspace_root": self.workspace_root,
            "repo_id": self.repo_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "display_name": self.display_name,
            "backend_thread_id": self.backend_thread_id,
            "metadata": dict(self.metadata),
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


__all__ = [
    "THREAD_TARGET_LIST_LIFECYCLE_STATUSES",
    "ThreadActivityRecordRequest",
    "ThreadBackendIdUpdateRequest",
    "ThreadCompactSeedUpdateRequest",
    "ThreadTargetArchiveRequest",
    "ThreadTargetCreateRequest",
    "ThreadTargetListRequest",
    "ThreadTargetListResponse",
    "ThreadTargetLookupRequest",
    "ThreadTargetResponse",
    "ThreadTargetResumeRequest",
    "resolve_thread_target_list_status_fields",
]
