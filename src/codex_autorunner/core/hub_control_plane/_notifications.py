from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ._normalizers import (
    copy_mapping,
    normalize_optional_text,
    normalize_required_identifier,
    normalize_required_text,
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
            notification_id=normalize_required_text(
                data.get("notification_id"),
                field_name="notification_id",
            ),
            correlation_id=normalize_required_text(
                data.get("correlation_id"),
                field_name="correlation_id",
            ),
            source_kind=normalize_required_text(
                data.get("source_kind"),
                field_name="source_kind",
            ),
            delivery_mode=normalize_required_text(
                data.get("delivery_mode"),
                field_name="delivery_mode",
            ),
            surface_kind=normalize_required_text(
                data.get("surface_kind"),
                field_name="surface_kind",
            ),
            surface_key=normalize_required_text(
                data.get("surface_key"),
                field_name="surface_key",
            ),
            delivery_record_id=normalize_required_text(
                data.get("delivery_record_id"),
                field_name="delivery_record_id",
            ),
            delivered_message_id=normalize_optional_text(
                data.get("delivered_message_id")
            ),
            repo_id=normalize_optional_text(data.get("repo_id")),
            workspace_root=normalize_optional_text(data.get("workspace_root")),
            run_id=normalize_optional_text(data.get("run_id")),
            managed_thread_id=normalize_optional_text(data.get("managed_thread_id")),
            continuation_thread_target_id=normalize_optional_text(
                data.get("continuation_thread_target_id")
            ),
            context=copy_mapping(data.get("context")),
            created_at=normalize_optional_text(data.get("created_at")),
            updated_at=normalize_optional_text(data.get("updated_at")),
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
            notification_id=normalize_required_text(
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
            surface_kind=normalize_required_text(
                data.get("surface_kind"),
                field_name="surface_kind",
            ),
            surface_key=normalize_required_text(
                data.get("surface_key"),
                field_name="surface_key",
            ),
            delivered_message_id=normalize_required_identifier(
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
            delivery_record_id=normalize_required_text(
                data.get("delivery_record_id"),
                field_name="delivery_record_id",
            ),
            delivered_message_id=normalize_required_identifier(
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
            notification_id=normalize_required_text(
                data.get("notification_id"),
                field_name="notification_id",
            ),
            thread_target_id=normalize_required_text(
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


__all__ = [
    "NotificationContinuationBindRequest",
    "NotificationDeliveryMarkRequest",
    "NotificationLookupRequest",
    "NotificationRecord",
    "NotificationRecordResponse",
    "NotificationReplyTargetLookupRequest",
]
