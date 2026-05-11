from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Optional

from ..hub_projection_store import HubProjectionStore
from ..logging_utils import log_event
from ..text_utils import _normalize_optional_text
from .chat_surface_events import (
    ChatSurfaceEventAppendResult,
    ChatSurfaceEventType,
    SQLiteChatSurfaceEventJournal,
)
from .models import Binding

logger = logging.getLogger(__name__)


def emit_chat_surface_event(
    hub_root: Path,
    *,
    durable: bool = True,
    idempotency_key: str,
    event_type: ChatSurfaceEventType,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: Optional[str] = None,
    external_conversation_id: Optional[str] = None,
    repo_id: Optional[str] = None,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    workspace_root: Optional[str] = None,
    lifecycle_status: Optional[str] = None,
    status: Optional[str] = None,
    source_kind: Optional[str] = None,
    source_id: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    occurred_at: Optional[str] = None,
) -> ChatSurfaceEventAppendResult:
    """Append a chat-surface journal event and log failures before re-raising."""

    try:
        result = SQLiteChatSurfaceEventJournal(
            Path(hub_root), durable=durable
        ).append_event(
            idempotency_key=idempotency_key,
            event_type=event_type,
            surface_kind=surface_kind,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            external_conversation_id=external_conversation_id,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_root=workspace_root,
            lifecycle_status=lifecycle_status,
            status=status,
            source_kind=source_kind,
            source_id=source_id,
            payload=payload,
            occurred_at=occurred_at,
        )
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "chat_surface.event_emit_failed",
            event_type=str(event_type),
            surface_kind=str(surface_kind),
            surface_key=str(surface_key),
            managed_thread_id=managed_thread_id,
            source_kind=source_kind,
            source_id=source_id,
            idempotency_key=str(idempotency_key),
            error=str(exc),
        )
        raise
    log_event(
        logger,
        logging.DEBUG,
        "chat_surface.event_emitted",
        event_type=result.event.event_type,
        surface_kind=result.event.surface_kind,
        surface_key=result.event.surface_key,
        managed_thread_id=result.event.managed_thread_id,
        source_kind=result.event.source_kind,
        source_id=result.event.source_id,
        idempotency_key=result.event.idempotency_key,
        inserted=result.inserted,
        cursor=result.event.cursor,
    )
    try:
        HubProjectionStore(Path(hub_root), durable=durable).append_event(
            idempotency_key=f"chat_surface:{result.event.cursor}",
            entity_kind="chat_surface",
            entity_id=f"{result.event.surface_kind}:{result.event.surface_key}",
            operation=_hub_operation_from_chat_surface_event(result.event.event_type),
            payload=result.event.to_dict(),
            source_revision=f"chat_surface:{result.event.cursor}",
            generated_at=result.event.created_at,
        )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "chat_surface.hub_ui_event_emit_failed",
            event_type=result.event.event_type,
            surface_kind=result.event.surface_kind,
            surface_key=result.event.surface_key,
            source_revision=f"chat_surface:{result.event.cursor}",
            error=str(exc),
        )
    return result


def emit_binding_event(
    hub_root: Path,
    binding: Binding,
    *,
    durable: bool = True,
    event_type: ChatSurfaceEventType,
    idempotency_action: str,
    status: Optional[str] = None,
    lifecycle_status: Optional[str] = "active",
    source_kind: str = "orchestration.binding",
    payload: Optional[Mapping[str, Any]] = None,
) -> ChatSurfaceEventAppendResult:
    return emit_chat_surface_event(
        hub_root,
        durable=durable,
        idempotency_key=(f"binding:{binding.binding_id}:{idempotency_action}"),
        event_type=event_type,
        surface_kind=binding.surface_kind,
        surface_key=binding.surface_key,
        managed_thread_id=binding.thread_target_id,
        repo_id=binding.repo_id,
        resource_kind=binding.resource_kind,
        resource_id=binding.resource_id,
        lifecycle_status=lifecycle_status,
        status=status,
        source_kind=source_kind,
        source_id=binding.binding_id,
        payload={
            "binding": asdict(binding),
            **dict(payload or {}),
        },
    )


def pma_surface_key(managed_thread_id: Any) -> Optional[str]:
    normalized = _normalize_optional_text(managed_thread_id)
    return normalized


def _hub_operation_from_chat_surface_event(event_type: str) -> str:
    if event_type == "surface.archived":
        return "delete"
    if event_type in {
        "surface.bound",
        "surface.rebound",
        "channel_directory.discovered",
    }:
        return "upsert"
    return "patch"


__all__ = [
    "emit_binding_event",
    "emit_chat_surface_event",
    "pma_surface_key",
]
