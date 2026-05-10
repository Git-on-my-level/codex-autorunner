from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ._normalizers import (
    coerce_int,
    copy_mapping,
    normalize_optional_text,
    normalize_required_text,
)


@dataclass(frozen=True)
class PmaSnapshotResponse:
    snapshot: dict[str, Any]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PmaSnapshotResponse":
        raw_snapshot = data.get("snapshot")
        if isinstance(raw_snapshot, Mapping):
            return cls(snapshot=dict(raw_snapshot))
        return cls(snapshot={})

    def to_dict(self) -> dict[str, Any]:
        return {"snapshot": dict(self.snapshot)}


@dataclass(frozen=True)
class TranscriptWriteRequest:
    turn_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    assistant_text: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TranscriptWriteRequest":
        return cls(
            turn_id=normalize_required_text(
                data.get("turn_id"),
                field_name="turn_id",
            ),
            metadata=copy_mapping(data.get("metadata")),
            assistant_text=str(data.get("assistant_text") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "metadata": dict(self.metadata),
            "assistant_text": self.assistant_text,
        }


@dataclass(frozen=True)
class TranscriptWriteResponse:
    turn_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TranscriptWriteResponse":
        return cls(
            turn_id=normalize_required_text(
                data.get("turn_id"),
                field_name="turn_id",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"turn_id": self.turn_id}


@dataclass(frozen=True)
class TranscriptHistoryRequest:
    target_kind: str
    target_id: str
    limit: int = 10

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TranscriptHistoryRequest":
        return cls(
            target_kind=normalize_required_text(
                data.get("target_kind"),
                field_name="target_kind",
            ),
            target_id=normalize_required_text(
                data.get("target_id"),
                field_name="target_id",
            ),
            limit=max(0, coerce_int(data.get("limit", 10), field_name="limit")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class TranscriptHistoryResponse:
    entries: tuple[dict[str, Any], ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TranscriptHistoryResponse":
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, list):
            return cls(entries=())
        entries: list[dict[str, Any]] = []
        for item in raw_entries:
            if isinstance(item, Mapping):
                entries.append(dict(item))
        return cls(entries=tuple(entries))

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [dict(e) for e in self.entries]}


@dataclass(frozen=True)
class WorkspaceSetupCommandRequest:
    workspace_root: str
    repo_id_hint: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "WorkspaceSetupCommandRequest":
        return cls(
            workspace_root=normalize_required_text(
                data.get("workspace_root"),
                field_name="workspace_root",
            ),
            repo_id_hint=normalize_optional_text(data.get("repo_id_hint")),
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
            workspace_root=normalize_required_text(
                data.get("workspace_root"),
                field_name="workspace_root",
            ),
            repo_id_hint=normalize_optional_text(data.get("repo_id_hint")),
            setup_command_count=coerce_int(
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
            operation=normalize_required_text(
                data.get("operation"),
                field_name="operation",
            ),
            surface_kind=normalize_optional_text(data.get("surface_kind")),
            surface_key=normalize_optional_text(data.get("surface_key")),
            thread_target_id=normalize_optional_text(data.get("thread_target_id")),
            payload=copy_mapping(data.get("payload")),
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
            operation=normalize_required_text(
                data.get("operation"),
                field_name="operation",
            ),
            accepted=bool(data.get("accepted")),
            payload=copy_mapping(data.get("payload")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "accepted": self.accepted,
            "payload": dict(self.payload),
        }


__all__ = [
    "AutomationRequest",
    "AutomationResult",
    "PmaSnapshotResponse",
    "TranscriptHistoryRequest",
    "TranscriptHistoryResponse",
    "TranscriptWriteRequest",
    "TranscriptWriteResponse",
    "WorkspaceSetupCommandRequest",
    "WorkspaceSetupCommandResult",
]
