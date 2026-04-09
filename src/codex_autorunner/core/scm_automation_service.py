from __future__ import annotations

import copy
import hashlib
import importlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from .config import load_hub_config
from .pr_binding_resolver import resolve_binding_for_scm_event
from .pr_bindings import PrBinding
from .publish_executor import PublishOperationProcessor
from .publish_journal import PublishJournalStore, PublishOperation
from .publish_operation_executors import (
    build_enqueue_managed_turn_executor,
    build_notify_chat_executor,
)
from .scm_events import ScmEvent, ScmEventStore
from .scm_observability import (
    SCM_AUDIT_BINDING_RESOLVED,
    SCM_AUDIT_PUBLISH_CREATED,
    SCM_AUDIT_PUBLISH_FINISHED,
    SCM_AUDIT_ROUTED_INTENT,
    ScmAuditRecorder,
    correlation_id_for_event,
    correlation_id_for_operation,
    with_correlation_id,
)
from .scm_reaction_router import route_scm_reactions
from .scm_reaction_state import ScmReactionStateStore
from .scm_reaction_types import ReactionIntent, ScmReactionConfig
from .text_utils import _normalize_text

_LOGGER = logging.getLogger(__name__)


class ScmEventLookup(Protocol):
    def get_event(self, event_id: str) -> Optional[ScmEvent]: ...


class ScmBindingResolver(Protocol):
    def __call__(
        self,
        event: ScmEvent,
        *,
        thread_target_id: Optional[str] = None,
    ) -> Optional[PrBinding]: ...


class ScmReactionRouter(Protocol):
    def __call__(
        self,
        event: ScmEvent,
        *,
        binding: Optional[PrBinding] = None,
        config: ScmReactionConfig | Mapping[str, Any] | None = None,
    ) -> list[ReactionIntent]: ...


class PublishJournalWriter(Protocol):
    def create_operation(
        self,
        *,
        operation_key: str,
        operation_kind: str,
        payload: Optional[dict[str, Any]] = None,
        next_attempt_at: Optional[str] = None,
    ) -> tuple[PublishOperation, bool]: ...


class PublishOperationDrainer(Protocol):
    def process_now(self, limit: int = 10) -> list[PublishOperation]: ...


class ScmReactionStateTracker(Protocol):
    def compute_reaction_fingerprint(
        self,
        event: ScmEvent,
        *,
        binding: Optional[PrBinding],
        intent: ReactionIntent,
    ) -> str: ...

    def should_emit_reaction(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
    ) -> bool: ...

    def get_reaction_state(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
    ) -> object | None: ...

    def mark_reaction_emitted(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        event_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> object: ...

    def mark_reaction_suppressed(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        event_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> object: ...

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

    def mark_reaction_delivery_failed(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        event_id: Optional[str] = None,
        error_text: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> object: ...

    def mark_reaction_delivery_succeeded(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        event_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> object: ...

    def resolve_other_active_reactions(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        keep_fingerprint: str,
        event_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> int: ...


def _normalize_event_id(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _compact_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value is not None and value != {}
    }


def _stable_escalation_operation_key(
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


def _reaction_subject(tracking: Mapping[str, Any]) -> str:
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


def _reaction_label(reaction_kind: str) -> str:
    return reaction_kind.replace("_", " ")


def _failure_escalation_message(
    tracking: Mapping[str, Any],
    *,
    delivery_failure_count: int,
    last_error_text: Optional[str],
) -> str:
    subject = _reaction_subject(tracking)
    reaction_label = _reaction_label(str(tracking.get("reaction_kind") or "reaction"))
    details = (
        f" Last error: {last_error_text}."
        if _normalize_text(last_error_text) is not None
        else ""
    )
    return (
        f"SCM automation escalation: {reaction_label} for {subject} failed delivery "
        f"{delivery_failure_count} times.{details}"
    )


def _duplicate_escalation_message(
    tracking: Mapping[str, Any],
    *,
    attempt_count: int,
) -> str:
    subject = _reaction_subject(tracking)
    reaction_label = _reaction_label(str(tracking.get("reaction_kind") or "reaction"))
    return (
        f"SCM automation escalation: {reaction_label} for {subject} remained active "
        f"across {attempt_count} identical deliveries. Duplicate follow-ups are suppressed."
    )


def _resolve_escalation_payload(
    tracking: Mapping[str, Any], *, message: str
) -> dict[str, Any]:
    thread_target_id = _normalize_text(tracking.get("thread_target_id"))
    repo_id = _normalize_text(tracking.get("repo_id"))
    payload = {
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


def _tracking_from_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    tracking = payload.get("scm_reaction")
    return dict(tracking) if isinstance(tracking, Mapping) else {}


def _default_binding_resolver(hub_root: Path) -> ScmBindingResolver:
    def resolver(
        event: ScmEvent,
        *,
        thread_target_id: Optional[str] = None,
    ) -> Optional[PrBinding]:
        return resolve_binding_for_scm_event(
            hub_root,
            event,
            thread_target_id=thread_target_id,
        )

    return resolver


def _default_publish_processor(
    hub_root: Path,
    *,
    journal: PublishJournalStore,
) -> PublishOperationProcessor:
    github_publisher = importlib.import_module(
        "codex_autorunner.integrations.github.publisher"
    )
    build_react_pr_review_comment_executor = (
        github_publisher.build_react_pr_review_comment_executor
    )
    raw_config = load_hub_config(hub_root).raw
    return PublishOperationProcessor(
        journal,
        executors={
            "enqueue_managed_turn": build_enqueue_managed_turn_executor(
                hub_root=hub_root
            ),
            "notify_chat": build_notify_chat_executor(hub_root=hub_root),
            "react_pr_review_comment": build_react_pr_review_comment_executor(
                repo_root=hub_root,
                raw_config=raw_config,
            ),
        },
        mutation_policy_config=raw_config,
    )


@dataclass(frozen=True)
class ScmAutomationIngestResult:
    event: ScmEvent
    binding: Optional[PrBinding]
    reaction_intents: tuple[ReactionIntent, ...]
    publish_operations: tuple[PublishOperation, ...]


class ScmAutomationService:
    def __init__(
        self,
        hub_root: Path,
        *,
        event_store: Optional[ScmEventLookup] = None,
        binding_resolver: Optional[ScmBindingResolver] = None,
        reaction_router: Optional[ScmReactionRouter] = None,
        reaction_config: ScmReactionConfig | Mapping[str, Any] | None = None,
        reaction_state_store: Optional[ScmReactionStateTracker] = None,
        journal: Optional[PublishJournalWriter] = None,
        publish_processor: Optional[PublishOperationDrainer] = None,
    ) -> None:
        self._hub_root = Path(hub_root)
        self._event_store = event_store or ScmEventStore(self._hub_root)
        self._binding_resolver = binding_resolver or _default_binding_resolver(
            self._hub_root
        )
        self._reaction_router = reaction_router or route_scm_reactions
        self._reaction_config = ScmReactionConfig.from_mapping(reaction_config)
        self._audit_recorder = ScmAuditRecorder(self._hub_root)
        self._reaction_state_store = reaction_state_store or ScmReactionStateStore(
            self._hub_root
        )
        resolved_journal = journal or PublishJournalStore(self._hub_root)
        self._journal = resolved_journal
        if publish_processor is not None:
            self._publish_processor = publish_processor
        elif isinstance(resolved_journal, PublishJournalStore):
            self._publish_processor = _default_publish_processor(
                self._hub_root,
                journal=resolved_journal,
            )
        else:
            raise TypeError(
                "publish_processor is required when journal is not a PublishJournalStore"
            )

    def _resolve_event(self, event_or_id: ScmEvent | str) -> ScmEvent:
        if isinstance(event_or_id, ScmEvent):
            return event_or_id
        event_id = _normalize_event_id(event_or_id)
        if event_id is None:
            raise ValueError("event_or_id must be a ScmEvent or non-empty event_id")
        event = self._event_store.get_event(event_id)
        if event is None:
            raise LookupError(f"SCM event '{event_id}' was not found")
        return event

    def ingest_event(
        self,
        event_or_id: ScmEvent | str,
        *,
        thread_target_id: Optional[str] = None,
    ) -> ScmAutomationIngestResult:
        event = self._resolve_event(event_or_id)
        correlation_id = correlation_id_for_event(event)
        binding = self._binding_resolver(event, thread_target_id=thread_target_id)
        self._audit_recorder.record(
            action_type=SCM_AUDIT_BINDING_RESOLVED,
            correlation_id=correlation_id,
            event=event,
            binding=binding,
            payload={"binding_found": binding is not None},
        )
        reaction_intents = tuple(
            self._reaction_router(
                event,
                binding=binding,
                config=self._reaction_config,
            )
        )

        publish_operations: list[PublishOperation] = []
        seen_operation_keys: set[str] = set()
        for intent in reaction_intents:
            self._audit_recorder.record(
                action_type=SCM_AUDIT_ROUTED_INTENT,
                correlation_id=correlation_id,
                event=event,
                binding=binding,
                intent=intent,
            )
            binding_id: Optional[str] = None
            fingerprint: Optional[str] = None
            tracking: dict[str, Any] = {}
            if binding is not None and intent.binding_id is not None:
                binding_id = intent.binding_id
                fingerprint = self._reaction_state_store.compute_reaction_fingerprint(
                    event,
                    binding=binding,
                    intent=intent,
                )
                tracking = _compact_mapping(
                    {
                        "binding_id": binding_id,
                        "correlation_id": correlation_id,
                        "event_id": intent.event_id or event.event_id,
                        "event_type": event.event_type,
                        "fingerprint": fingerprint,
                        "operation_kind": intent.operation_kind,
                        "pr_number": binding.pr_number,
                        "provider": event.provider,
                        "reaction_kind": intent.reaction_kind,
                        "repo_id": binding.repo_id or event.repo_id,
                        "repo_slug": binding.repo_slug or event.repo_slug,
                        "thread_target_id": binding.thread_target_id,
                    }
                )
                self._reaction_state_store.resolve_other_active_reactions(
                    binding_id=binding_id,
                    reaction_kind=intent.reaction_kind,
                    keep_fingerprint=fingerprint,
                    event_id=intent.event_id or event.event_id,
                    metadata=tracking,
                )
                existing = self._reaction_state_store.get_reaction_state(
                    binding_id=binding_id,
                    reaction_kind=intent.reaction_kind,
                    fingerprint=fingerprint,
                )
                if not self._reaction_state_store.should_emit_reaction(
                    binding_id=binding_id,
                    reaction_kind=intent.reaction_kind,
                    fingerprint=fingerprint,
                ):
                    existing_attempt_count = int(
                        getattr(existing, "attempt_count", 0) or 0
                    )
                    duplicate_threshold = (
                        self._reaction_config.duplicate_escalation_threshold
                    )
                    if (
                        existing is not None
                        and getattr(existing, "escalated_at", None) is None
                        and duplicate_threshold > 0
                        and existing_attempt_count + 1 >= duplicate_threshold
                    ):
                        escalation_operation = self._create_escalation_operation(
                            binding_id=binding_id,
                            reaction_kind=intent.reaction_kind,
                            fingerprint=fingerprint,
                            tracking=tracking,
                            message=_duplicate_escalation_message(
                                tracking,
                                attempt_count=existing_attempt_count + 1,
                            ),
                            reason="duplicate",
                            seen_operation_keys=seen_operation_keys,
                            event_id=intent.event_id or event.event_id,
                        )
                        if escalation_operation is not None:
                            publish_operations.append(escalation_operation)
                    elif (
                        existing is not None
                        and getattr(existing, "escalated_at", None) is None
                    ):
                        self._reaction_state_store.mark_reaction_suppressed(
                            binding_id=binding_id,
                            reaction_kind=intent.reaction_kind,
                            fingerprint=fingerprint,
                            event_id=intent.event_id or event.event_id,
                            metadata=tracking,
                        )
                    continue
            if intent.operation_key in seen_operation_keys:
                continue
            seen_operation_keys.add(intent.operation_key)
            payload = with_correlation_id(
                copy.deepcopy(intent.payload),
                correlation_id=correlation_id,
            )
            if tracking:
                payload["scm_reaction"] = tracking
            operation, deduped = self._journal.create_operation(
                operation_key=intent.operation_key,
                operation_kind=intent.operation_kind,
                payload=payload,
            )
            self._audit_recorder.record(
                action_type=SCM_AUDIT_PUBLISH_CREATED,
                correlation_id=correlation_id,
                event=event,
                binding=binding,
                intent=intent,
                operation=operation,
                payload={"deduped": deduped},
            )
            if fingerprint is not None and binding_id is not None:
                self._reaction_state_store.mark_reaction_emitted(
                    binding_id=binding_id,
                    reaction_kind=intent.reaction_kind,
                    fingerprint=fingerprint,
                    event_id=intent.event_id or event.event_id,
                    operation_key=intent.operation_key,
                    metadata=tracking,
                )
            publish_operations.append(operation)

        return ScmAutomationIngestResult(
            event=event,
            binding=binding,
            reaction_intents=reaction_intents,
            publish_operations=tuple(publish_operations),
        )

    def process_now(self, limit: int = 10) -> list[PublishOperation]:
        processed = self._publish_processor.process_now(limit=limit)
        self._record_publish_finished_audit_entries(processed)
        escalations = self._handle_processed_operations(processed)
        if escalations:
            escalation_results = self._publish_processor.process_now(
                limit=len(escalations)
            )
            self._record_publish_finished_audit_entries(escalation_results)
            processed.extend(escalation_results)
        return processed

    def _create_escalation_operation(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        tracking: Mapping[str, Any],
        message: str,
        reason: str,
        seen_operation_keys: set[str],
        event_id: Optional[str],
    ) -> Optional[PublishOperation]:
        operation_key = _stable_escalation_operation_key(
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
        payload = _resolve_escalation_payload(tracking, message=message)
        if correlation_id is not None:
            payload = with_correlation_id(payload, correlation_id=correlation_id)
        operation, deduped = self._journal.create_operation(
            operation_key=operation_key,
            operation_kind="notify_chat",
            payload=payload,
        )
        escalation_metadata = dict(tracking)
        escalation_metadata["escalation_reason"] = reason
        if correlation_id is not None:
            try:
                self._audit_recorder.record(
                    action_type=SCM_AUDIT_PUBLISH_CREATED,
                    correlation_id=correlation_id,
                    operation=operation,
                    payload={
                        "deduped": deduped,
                        "escalation_reason": reason,
                    },
                )
            except (
                Exception
            ):  # intentional: defensive audit logging, must not crash caller
                _LOGGER.warning(
                    "SCM publish-created audit recording failed for %s",
                    operation.operation_id,
                    exc_info=True,
                )
        try:
            self._reaction_state_store.mark_reaction_escalated(
                binding_id=binding_id,
                reaction_kind=reaction_kind,
                fingerprint=fingerprint,
                event_id=event_id,
                operation_key=operation_key,
                metadata=escalation_metadata,
            )
        except Exception:  # intentional: defensive state update, must not crash caller
            _LOGGER.warning(
                "SCM escalation state update failed for operation %s",
                operation.operation_id,
                exc_info=True,
            )
        return operation

    def _handle_processed_operations(
        self,
        operations: list[PublishOperation],
    ) -> list[PublishOperation]:
        seen_operation_keys: set[str] = set()
        escalations: list[PublishOperation] = []
        for operation in operations:
            tracking = _tracking_from_payload(operation.payload)
            binding_id = _normalize_text(tracking.get("binding_id"))
            reaction_kind = _normalize_text(tracking.get("reaction_kind"))
            fingerprint = _normalize_text(tracking.get("fingerprint"))
            if binding_id is None or reaction_kind is None or fingerprint is None:
                continue
            event_id = _normalize_text(tracking.get("event_id"))
            try:
                if operation.state == "succeeded":
                    self._reaction_state_store.mark_reaction_delivery_succeeded(
                        binding_id=binding_id,
                        reaction_kind=reaction_kind,
                        fingerprint=fingerprint,
                        event_id=event_id,
                        operation_key=operation.operation_key,
                        metadata=tracking,
                    )
                    continue
                if operation.state not in {"failed", "pending"}:
                    continue
                failed_state = self._reaction_state_store.mark_reaction_delivery_failed(
                    binding_id=binding_id,
                    reaction_kind=reaction_kind,
                    fingerprint=fingerprint,
                    event_id=event_id,
                    error_text=operation.last_error_text,
                    metadata=tracking,
                )
            except (
                Exception
            ):  # intentional: defensive state update, must not crash caller
                _LOGGER.warning(
                    "SCM reaction-state update failed for operation %s",
                    operation.operation_id,
                    exc_info=True,
                )
                continue
            failure_threshold = (
                self._reaction_config.delivery_failure_escalation_threshold
            )
            if (
                getattr(failed_state, "escalated_at", None) is not None
                or failure_threshold <= 0
                or int(getattr(failed_state, "delivery_failure_count", 0) or 0)
                < failure_threshold
            ):
                continue
            escalation_operation = self._create_escalation_operation(
                binding_id=binding_id,
                reaction_kind=reaction_kind,
                fingerprint=fingerprint,
                tracking=tracking,
                message=_failure_escalation_message(
                    tracking,
                    delivery_failure_count=int(
                        getattr(failed_state, "delivery_failure_count", 0) or 0
                    ),
                    last_error_text=operation.last_error_text,
                ),
                reason="delivery_failed",
                seen_operation_keys=seen_operation_keys,
                event_id=event_id,
            )
            if escalation_operation is not None:
                escalations.append(escalation_operation)
        return escalations

    def _record_publish_finished_audit_entries(
        self,
        operations: list[PublishOperation],
    ) -> None:
        for operation in operations:
            correlation_id = correlation_id_for_operation(operation)
            if correlation_id is None:
                continue
            try:
                self._audit_recorder.record(
                    action_type=SCM_AUDIT_PUBLISH_FINISHED,
                    correlation_id=correlation_id,
                    operation=operation,
                )
            except (
                Exception
            ):  # intentional: defensive audit logging, must not crash caller
                _LOGGER.warning(
                    "SCM publish-finished audit recording failed for %s",
                    operation.operation_id,
                    exc_info=True,
                )


__all__ = [
    "PublishJournalWriter",
    "PublishOperationDrainer",
    "ScmAutomationIngestResult",
    "ScmAutomationService",
    "ScmBindingResolver",
    "ScmEventLookup",
    "ScmReactionRouter",
    "ScmReactionStateTracker",
]
