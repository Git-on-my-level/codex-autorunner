from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any, Optional, Protocol

from .publish_journal import PublishOperation
from .scm_observability import (
    SCM_AUDIT_PUBLISH_CREATED,
    ScmAuditRecorder,
    with_correlation_id,
)
from .text_utils import _normalize_text

_LOGGER = logging.getLogger(__name__)


class _JournalCreator(Protocol):
    def create_operation(
        self,
        *,
        operation_key: str,
        operation_kind: str,
        payload: Optional[dict[str, Any]] = None,
        next_attempt_at: Optional[str] = None,
    ) -> tuple[PublishOperation, bool]: ...


class _ReactionStateEscalator(Protocol):
    def mark_reaction_escalated(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        event_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> object: ...


def reaction_subject(tracking: Mapping[str, Any]) -> str:
    repo_slug = _normalize_text(tracking.get("repo_slug"))
    pr_number = tracking.get("pr_number")
    if repo_slug is not None and isinstance(pr_number, int):
        return f"{repo_slug}#{pr_number}"
    if repo_slug is not None:
        return repo_slug
    binding_id = _normalize_text(tracking.get("binding_id"))
    if binding_id is not None:
        return f"binding {binding_id}"
    return "SCM binding"


def reaction_kind_label(reaction_kind: str) -> str:
    return reaction_kind.replace("_", " ")


def format_failure_escalation_message(
    tracking: Mapping[str, Any],
    *,
    delivery_failure_count: int,
    last_error_text: Optional[str],
) -> str:
    subject = reaction_subject(tracking)
    label = reaction_kind_label(str(tracking.get("reaction_kind") or "reaction"))
    details = (
        f" Last error: {last_error_text}."
        if _normalize_text(last_error_text) is not None
        else ""
    )
    return (
        f"SCM automation escalation: {label} for {subject} failed delivery "
        f"{delivery_failure_count} times.{details}"
    )


def format_duplicate_escalation_message(
    tracking: Mapping[str, Any],
    *,
    attempt_count: int,
) -> str:
    subject = reaction_subject(tracking)
    label = reaction_kind_label(str(tracking.get("reaction_kind") or "reaction"))
    return (
        f"SCM automation escalation: {label} for {subject} remained active "
        f"across {attempt_count} identical deliveries. Duplicate follow-ups are suppressed."
    )


def stable_escalation_operation_key(
    *,
    binding_id: str,
    reaction_kind: str,
    fingerprint: str,
    reason: str,
) -> str:
    encoded = json.dumps(
        {
            "binding_id": binding_id,
            "reaction_kind": reaction_kind,
            "fingerprint": fingerprint,
            "reason": reason,
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return f"scm-reaction-escalation:{reason}:{digest}"


def resolve_escalation_payload(
    tracking: Mapping[str, Any],
    *,
    message: str,
) -> dict[str, Any]:
    thread_target_id = _normalize_text(tracking.get("thread_target_id"))
    repo_id = _normalize_text(tracking.get("repo_id"))
    payload: dict[str, Any] = {
        "message": message,
        "scm_reaction": dict(tracking),
    }
    if thread_target_id is not None:
        payload["thread_target_id"] = thread_target_id
        return payload
    payload["delivery"] = "primary_pma"
    if repo_id is not None:
        payload["repo_id"] = repo_id
    return payload


def create_escalation_operation(
    *,
    journal: _JournalCreator,
    reaction_state_store: _ReactionStateEscalator,
    audit_recorder: ScmAuditRecorder,
    binding_id: str,
    reaction_kind: str,
    fingerprint: str,
    tracking: Mapping[str, Any],
    message: str,
    reason: str,
    seen_operation_keys: set[str],
    event_id: Optional[str],
) -> Optional[PublishOperation]:
    operation_key = stable_escalation_operation_key(
        binding_id=binding_id,
        reaction_kind=reaction_kind,
        fingerprint=fingerprint,
        reason=reason,
    )
    if operation_key in seen_operation_keys:
        return None
    seen_operation_keys.add(operation_key)
    correlation_id = _normalize_text(tracking.get("correlation_id"))
    if correlation_id is None:
        normalized_event_id = _normalize_text(event_id)
        if normalized_event_id is not None:
            correlation_id = f"scm:{normalized_event_id}"
    payload = resolve_escalation_payload(tracking, message=message)
    if correlation_id is not None:
        payload = with_correlation_id(payload, correlation_id=correlation_id)
    operation, deduped = journal.create_operation(
        operation_key=operation_key,
        operation_kind="notify_chat",
        payload=payload,
    )
    escalation_metadata = dict(tracking)
    escalation_metadata["escalation_reason"] = reason
    if correlation_id is not None:
        try:
            audit_recorder.record(
                action_type=SCM_AUDIT_PUBLISH_CREATED,
                correlation_id=correlation_id,
                operation=operation,
                payload={
                    "deduped": deduped,
                    "escalation_reason": reason,
                },
            )
        except Exception:
            _LOGGER.warning(
                "SCM publish-created audit recording failed for %s",
                operation.operation_id,
                exc_info=True,
            )
    try:
        reaction_state_store.mark_reaction_escalated(
            binding_id=binding_id,
            reaction_kind=reaction_kind,
            fingerprint=fingerprint,
            event_id=event_id,
            operation_key=operation_key,
            metadata=escalation_metadata,
        )
    except Exception:
        _LOGGER.warning(
            "SCM escalation state update failed for operation %s",
            operation.operation_id,
            exc_info=True,
        )
    return operation


__all__ = [
    "create_escalation_operation",
    "format_duplicate_escalation_message",
    "format_failure_escalation_message",
    "reaction_kind_label",
    "reaction_subject",
    "resolve_escalation_payload",
    "stable_escalation_operation_key",
]
