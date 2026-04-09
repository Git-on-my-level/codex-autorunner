from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .git_utils import git_branch
from .managed_thread_status import ManagedThreadStatusSnapshot
from .orchestration.models import (
    ExecutionRecord,
    ThreadTarget,
    normalize_resource_owner_fields,
)
from .text_utils import _json_loads_object


def row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def coerce_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def normalize_request_kind(value: Any) -> str:
    normalized = (coerce_text(value) or "").lower()
    if normalized == "review":
        return "review"
    return "message"


def sanitize_thread_metadata(metadata: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(metadata or {})
    payload.pop("backend_runtime_instance_id", None)
    return payload


def workspace_head_branch(workspace_root: Path) -> Optional[str]:
    return coerce_text(git_branch(workspace_root))


def enrich_thread_metadata_for_workspace(
    metadata: Optional[dict[str, Any]],
    *,
    workspace_root: Path,
) -> dict[str, Any]:
    payload = sanitize_thread_metadata(metadata)
    if coerce_text(payload.get("head_branch")) is None:
        head_branch = workspace_head_branch(workspace_root)
        if head_branch is not None:
            payload["head_branch"] = head_branch
    return payload


@dataclass(frozen=True)
class PmaThreadRecord:
    managed_thread_id: str
    agent: str
    repo_id: Optional[str]
    resource_kind: Optional[str]
    resource_id: Optional[str]
    workspace_root: str
    name: Optional[str]
    status: str
    lifecycle_status: str
    normalized_status: str
    status_reason_code: Optional[str]
    status_reason: Optional[str]
    status_updated_at: Optional[str]
    status_changed_at: Optional[str]
    status_terminal: bool
    status_turn_id: Optional[str]
    last_turn_id: Optional[str]
    last_message_preview: Optional[str]
    compact_seed: Optional[str]
    metadata: dict[str, Any]
    created_at: Optional[str]
    updated_at: Optional[str]

    @classmethod
    def from_store_mapping(cls, data: Mapping[str, Any]) -> "PmaThreadRecord":
        record = dict(data)
        lifecycle_status = (
            coerce_text(record.get("lifecycle_status") or record.get("status"))
            or "active"
        )
        record["status"] = lifecycle_status
        record["lifecycle_status"] = lifecycle_status
        snapshot = ManagedThreadStatusSnapshot.from_mapping(record)
        raw_metadata = record.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        if not metadata and "metadata_json" in record:
            metadata = _json_loads_object(record.get("metadata_json"))
        resource_kind, resource_id, repo_id = normalize_resource_owner_fields(
            resource_kind=record.get("resource_kind"),
            resource_id=record.get("resource_id"),
            repo_id=record.get("repo_id"),
        )
        managed_thread_id = coerce_text(
            record.get("managed_thread_id") or record.get("thread_target_id")
        )
        if managed_thread_id is None:
            raise ValueError("PmaThreadRecord requires a managed_thread_id")
        agent = coerce_text(record.get("agent") or record.get("agent_id")) or "unknown"
        workspace_root = coerce_text(record.get("workspace_root")) or ""
        return cls(
            managed_thread_id=managed_thread_id,
            agent=agent,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_root=workspace_root,
            name=coerce_text(record.get("name") or record.get("display_name")),
            status=lifecycle_status,
            lifecycle_status=lifecycle_status,
            normalized_status=snapshot.status,
            status_reason_code=snapshot.reason_code,
            status_reason=snapshot.reason_code,
            status_updated_at=snapshot.changed_at,
            status_changed_at=snapshot.changed_at,
            status_terminal=bool(snapshot.terminal),
            status_turn_id=coerce_text(record.get("status_turn_id"))
            or snapshot.turn_id,
            last_turn_id=coerce_text(
                record.get("last_turn_id") or record.get("last_execution_id")
            ),
            last_message_preview=coerce_text(record.get("last_message_preview")),
            compact_seed=coerce_text(record.get("compact_seed")),
            metadata=metadata,
            created_at=coerce_text(record.get("created_at")),
            updated_at=coerce_text(record.get("updated_at")),
        )

    @classmethod
    def from_orchestration_row(cls, row: Any) -> "PmaThreadRecord":
        metadata = (
            _json_loads_object(row["metadata_json"])
            if "metadata_json" in row.keys()
            else {}
        )
        resource_kind, resource_id, repo_id = normalize_resource_owner_fields(
            resource_kind=row["resource_kind"],
            resource_id=row["resource_id"],
            repo_id=row["repo_id"],
        )
        return cls.from_store_mapping(
            {
                "managed_thread_id": row["thread_target_id"],
                "agent": row["agent_id"],
                "repo_id": repo_id,
                "resource_kind": resource_kind,
                "resource_id": resource_id,
                "workspace_root": row["workspace_root"],
                "name": row["display_name"],
                "status": row["lifecycle_status"] or "active",
                "normalized_status": row["runtime_status"] or "idle",
                "status_reason_code": row["status_reason"],
                "status_updated_at": row["status_updated_at"] or row["updated_at"],
                "status_terminal": int(row["status_terminal"] or 0),
                "status_turn_id": row["status_turn_id"],
                "last_turn_id": row["last_execution_id"],
                "last_message_preview": row["last_message_preview"],
                "compact_seed": row["compact_seed"],
                "metadata": metadata,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_thread_target(self) -> ThreadTarget:
        return ThreadTarget.from_mapping(self.to_dict())


@dataclass(frozen=True)
class PmaExecutionRecord:
    managed_turn_id: str
    managed_thread_id: str
    client_turn_id: Optional[str]
    request_kind: str
    backend_turn_id: Optional[str]
    prompt: str
    status: str
    assistant_text: Optional[str]
    transcript_turn_id: Optional[str]
    model: Optional[str]
    reasoning: Optional[str]
    error: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]

    @classmethod
    def from_orchestration_row(cls, row: Any) -> "PmaExecutionRecord":
        return cls(
            managed_turn_id=str(row["execution_id"]),
            managed_thread_id=str(row["thread_target_id"]),
            client_turn_id=coerce_text(row["client_request_id"]),
            request_kind=normalize_request_kind(row["request_kind"]),
            backend_turn_id=coerce_text(row["backend_turn_id"]),
            prompt=str(row["prompt_text"]),
            status=str(row["status"]),
            assistant_text=coerce_text(row["assistant_text"]),
            transcript_turn_id=coerce_text(row["transcript_mirror_id"]),
            model=coerce_text(row["model_id"]),
            reasoning=coerce_text(row["reasoning_level"]),
            error=coerce_text(row["error_text"]),
            started_at=coerce_text(row["started_at"]),
            finished_at=coerce_text(row["finished_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_execution_record(self) -> ExecutionRecord:
        return ExecutionRecord.from_mapping(self.to_dict())


@dataclass(frozen=True)
class PmaPendingQueueItem:
    queue_item_id: str
    state: str
    visible_at: Optional[str]
    enqueued_at: Optional[str]
    managed_turn_id: str
    request_kind: str
    prompt: str
    model: Optional[str]
    reasoning: Optional[str]
    client_turn_id: Optional[str]

    @classmethod
    def from_queue_row(cls, row: Any) -> "PmaPendingQueueItem":
        return cls(
            queue_item_id=str(row["queue_item_id"]),
            state=str(row["state"]),
            visible_at=coerce_text(row["visible_at"]),
            enqueued_at=coerce_text(row["created_at"]),
            managed_turn_id=str(row["execution_id"]),
            request_kind=normalize_request_kind(row["request_kind"]),
            prompt=str(row["prompt_text"] or ""),
            model=coerce_text(row["model_id"]),
            reasoning=coerce_text(row["reasoning_level"]),
            client_turn_id=coerce_text(row["client_request_id"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "PmaExecutionRecord",
    "PmaPendingQueueItem",
    "PmaThreadRecord",
    "coerce_text",
    "enrich_thread_metadata_for_workspace",
    "normalize_request_kind",
    "row_to_dict",
    "sanitize_thread_metadata",
    "workspace_head_branch",
]
