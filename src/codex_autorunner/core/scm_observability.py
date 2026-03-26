from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from .orchestration.sqlite import open_orchestration_sqlite
from .pr_bindings import PrBinding
from .publish_journal import PublishOperation
from .scm_events import ScmEvent
from .scm_reaction_types import ReactionIntent
from .time_utils import now_iso

SCM_AUDIT_INGEST = "scm.ingest"
SCM_AUDIT_BINDING_RESOLVED = "scm.binding_resolved"
SCM_AUDIT_ROUTED_INTENT = "scm.routed_intent"
SCM_AUDIT_PUBLISH_CREATED = "scm.publish_created"
SCM_AUDIT_PUBLISH_FINISHED = "scm.publish_finished"

_CORRELATION_HEADER_KEYS = (
    "x-correlation-id",
    "x-request-id",
)
_CORRELATION_PAYLOAD_KEYS = (
    "correlation_id",
    "request_id",
)


def _normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _compact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None or value == {} or value == []:
            continue
        if isinstance(value, Mapping):
            nested = _compact_payload(value)
            if nested:
                compact[key] = nested
            continue
        if isinstance(value, (list, tuple)):
            items = [item for item in value if item is not None]
            if items:
                compact[key] = items
            continue
        compact[key] = value
    return compact


def _correlation_from_headers(headers: Mapping[str, Any] | None) -> Optional[str]:
    normalized_headers = {
        str(key).lower(): value
        for key, value in (headers or {}).items()
        if key is not None
    }
    for key in _CORRELATION_HEADER_KEYS:
        candidate = _normalize_text(normalized_headers.get(key))
        if candidate is not None:
            return candidate
    return None


def _correlation_from_payload(payload: Mapping[str, Any] | None) -> Optional[str]:
    mapping = _mapping(payload)
    for key in _CORRELATION_PAYLOAD_KEYS:
        candidate = _normalize_text(mapping.get(key))
        if candidate is not None:
            return candidate
    metadata = _mapping(mapping.get("metadata"))
    scm = _mapping(metadata.get("scm"))
    candidate = _normalize_text(scm.get("correlation_id"))
    if candidate is not None:
        return candidate
    tracking = _mapping(mapping.get("scm_reaction"))
    return _normalize_text(tracking.get("correlation_id"))


def correlation_id_from_payload(payload: Mapping[str, Any] | None) -> Optional[str]:
    return _correlation_from_payload(payload)


def create_or_preserve_correlation_id(
    *,
    provider: str,
    event_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    headers: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> str:
    existing = _normalize_text(correlation_id)
    if existing is not None:
        return existing
    header_value = _correlation_from_headers(headers)
    if header_value is not None:
        return header_value
    payload_value = _correlation_from_payload(payload)
    if payload_value is not None:
        return payload_value
    normalized_event_id = _normalize_text(event_id)
    if normalized_event_id is not None:
        return f"scm:{normalized_event_id}"
    return f"scm:{provider}:{uuid.uuid4().hex[:12]}"


def correlation_id_for_event(event: ScmEvent) -> str:
    return create_or_preserve_correlation_id(
        provider=event.provider,
        event_id=event.event_id,
        correlation_id=event.correlation_id,
        payload=event.payload,
    )


def correlation_id_for_operation(operation: PublishOperation) -> Optional[str]:
    return _correlation_from_payload(operation.payload)


def with_correlation_id(
    payload: Mapping[str, Any] | None,
    *,
    correlation_id: str,
) -> dict[str, Any]:
    hydrated = dict(_mapping(payload))
    hydrated["correlation_id"] = correlation_id
    return hydrated


def audit_payload_for_event(event: ScmEvent) -> dict[str, Any]:
    return _compact_payload(
        {
            "correlation_id": correlation_id_for_event(event),
            "event_id": event.event_id,
            "provider": event.provider,
            "event_type": event.event_type,
            "repo_slug": event.repo_slug,
            "repo_id": event.repo_id,
            "pr_number": event.pr_number,
            "delivery_id": event.delivery_id,
        }
    )


class ScmAuditRecorder:
    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def record(
        self,
        *,
        action_type: str,
        correlation_id: str,
        event: Optional[ScmEvent] = None,
        binding: Optional[PrBinding] = None,
        intent: Optional[ReactionIntent] = None,
        operation: Optional[PublishOperation] = None,
        payload: Mapping[str, Any] | None = None,
    ) -> str:
        record_payload = _compact_payload(
            {
                "correlation_id": correlation_id,
                **(audit_payload_for_event(event) if event is not None else {}),
                "binding_id": binding.binding_id if binding is not None else None,
                "thread_target_id": (
                    binding.thread_target_id if binding is not None else None
                ),
                "reaction_kind": (intent.reaction_kind if intent is not None else None),
                "operation_kind": (
                    operation.operation_kind
                    if operation is not None
                    else intent.operation_kind if intent is not None else None
                ),
                "operation_key": (
                    operation.operation_key
                    if operation is not None
                    else intent.operation_key if intent is not None else None
                ),
                "operation_id": (
                    operation.operation_id if operation is not None else None
                ),
                "operation_state": operation.state if operation is not None else None,
                "attempt_count": (
                    operation.attempt_count if operation is not None else None
                ),
                "last_error_text": (
                    operation.last_error_text if operation is not None else None
                ),
                "response_keys": (
                    sorted(operation.response.keys())
                    if operation is not None and isinstance(operation.response, dict)
                    else None
                ),
                **_compact_payload(payload or {}),
            }
        )
        target_kind, target_id = self._resolve_target(
            event=event,
            binding=binding,
            operation=operation,
        )
        repo_id = self._resolve_repo_id(
            event=event, binding=binding, payload=record_payload
        )
        fingerprint = self._fingerprint(
            action_type=action_type,
            correlation_id=correlation_id,
            target_kind=target_kind,
            target_id=target_id,
            payload=record_payload,
        )
        audit_id = f"{action_type}:{uuid.uuid4().hex}"
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            conn.execute(
                """
                INSERT INTO orch_audit_entries (
                    audit_id,
                    action_type,
                    actor_kind,
                    actor_id,
                    target_kind,
                    target_id,
                    repo_id,
                    payload_json,
                    created_at,
                    fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    action_type,
                    "scm_provider" if event is not None else None,
                    event.provider if event is not None else None,
                    target_kind,
                    target_id,
                    repo_id,
                    _json_dumps(record_payload),
                    now_iso(),
                    fingerprint,
                ),
            )
        return audit_id

    @staticmethod
    def _resolve_target(
        *,
        event: Optional[ScmEvent],
        binding: Optional[PrBinding],
        operation: Optional[PublishOperation],
    ) -> tuple[Optional[str], Optional[str]]:
        if operation is not None:
            return "publish_operation", operation.operation_id
        if binding is not None:
            return "pr_binding", binding.binding_id
        if event is not None:
            return "scm_event", event.event_id
        return None, None

    @staticmethod
    def _resolve_repo_id(
        *,
        event: Optional[ScmEvent],
        binding: Optional[PrBinding],
        payload: Mapping[str, Any],
    ) -> Optional[str]:
        return (
            _normalize_text(payload.get("repo_id"))
            or (binding.repo_id if binding is not None else None)
            or (event.repo_id if event is not None else None)
        )

    @staticmethod
    def _fingerprint(
        *,
        action_type: str,
        correlation_id: str,
        target_kind: Optional[str],
        target_id: Optional[str],
        payload: Mapping[str, Any],
    ) -> str:
        encoded = _json_dumps(
            {
                "action_type": action_type,
                "correlation_id": correlation_id,
                "payload": payload,
                "target_id": target_id,
                "target_kind": target_kind,
            }
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "SCM_AUDIT_BINDING_RESOLVED",
    "SCM_AUDIT_INGEST",
    "SCM_AUDIT_PUBLISH_CREATED",
    "SCM_AUDIT_PUBLISH_FINISHED",
    "SCM_AUDIT_ROUTED_INTENT",
    "ScmAuditRecorder",
    "audit_payload_for_event",
    "correlation_id_for_event",
    "correlation_id_from_payload",
    "correlation_id_for_operation",
    "create_or_preserve_correlation_id",
    "with_correlation_id",
]
