from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..orchestration.models import BusyThreadPolicy, ExecutionRecord, MessageRequestKind
from ._normalizers import (
    coerce_int,
    copy_mapping,
    normalize_busy_thread_policy,
    normalize_message_request_kind,
    normalize_optional_text,
    normalize_required_text,
    normalize_run_event_payloads,
)


@dataclass(frozen=True)
class ExecutionCreateRequest:
    thread_target_id: str
    prompt: str
    request_kind: MessageRequestKind = "message"
    busy_policy: BusyThreadPolicy = "reject"
    model: Optional[str] = None
    reasoning: Optional[str] = None
    client_request_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    queue_payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionCreateRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            prompt=normalize_required_text(
                data.get("prompt"),
                field_name="prompt",
            ),
            request_kind=normalize_message_request_kind(data.get("request_kind")),
            busy_policy=normalize_busy_thread_policy(data.get("busy_policy")),
            model=normalize_optional_text(data.get("model")),
            reasoning=normalize_optional_text(data.get("reasoning")),
            client_request_id=normalize_optional_text(data.get("client_request_id")),
            metadata=copy_mapping(data.get("metadata")),
            queue_payload=copy_mapping(data.get("queue_payload")),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "thread_target_id": self.thread_target_id,
            "prompt": self.prompt,
            "request_kind": self.request_kind,
            "busy_policy": self.busy_policy,
            "model": self.model,
            "reasoning": self.reasoning,
            "client_request_id": self.client_request_id,
            "queue_payload": dict(self.queue_payload),
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class ExecutionLookupRequest:
    thread_target_id: str
    execution_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionLookupRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "execution_id": self.execution_id,
        }


@dataclass(frozen=True)
class RunningExecutionLookupRequest:
    thread_target_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RunningExecutionLookupRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"thread_target_id": self.thread_target_id}


@dataclass(frozen=True)
class RunningThreadTargetIdsRequest:
    limit: Optional[int] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RunningThreadTargetIdsRequest":
        if "limit" not in data:
            return cls(limit=None)
        limit_value = data.get("limit")
        if limit_value is None:
            return cls(limit=None)
        return cls(limit=coerce_int(limit_value, field_name="limit"))

    def to_dict(self) -> dict[str, Any]:
        return {"limit": self.limit}


@dataclass(frozen=True)
class RunningThreadTargetIdsResponse:
    thread_target_ids: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RunningThreadTargetIdsResponse":
        raw = data.get("thread_target_ids", data.get("threadTargetIds", ()))
        if not isinstance(raw, (list, tuple)):
            return cls(thread_target_ids=())
        normalized: list[str] = []
        for item in raw:
            tid = normalize_optional_text(item)
            if tid:
                normalized.append(tid)
        return cls(thread_target_ids=tuple(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {"thread_target_ids": list(self.thread_target_ids)}


@dataclass(frozen=True)
class LatestExecutionLookupRequest:
    thread_target_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "LatestExecutionLookupRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"thread_target_id": self.thread_target_id}


@dataclass(frozen=True)
class QueuedExecutionListRequest:
    thread_target_id: str
    limit: int = 200

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "QueuedExecutionListRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            limit=max(1, coerce_int(data.get("limit", 200), field_name="limit")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class QueueDepthRequest:
    thread_target_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "QueueDepthRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"thread_target_id": self.thread_target_id}


@dataclass(frozen=True)
class ExecutionCancelRequest:
    thread_target_id: str
    execution_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionCancelRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "execution_id": self.execution_id,
        }


@dataclass(frozen=True)
class ExecutionPromoteRequest:
    thread_target_id: str
    execution_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionPromoteRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "execution_id": self.execution_id,
        }


@dataclass(frozen=True)
class ExecutionResultRecordRequest:
    thread_target_id: str
    execution_id: str
    status: str
    assistant_text: Optional[str] = None
    error: Optional[str] = None
    backend_turn_id: Optional[str] = None
    transcript_turn_id: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionResultRecordRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
            status=normalize_required_text(
                data.get("status"),
                field_name="status",
            ),
            assistant_text=normalize_optional_text(data.get("assistant_text")),
            error=normalize_optional_text(data.get("error")),
            backend_turn_id=normalize_optional_text(data.get("backend_turn_id")),
            transcript_turn_id=normalize_optional_text(data.get("transcript_turn_id")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "execution_id": self.execution_id,
            "status": self.status,
            "assistant_text": self.assistant_text,
            "error": self.error,
            "backend_turn_id": self.backend_turn_id,
            "transcript_turn_id": self.transcript_turn_id,
        }


@dataclass(frozen=True)
class ExecutionInterruptRecordRequest:
    thread_target_id: str
    execution_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionInterruptRecordRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "execution_id": self.execution_id,
        }


@dataclass(frozen=True)
class ExecutionCancelAllRequest:
    thread_target_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionCancelAllRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"thread_target_id": self.thread_target_id}


@dataclass(frozen=True)
class ExecutionBackendIdUpdateRequest:
    execution_id: str
    backend_turn_id: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionBackendIdUpdateRequest":
        return cls(
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
            backend_turn_id=normalize_optional_text(
                data.get("backend_turn_id") or data.get("backend_id")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "backend_turn_id": self.backend_turn_id,
        }


@dataclass(frozen=True)
class ExecutionClaimNextRequest:
    thread_target_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionClaimNextRequest":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"thread_target_id": self.thread_target_id}


@dataclass(frozen=True)
class ExecutionTimelinePersistRequest:
    execution_id: str
    target_kind: Optional[str] = None
    target_id: Optional[str] = None
    repo_id: Optional[str] = None
    run_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    start_index: int = 1

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionTimelinePersistRequest":
        return cls(
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
            target_kind=normalize_optional_text(data.get("target_kind")),
            target_id=normalize_optional_text(data.get("target_id")),
            repo_id=normalize_optional_text(data.get("repo_id")),
            run_id=normalize_optional_text(data.get("run_id")),
            resource_kind=normalize_optional_text(data.get("resource_kind")),
            resource_id=normalize_optional_text(data.get("resource_id")),
            metadata=copy_mapping(data.get("metadata")),
            events=normalize_run_event_payloads(data.get("events")),
            start_index=max(
                1,
                coerce_int(data.get("start_index", 1), field_name="start_index"),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "repo_id": self.repo_id,
            "run_id": self.run_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "metadata": dict(self.metadata),
            "events": [dict(event) for event in self.events],
            "start_index": self.start_index,
        }


@dataclass(frozen=True)
class ExecutionTimelinePersistResponse:
    execution_id: str
    persisted_event_count: int = 0

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any]
    ) -> "ExecutionTimelinePersistResponse":
        return cls(
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
            persisted_event_count=coerce_int(
                data.get("persisted_event_count", 0),
                field_name="persisted_event_count",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "persisted_event_count": self.persisted_event_count,
        }


@dataclass(frozen=True)
class ExecutionColdTraceFinalizeRequest:
    execution_id: str
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    backend_thread_id: Optional[str] = None
    backend_turn_id: Optional[str] = None

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any]
    ) -> "ExecutionColdTraceFinalizeRequest":
        return cls(
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
            events=normalize_run_event_payloads(data.get("events")),
            backend_thread_id=normalize_optional_text(data.get("backend_thread_id")),
            backend_turn_id=normalize_optional_text(data.get("backend_turn_id")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "events": [dict(event) for event in self.events],
            "backend_thread_id": self.backend_thread_id,
            "backend_turn_id": self.backend_turn_id,
        }


@dataclass(frozen=True)
class ExecutionColdTraceFinalizeResponse:
    execution_id: str
    trace_manifest_id: Optional[str] = None

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any]
    ) -> "ExecutionColdTraceFinalizeResponse":
        return cls(
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
            trace_manifest_id=normalize_optional_text(data.get("trace_manifest_id")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "trace_manifest_id": self.trace_manifest_id,
        }


@dataclass(frozen=True)
class ExecutionResponse:
    execution: Optional[ExecutionRecord]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionResponse":
        raw_execution = data.get("execution")
        execution = (
            ExecutionRecord.from_mapping(raw_execution)
            if isinstance(raw_execution, Mapping)
            else None
        )
        return cls(execution=execution)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution": (None if self.execution is None else self.execution.to_dict())
        }


@dataclass(frozen=True)
class ExecutionListResponse:
    executions: tuple[ExecutionRecord, ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionListResponse":
        raw_executions = data.get("executions")
        if not isinstance(raw_executions, list):
            return cls(executions=())
        return cls(
            executions=tuple(
                ExecutionRecord.from_mapping(item)
                for item in raw_executions
                if isinstance(item, Mapping)
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"executions": [execution.to_dict() for execution in self.executions]}


@dataclass(frozen=True)
class QueueDepthResponse:
    thread_target_id: str
    queue_depth: int

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "QueueDepthResponse":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            queue_depth=coerce_int(
                data.get("queue_depth"),
                field_name="queue_depth",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "queue_depth": self.queue_depth,
        }


@dataclass(frozen=True)
class ExecutionCancelResponse:
    thread_target_id: str
    execution_id: str
    cancelled: bool

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionCancelResponse":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
            cancelled=bool(data.get("cancelled")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "execution_id": self.execution_id,
            "cancelled": self.cancelled,
        }


@dataclass(frozen=True)
class ExecutionPromoteResponse:
    thread_target_id: str
    execution_id: str
    promoted: bool

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionPromoteResponse":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            execution_id=normalize_required_text(
                data.get("execution_id"),
                field_name="execution_id",
            ),
            promoted=bool(data.get("promoted")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "execution_id": self.execution_id,
            "promoted": self.promoted,
        }


@dataclass(frozen=True)
class ExecutionCancelAllResponse:
    thread_target_id: str
    cancelled_count: int

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionCancelAllResponse":
        return cls(
            thread_target_id=normalize_required_text(
                data.get("thread_target_id"),
                field_name="thread_target_id",
            ),
            cancelled_count=coerce_int(
                data.get("cancelled_count"),
                field_name="cancelled_count",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_target_id": self.thread_target_id,
            "cancelled_count": self.cancelled_count,
        }


@dataclass(frozen=True)
class ExecutionClaimNextResponse:
    execution: Optional[ExecutionRecord]
    queue_payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ExecutionClaimNextResponse":
        raw_execution = data.get("execution")
        execution = (
            ExecutionRecord.from_mapping(raw_execution)
            if isinstance(raw_execution, Mapping)
            else None
        )
        return cls(
            execution=execution,
            queue_payload=copy_mapping(data.get("queue_payload")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution": (None if self.execution is None else self.execution.to_dict()),
            "queue_payload": dict(self.queue_payload),
        }


__all__ = [
    "ExecutionBackendIdUpdateRequest",
    "ExecutionCancelAllRequest",
    "ExecutionCancelAllResponse",
    "ExecutionCancelRequest",
    "ExecutionCancelResponse",
    "ExecutionClaimNextRequest",
    "ExecutionClaimNextResponse",
    "ExecutionColdTraceFinalizeRequest",
    "ExecutionColdTraceFinalizeResponse",
    "ExecutionCreateRequest",
    "ExecutionInterruptRecordRequest",
    "ExecutionListResponse",
    "ExecutionLookupRequest",
    "ExecutionPromoteRequest",
    "ExecutionPromoteResponse",
    "ExecutionResponse",
    "ExecutionResultRecordRequest",
    "ExecutionTimelinePersistRequest",
    "ExecutionTimelinePersistResponse",
    "LatestExecutionLookupRequest",
    "QueueDepthRequest",
    "QueueDepthResponse",
    "QueuedExecutionListRequest",
    "RunningExecutionLookupRequest",
    "RunningThreadTargetIdsRequest",
    "RunningThreadTargetIdsResponse",
]
