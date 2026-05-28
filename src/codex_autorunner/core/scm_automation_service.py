from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from .automation import (
    AutomationEvent,
    AutomationExecutorResult,
    AutomationJob,
    AutomationJobAttempt,
    AutomationRule,
    AutomationRuleEngine,
    AutomationStore,
)
from .automation.models import (
    AUTOMATION_CHILD_KIND_PUBLISH_OPERATION,
    EXECUTOR_PUBLISH_OPERATION,
    JOB_SKIPPED,
    JOB_SUCCEEDED,
    TARGET_POLICY_PR_WORKTREE,
    TRIGGER_KIND_EVENT,
    AutomationChildExecutionEdge,
    AutomationRuntimeContract,
)
from .chat_bindings import active_chat_binding_metadata_by_thread
from .config import load_hub_config
from .pr_binding_resolver import resolve_binding_for_scm_event
from .pr_bindings import PrBinding
from .publish_executor import PublishActionExecutor, PublishOperationProcessor
from .publish_journal import PublishJournalStore, PublishOperation
from .publish_operation_executors import (
    _normalize_mapping,
    build_enqueue_managed_turn_executor,
    build_notify_chat_executor,
)
from .scm_escalation import (
    create_escalation_operation,
    format_duplicate_escalation_message,
    format_failure_escalation_message,
)
from .scm_events import ScmEvent, ScmEventStore
from .scm_feedback_bundle import (
    apply_feedback_bundle_to_publish_payload,
    build_feedback_bundle,
    extract_feedback_bundle,
    merge_feedback_bundles,
)
from .scm_observability import (
    SCM_AUDIT_BINDING_DELIVERY_FAILED,
    SCM_AUDIT_BINDING_RESOLVED,
    SCM_AUDIT_PUBLISH_CREATED,
    SCM_AUDIT_PUBLISH_FINISHED,
    SCM_AUDIT_ROUTED_INTENT,
    ScmAuditRecorder,
    correlation_id_for_event,
    correlation_id_for_operation,
    with_correlation_id,
)
from .scm_reaction_router import route_scm_action_specs
from .scm_reaction_state import (
    ScmReactionStateStore,
    reaction_state_kind,
    tracking_reaction_state_kind,
)
from .scm_reaction_types import (
    ReactionIntent,
    ReactionKind,
    ScmActionDescriptor,
    ScmMessageDescriptor,
    ScmReactionConfig,
    stable_reaction_operation_key,
)
from .text_utils import _normalize_text, _parse_iso_timestamp

_LOGGER = logging.getLogger(__name__)
_MARKDOWN_LINK_RE = re.compile(r"!\[([^\]\n]*)\]\([^)]+\)|\[([^\]\n]+)\]\([^)]+\)")
_REVIEW_BADGE_RE = re.compile(r"!\s*(P\d+)\s+Badge\b", re.IGNORECASE)
_REVIEW_HTML_TAG_RE = re.compile(
    r"</?(?:sub|sup|strong|b|em|i|code|br)\b[^>\n]*>", re.IGNORECASE
)
_SCM_PUBLISH_RETRY_DELAYS_SECONDS = (0.0, 10.0, 30.0, 60.0, 300.0)


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


class ScmActionRouter(Protocol):
    def __call__(
        self,
        event: ScmEvent,
        *,
        binding: Optional[PrBinding] = None,
        config: ScmReactionConfig | Mapping[str, Any] | None = None,
    ) -> list[ScmActionDescriptor | Mapping[str, Any]]: ...


class ScmAutomationRuleEvaluator(Protocol):
    def record_event_and_enqueue_jobs(self, event: AutomationEvent) -> object: ...


class PublishJournalWriter(Protocol):
    def create_operation(
        self,
        *,
        operation_key: str,
        operation_kind: str,
        payload: Optional[dict[str, Any]] = None,
        next_attempt_at: Optional[str] = None,
    ) -> tuple[PublishOperation, bool]: ...

    def update_pending_operation(
        self,
        operation_id: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        next_attempt_at: Optional[str] = None,
    ) -> Optional[PublishOperation]: ...

    def list_operations(
        self,
        *,
        state: Optional[str] = None,
        operation_kind: Optional[str] = None,
        limit: Optional[int] = None,
        newest_first: bool = False,
    ) -> list[PublishOperation]: ...


class PublishOperationDrainer(Protocol):
    def process_now(self, limit: int = 10) -> list[PublishOperation]: ...


class PublishExecutorFactory(Protocol):
    def __call__(
        self,
        *,
        hub_root: Path,
        checkout_root: Path,
        raw_config: Optional[dict[str, Any]] = None,
    ) -> Mapping[str, PublishActionExecutor]: ...


class ScmCompatibilityPublishBridgeExecutor:
    def __init__(
        self,
        publish_operations: tuple[PublishOperation, ...] = (),
        *,
        service: Optional["ScmAutomationService"] = None,
        event_context: Optional[
            Mapping[
                str,
                tuple[ScmEvent, Optional[PrBinding], tuple[ScmActionDescriptor, ...]],
            ]
        ] = None,
    ) -> None:
        self._publish_operations = publish_operations
        self._service = service
        self._event_context = dict(event_context or {})

    @property
    def publish_operations(self) -> tuple[PublishOperation, ...]:
        return self._publish_operations

    def publish_for_job(self, job: AutomationJob) -> tuple[PublishOperation, ...]:
        if self._service is None:
            raise RuntimeError(
                "SCM publish bridge executor requires a service to publish jobs"
            )
        cached = self._event_context.get(job.event_id)
        if cached is not None:
            event, binding, actions = cached
            return self.publish_for_event(event=event, binding=binding, actions=actions)
        automation_event = self._service._automation_store.get_event(job.event_id)
        if automation_event is None:
            self._publish_operations = ()
            return self._publish_operations
        event = _scm_event_from_automation_event(automation_event)
        actions = _actions_for_scm_job(job, automation_event)
        thread_target_id = _normalize_text(
            _mapping_or_empty(automation_event.payload.get("binding")).get(
                "thread_target_id"
            )
        )
        binding = self._service._binding_resolver(
            event,
            thread_target_id=thread_target_id,
        )
        return self.publish_for_event(event=event, binding=binding, actions=actions)

    def publish_for_event(
        self,
        *,
        event: ScmEvent,
        binding: Optional[PrBinding],
        actions: tuple[ScmActionDescriptor, ...],
    ) -> tuple[PublishOperation, ...]:
        if self._service is None:
            raise RuntimeError(
                "SCM publish bridge executor requires a service to publish actions"
            )
        service = self._service
        correlation_id = correlation_id_for_event(event)
        publish_operations: list[PublishOperation] = []
        seen_operation_keys: set[str] = set()
        for action in actions:
            legacy_intent = action.to_intent()
            action_binding_id = action.binding_id
            action_event_id = action.event_id or event.event_id
            action_reaction_kind = action.reaction_kind
            action_operation_kind = action.operation_kind
            service._audit_recorder.record(
                action_type=SCM_AUDIT_ROUTED_INTENT,
                correlation_id=correlation_id,
                event=event,
                binding=binding,
                intent=legacy_intent,
            )
            binding_id: Optional[str] = None
            fingerprint: Optional[str] = None
            rsk: Optional[str] = None
            tracking: dict[str, Any] = {}
            if binding is not None and action_binding_id is not None:
                binding_id = action_binding_id
                fingerprint = (
                    service._reaction_state_store.compute_reaction_fingerprint(
                        event,
                        binding=binding,
                        intent=legacy_intent,
                    )
                )
                rsk = reaction_state_kind(
                    reaction_kind=action_reaction_kind,
                    operation_kind=action_operation_kind,
                )
                tracking = _compact_mapping(
                    {
                        "binding_id": binding_id,
                        "correlation_id": correlation_id,
                        "event_id": action_event_id,
                        "event_type": event.event_type,
                        "fingerprint": fingerprint,
                        "operation_kind": action_operation_kind,
                        "pr_number": binding.pr_number,
                        "provider": event.provider,
                        "reaction_kind": action_reaction_kind,
                        "reaction_state_kind": rsk,
                        "repo_id": binding.repo_id or event.repo_id,
                        "repo_slug": binding.repo_slug or event.repo_slug,
                        "head_branch": binding.head_branch,
                        "base_branch": binding.base_branch,
                        "thread_target_id": binding.thread_target_id,
                    }
                )
                service._reaction_state_store.resolve_other_active_reactions(
                    binding_id=binding_id,
                    reaction_kind=rsk,
                    keep_fingerprint=fingerprint,
                    event_id=action_event_id,
                    metadata=tracking,
                )
                existing = service._reaction_state_store.get_reaction_state(
                    binding_id=binding_id,
                    reaction_kind=rsk,
                    fingerprint=fingerprint,
                )
                if not service._reaction_state_store.should_emit_reaction(
                    binding_id=binding_id,
                    reaction_kind=rsk,
                    fingerprint=fingerprint,
                ):
                    existing_attempt_count = int(
                        getattr(existing, "attempt_count", 0) or 0
                    )
                    duplicate_threshold = (
                        service._reaction_config.duplicate_escalation_threshold
                    )
                    if (
                        existing is not None
                        and getattr(existing, "escalated_at", None) is None
                        and duplicate_threshold > 0
                        and existing_attempt_count + 1 >= duplicate_threshold
                    ):
                        escalation_operation = create_escalation_operation(
                            journal=service._journal,
                            reaction_state_store=service._reaction_state_store,
                            audit_recorder=service._audit_recorder,
                            binding_id=binding_id,
                            reaction_kind=rsk,
                            fingerprint=fingerprint,
                            tracking=tracking,
                            message=format_duplicate_escalation_message(
                                tracking,
                                attempt_count=existing_attempt_count + 1,
                            ),
                            reason="duplicate",
                            seen_operation_keys=seen_operation_keys,
                            event_id=action_event_id,
                        )
                        if escalation_operation is not None:
                            publish_operations.append(escalation_operation)
                    elif (
                        existing is not None
                        and getattr(existing, "escalated_at", None) is None
                    ):
                        service._reaction_state_store.mark_reaction_suppressed(
                            binding_id=binding_id,
                            reaction_kind=rsk,
                            fingerprint=fingerprint,
                            event_id=action_event_id,
                            metadata=tracking,
                        )
                    continue
            operation_key = action.operation_key
            next_attempt_at: Optional[str] = None
            if (
                binding is not None
                and action_reaction_kind == "review_comment"
                and action_operation_kind == "enqueue_managed_turn"
            ):
                operation_key = service._review_comment_enqueue_batch_key(
                    event=event,
                    binding=binding,
                )
                next_attempt_at = service._review_comment_enqueue_next_attempt_at(
                    event=event
                )
            if operation_key in seen_operation_keys:
                continue
            seen_operation_keys.add(operation_key)
            payload = service._build_feedback_payload(
                event=event,
                intent=legacy_intent,
                binding=binding,
                payload=with_correlation_id(
                    copy.deepcopy(action.payload),
                    correlation_id=correlation_id,
                ),
                tracking=tracking,
            )
            operation: PublishOperation
            deduped = False
            if (
                binding is not None
                and action_reaction_kind == "ci_failed"
                and action_operation_kind == "enqueue_managed_turn"
            ):
                head_sha = _ci_failed_head_sha_from_payload(payload)
                if tracking and head_sha is not None:
                    tracking["ci_head_sha"] = head_sha
                    payload["scm_reaction"] = tracking
            else:
                head_sha = None
            if tracking and "scm_reaction" not in payload:
                payload["scm_reaction"] = tracking
            if (
                binding is not None
                and action_reaction_kind == "ci_failed"
                and action_operation_kind == "enqueue_managed_turn"
            ):
                if head_sha is not None:
                    existing_ci_batch = service._find_pending_ci_failed_batch_operation(
                        binding=binding,
                        head_sha=head_sha,
                    )
                    if existing_ci_batch is not None:
                        next_attempt_at = service._ci_failed_enqueue_next_attempt_at(
                            event=event,
                            bundle=merge_feedback_bundles(
                                extract_feedback_bundle(existing_ci_batch.payload)
                                or {},
                                extract_feedback_bundle(payload) or {},
                            ),
                        )
                        operation = service._merge_pending_feedback_operation(
                            operation=existing_ci_batch,
                            incoming_payload=payload,
                            next_attempt_at=next_attempt_at,
                            operation_key=operation_key,
                            operation_kind=action_operation_kind,
                        )
                        deduped = True
                    else:
                        if (
                            binding_id is not None
                            and fingerprint is not None
                            and rsk is not None
                            and _ci_head_already_has_reaction(
                                service._reaction_state_store,
                                binding_id=binding_id,
                                head_sha=head_sha,
                                fingerprint=fingerprint,
                            )
                        ):
                            service._reaction_state_store.mark_reaction_suppressed(
                                binding_id=binding_id,
                                reaction_kind=rsk,
                                fingerprint=fingerprint,
                                event_id=action_event_id,
                                metadata={
                                    **tracking,
                                    "suppression_reason": "ci_head_already_queued",
                                },
                            )
                            continue
                        next_attempt_at = service._ci_failed_enqueue_next_attempt_at(
                            event=event,
                            bundle=extract_feedback_bundle(payload) or {},
                        )
                        operation, deduped = service._journal.create_operation(
                            operation_key=operation_key,
                            operation_kind=action_operation_kind,
                            payload=payload,
                            next_attempt_at=next_attempt_at,
                        )
                else:
                    operation, deduped = service._journal.create_operation(
                        operation_key=operation_key,
                        operation_kind=action_operation_kind,
                        payload=payload,
                        next_attempt_at=next_attempt_at,
                    )
            else:
                operation, deduped = service._journal.create_operation(
                    operation_key=operation_key,
                    operation_kind=action_operation_kind,
                    payload=payload,
                    next_attempt_at=next_attempt_at,
                )
                if (
                    deduped
                    and operation.state == "pending"
                    and action_operation_kind == "enqueue_managed_turn"
                    and extract_feedback_bundle(payload) is not None
                ):
                    operation = service._merge_pending_feedback_operation(
                        operation=operation,
                        incoming_payload=payload,
                        next_attempt_at=next_attempt_at,
                        operation_key=operation_key,
                        operation_kind=action_operation_kind,
                    )
            if (
                next_attempt_at is not None
                and action_operation_kind == "enqueue_managed_turn"
            ):
                drain_at = operation.next_attempt_at
                if drain_at is not None:
                    service._schedule_deferred_publish_drain_at(drain_at)
            service._audit_recorder.record(
                action_type=SCM_AUDIT_PUBLISH_CREATED,
                correlation_id=correlation_id,
                event=event,
                binding=binding,
                intent=legacy_intent,
                operation=operation,
                payload={"deduped": deduped, "coalesced": deduped},
            )
            if fingerprint is not None and binding_id is not None and rsk is not None:
                service._reaction_state_store.mark_reaction_emitted(
                    binding_id=binding_id,
                    reaction_kind=rsk,
                    fingerprint=fingerprint,
                    event_id=action_event_id,
                    operation_key=operation_key,
                    metadata=tracking,
                )
            publish_operations.append(operation)
            if (
                binding is not None
                and action_reaction_kind == "review_comment"
                and action_operation_kind == "enqueue_managed_turn"
            ):
                notice_operation = service._create_review_comment_notice_operation(
                    tracking=tracking,
                    enqueue_operation=operation,
                    seen_operation_keys=seen_operation_keys,
                    next_attempt_at=operation.next_attempt_at,
                )
                if notice_operation is not None:
                    publish_operations.append(notice_operation)
        created = tuple(publish_operations)
        self._publish_operations = (*self._publish_operations, *created)
        return created

    def execute(self, job: AutomationJob) -> AutomationExecutorResult:
        operation = _matching_publish_operation_for_job(job, self._publish_operations)
        if self._service is not None and operation is None:
            self.publish_for_job(job)
            operation = _matching_publish_operation_for_job(
                job, self._publish_operations
            )
        status = JOB_SKIPPED if operation is None else JOB_SUCCEEDED
        return AutomationExecutorResult(
            status=status,
            summary=_operation_result_summary(operation),
            data=_operation_attempt_result(operation),
            execution_refs={},
        )


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


def _normalize_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _compact_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value is not None and value != {}
    }


def _action_priority(
    action: ScmActionDescriptor | Mapping[str, Any],
) -> tuple[int, str]:
    priorities = {
        "react_pr_review_comment": 0,
        "enqueue_managed_turn": 1,
        "notify_chat": 2,
    }
    if isinstance(action, ScmActionDescriptor):
        operation_kind: str = action.operation_kind
        operation_key: str = action.operation_key
    else:
        operation_kind = _normalize_text(action.get("operation_kind")) or ""
        operation_key = _normalize_text(action.get("operation_key")) or ""
    return (
        priorities.get(operation_kind, 50),
        operation_key,
    )


def _publish_notice_payload(
    *,
    hub_root: Path,
    thread_target_id: str,
    message: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "delivery": "bound",
        "thread_target_id": thread_target_id,
        "message": message,
    }
    binding = active_chat_binding_metadata_by_thread(hub_root=hub_root).get(
        thread_target_id
    )
    if isinstance(binding, Mapping):
        surface_kind = _normalize_text(binding.get("binding_kind"))
        surface_key = _normalize_text(binding.get("binding_id"))
        if surface_kind is not None and surface_key is not None:
            payload["delivery_target"] = {
                "surface_kind": surface_kind,
                "surface_key": surface_key,
            }
    return payload


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _auxiliary_correlation_id(*, correlation_id: str, operation_key: str) -> str:
    digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:12]
    return f"{correlation_id}:aux:{digest}"


def _automation_event_type_for_scm_event(event: ScmEvent) -> Optional[str]:
    if event.provider != "github":
        return None
    payload = _normalize_mapping(event.payload)
    action = (_normalize_text(payload.get("action")) or "").lower()
    status = (_normalize_text(payload.get("status")) or "").lower()
    if event.event_type == "pull_request" and action in {"opened", "closed"}:
        return f"scm.github.pull_request.{action}"
    if event.event_type == "pull_request_review" and action == "submitted":
        return "scm.github.pull_request_review.submitted"
    if event.event_type == "pull_request_review_comment" and action == "created":
        return "scm.github.pull_request_review_comment.created"
    if event.event_type == "issue_comment" and action == "created":
        return "scm.github.pull_request_review_comment.created"
    if event.event_type == "check_run" and status == "completed":
        return "scm.github.check_run.completed"
    if event.event_type == "workflow_run" and status == "completed":
        return "scm.github.workflow_run.completed"
    return None


def _scm_event_requires_delivery_target(event: ScmEvent) -> bool:
    if event.provider != "github":
        return False
    payload = _normalize_mapping(event.payload)
    action = (_normalize_text(payload.get("action")) or "").lower()
    status = (_normalize_text(payload.get("status")) or "").lower()
    conclusion = (_normalize_text(payload.get("conclusion")) or "").lower()
    review_state = (_normalize_text(payload.get("review_state")) or "").lower()
    state = (_normalize_text(payload.get("state")) or "").lower()
    if event.event_type == "check_run":
        return status == "completed" and conclusion in {
            "action_required",
            "cancelled",
            "failure",
            "startup_failure",
            "stale",
            "timed_out",
        }
    if event.event_type == "pull_request_review":
        return action == "submitted" and review_state in {
            "approved",
            "changes_requested",
            "commented",
        }
    if event.event_type in {"issue_comment", "pull_request_review_comment"}:
        return action == "created"
    if event.event_type == "pull_request":
        return action == "closed" and (
            payload.get("merged") is True or state == "merged"
        )
    return False


def _builtin_scm_rule_id(reaction_kind: ReactionKind) -> str:
    return f"builtin:scm:github:{reaction_kind}"


def _scm_config_hash(config: ScmReactionConfig) -> str:
    payload = json.dumps(config.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _builtin_scm_reaction_rule(
    *,
    reaction_kind: ReactionKind,
    config: ScmReactionConfig,
    existing: Optional[AutomationRule],
) -> AutomationRule:
    config_hash = _scm_config_hash(config)
    existing_hash = (
        _normalize_text(existing.metadata.get("scm_config_hash"))
        if existing is not None
        else None
    )
    enabled = (
        existing.enabled
        if existing is not None and existing_hash == config_hash
        else config.is_enabled(reaction_kind)
    )
    return AutomationRule.create(
        rule_id=_builtin_scm_rule_id(reaction_kind),
        name=f"Built-in GitHub SCM reaction: {reaction_kind}",
        enabled=enabled,
        system_owned=True,
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={
            "kind": "scm_event",
            "event_types": [
                "scm.github.pull_request.opened",
                "scm.github.pull_request.closed",
                "scm.github.pull_request_review.submitted",
                "scm.github.pull_request_review_comment.created",
                "scm.github.check_run.completed",
                "scm.github.workflow_run.completed",
            ],
        },
        filters={"event.payload.reaction_kind": reaction_kind},
        target_policy=TARGET_POLICY_PR_WORKTREE,
        target={
            "provider": "{{ event.payload.provider }}",
            "repo_id": "{{ event.repo_id }}",
            "repo_slug": "{{ event.payload.repo.slug }}",
            "pr_number": "{{ pr.number }}",
            "binding_id": "{{ event.payload.binding.binding_id }}",
            "thread_target_id": "{{ event.payload.binding.thread_target_id }}",
        },
        executor_kind=EXECUTOR_PUBLISH_OPERATION,
        executor={
            "operation_kind": "scm_action_specs",
            "reaction_kind": reaction_kind,
            "actions": {
                "kind": "scm_action_descriptors",
                "source": "automation_event.payload.actions",
                "schema_version": 1,
            },
            "message_descriptor": _builtin_scm_message_descriptor(reaction_kind),
        },
        policy={
            "dedupe_key": (
                f"scm:{reaction_kind}:"
                "{{ event.payload.source_event_id }}:"
                "{{ event.payload.binding.binding_id }}"
            ),
            "batch_key": f"scm:{reaction_kind}:{{{{ event.repo_id }}}}:{{{{ pr.number }}}}",
            "batch_window_seconds": _scm_batch_window_seconds(
                reaction_kind,
                config,
            ),
            "approval_mode": "pause_and_request_user",
            "max_attempts": 3,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
            "duplicate_escalation_threshold": config.duplicate_escalation_threshold,
            "delivery_failure_escalation_threshold": (
                config.delivery_failure_escalation_threshold
            ),
        },
        metadata={
            "builtin": True,
            "purpose": "scm_github_reaction",
            "reaction_kind": reaction_kind,
            "source_config": "github.automation.reactions",
            "scm_config_hash": config_hash,
        },
    )


def _scm_batch_window_seconds(
    reaction_kind: ReactionKind,
    config: ScmReactionConfig,
) -> int:
    if reaction_kind == "ci_failed":
        return config.ci_failed_batch_window_seconds
    if reaction_kind == "review_comment":
        return config.review_comment_batch_window_seconds
    return 0


def _builtin_scm_message_descriptor(reaction_kind: ReactionKind) -> dict[str, Any]:
    previews = {
        "ci_failed": "CI failed for the pull request. Inspect the failing check and push a fix.",
        "changes_requested": "Changes requested on the pull request. Address the feedback and reply after updating the PR.",
        "review_comment": "New PR review feedback arrived. Inspect the latest comments, address the feedback, and reply after updating the PR.",
        "approved_and_green": "The pull request is approved and ready to land.",
        "merged": "The pull request was merged.",
    }
    return {
        "source_kind": "scm_reaction_message_builder",
        "builder": "build_reaction_message",
        "reaction_kind": reaction_kind,
        "preview": previews[reaction_kind],
        "operation_resolution": "binding_aware",
    }


def _matching_publish_operation_for_job(
    job: AutomationJob,
    publish_operations: tuple[PublishOperation, ...],
) -> Optional[PublishOperation]:
    if not publish_operations:
        return None
    reaction_kind = _job_reaction_kind(job)
    if reaction_kind is None:
        return publish_operations[0]
    for operation in publish_operations:
        if (
            _operation_reaction_kind(operation) == reaction_kind
            and _operation_feedback_bundle(operation) is not None
        ):
            return operation
    for operation in publish_operations:
        if (
            _operation_reaction_kind(operation) == reaction_kind
            and operation.operation_kind == "enqueue_managed_turn"
        ):
            return operation
    for operation in publish_operations:
        if _operation_reaction_kind(operation) == reaction_kind:
            return operation
    return publish_operations[0]


def _normalize_action_descriptors(
    actions: Iterable[ScmActionDescriptor | Mapping[str, Any]],
) -> tuple[ScmActionDescriptor, ...]:
    normalized: list[ScmActionDescriptor] = []
    for action in actions:
        if isinstance(action, ScmActionDescriptor):
            normalized.append(action)
            continue
        if not isinstance(action, Mapping):
            raise ValueError("SCM action descriptor must be an object")
        normalized.append(ScmActionDescriptor.from_mapping(action))
    return tuple(sorted(normalized, key=_action_priority))


def _actions_for_scm_job(
    job: AutomationJob,
    automation_event: AutomationEvent,
) -> tuple[ScmActionDescriptor, ...]:
    payload = _mapping_or_empty(automation_event.payload)
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return ()
    reaction_kind = _job_reaction_kind(job)
    normalized: list[ScmActionDescriptor] = []
    for action in actions:
        if not isinstance(action, Mapping):
            raise ValueError("SCM automation event action must be an object")
        descriptor = ScmActionDescriptor.from_mapping(action)
        if reaction_kind is not None and descriptor.reaction_kind != reaction_kind:
            continue
        normalized.append(descriptor)
    return tuple(sorted(normalized, key=_action_priority))


def _scm_event_from_automation_event(event: AutomationEvent) -> ScmEvent:
    payload = _mapping_or_empty(event.payload)
    repo = _mapping_or_empty(payload.get("repo"))
    pr = _mapping_or_empty(payload.get("pr"))
    source_event_id = _normalize_text(payload.get("source_event_id"))
    source_event_type = _normalize_text(payload.get("source_event_type"))
    observed_at = event.observed_at
    return ScmEvent(
        event_id=source_event_id or event.event_id.removeprefix("scm:"),
        provider=_normalize_text(payload.get("provider")) or "github",
        event_type=source_event_type or event.event_type,
        occurred_at=observed_at,
        received_at=observed_at,
        created_at=observed_at,
        repo_slug=_normalize_text(repo.get("slug")),
        repo_id=_normalize_text(repo.get("repo_id")) or event.repo_id,
        pr_number=_normalize_int(pr.get("number")),
        delivery_id=_normalize_text(payload.get("delivery_id")),
        correlation_id=_normalize_text(payload.get("correlation_id"))
        or _normalize_text(event.metadata.get("scm_correlation_id")),
        payload=copy.deepcopy(_mapping_or_empty(payload.get("scm_payload"))),
        raw_payload=copy.deepcopy(event.raw_payload),
    )


def _job_reaction_kind(job: AutomationJob) -> Optional[str]:
    payload = job.payload if isinstance(job.payload, Mapping) else {}
    event = payload.get("event")
    event_payload = event.get("payload") if isinstance(event, Mapping) else None
    if isinstance(event_payload, Mapping):
        return _normalize_text(event_payload.get("reaction_kind"))
    return _normalize_text(job.executor.get("reaction_kind"))


def _is_scm_publish_bridge_job(job: AutomationJob) -> bool:
    return (
        _normalize_text(job.executor.get("kind")) == EXECUTOR_PUBLISH_OPERATION
        and _normalize_text(job.executor.get("operation_kind")) == "scm_action_specs"
    )


def _operation_reaction_kind(operation: PublishOperation) -> Optional[str]:
    payload = operation.payload if isinstance(operation.payload, Mapping) else {}
    tracking = payload.get("scm_reaction")
    if isinstance(tracking, Mapping):
        return _normalize_text(tracking.get("reaction_kind"))
    metadata = payload.get("metadata")
    scm = metadata.get("scm") if isinstance(metadata, Mapping) else None
    if isinstance(scm, Mapping):
        return _normalize_text(scm.get("reaction_kind"))
    return _normalize_text(payload.get("reaction_kind"))


def _operation_feedback_bundle(operation: PublishOperation) -> Optional[dict[str, Any]]:
    return extract_feedback_bundle(operation.payload)


def _operation_is_escalation(operation: PublishOperation) -> bool:
    return operation.operation_key.startswith("scm-reaction-escalation:")


def _operation_escalation_reason(operation: PublishOperation) -> Optional[str]:
    if not _operation_is_escalation(operation):
        return None
    parts = operation.operation_key.split(":", 2)
    return parts[1] if len(parts) > 1 and parts[1] else "unknown"


def _operation_attempt_result(operation: Optional[PublishOperation]) -> dict[str, Any]:
    result: dict[str, Any] = {"bridge": "scm_reaction_publish"}
    if operation is None:
        result["outcome"] = "suppressed"
        return result
    result.update(
        {
            "outcome": (
                "escalated" if _operation_is_escalation(operation) else "published"
            ),
            "publish_operation_id": operation.operation_id,
            "publish_operation_key": operation.operation_key,
            "publish_operation_kind": operation.operation_kind,
            "publish_operation_state": operation.state,
            "next_attempt_at": operation.next_attempt_at,
            "publish_operation": operation.to_dict(),
        }
    )
    escalation_reason = _operation_escalation_reason(operation)
    if escalation_reason is not None:
        result["escalation_reason"] = escalation_reason
    bundle = _operation_feedback_bundle(operation)
    if bundle is not None:
        bundle_items = bundle.get("items")
        result["batch"] = _compact_mapping(
            {
                "mode": bundle.get("batch_mode"),
                "item_count": (
                    len(bundle_items) if isinstance(bundle_items, list) else None
                ),
                "ci_head_sha": bundle.get("ci_head_sha"),
                "opened_at": bundle.get("opened_at"),
                "last_event_at": bundle.get("last_event_at"),
                "next_attempt_at": operation.next_attempt_at,
            }
        )
    elif operation.next_attempt_at is not None:
        result["batch"] = {"next_attempt_at": operation.next_attempt_at}
    return result


def _operation_result_summary(operation: Optional[PublishOperation]) -> str:
    if operation is None:
        return "SCM reaction suppressed before publish"
    if _operation_is_escalation(operation):
        reason = _operation_escalation_reason(operation) or "unknown"
        return f"SCM reaction escalated through publish operation ({reason})"
    if operation.next_attempt_at is not None:
        return "SCM reaction batched through publish operation"
    return "SCM reaction bridged through publish operation"


def _action_from_legacy_intent(intent: ReactionIntent) -> ScmActionDescriptor:
    return ScmActionDescriptor.create(
        reaction_kind=intent.reaction_kind,
        operation_kind=intent.operation_kind,
        operation_key=intent.operation_key,
        payload=copy.deepcopy(intent.payload),
        message=_message_descriptor_from_payload(intent),
        event_id=intent.event_id,
        binding_id=intent.binding_id,
    )


def _legacy_intents_from_actions(
    actions: tuple[ScmActionDescriptor, ...],
) -> tuple[ReactionIntent, ...]:
    return tuple(action.to_intent() for action in actions)


def _actions_from_legacy_router(
    router: ScmReactionRouter,
) -> ScmActionRouter:
    def route(
        event: ScmEvent,
        *,
        binding: Optional[PrBinding] = None,
        config: ScmReactionConfig | Mapping[str, Any] | None = None,
    ) -> list[ScmActionDescriptor | Mapping[str, Any]]:
        return [
            _action_from_legacy_intent(intent)
            for intent in router(event, binding=binding, config=config)
        ]

    return route


def _message_descriptor_from_payload(intent: ReactionIntent) -> ScmMessageDescriptor:
    if intent.operation_kind == "enqueue_managed_turn":
        request = _mapping_or_empty(intent.payload.get("request"))
        preview = _normalize_text(request.get("message_text")) or "SCM managed turn"
        return ScmMessageDescriptor.create(
            reaction_kind=intent.reaction_kind,
            operation_kind=intent.operation_kind,
            preview=preview,
            source_kind="static_payload",
            builder=None,
            payload_path="payload.request.message_text",
        )
    if intent.operation_kind == "notify_chat":
        preview = _normalize_text(intent.payload.get("message")) or "SCM notification"
        return ScmMessageDescriptor.create(
            reaction_kind=intent.reaction_kind,
            operation_kind=intent.operation_kind,
            preview=preview,
            source_kind="static_payload",
            builder=None,
            payload_path="payload.message",
        )
    preview = _normalize_text(intent.payload.get("content")) or "SCM reaction"
    return ScmMessageDescriptor.create(
        reaction_kind=intent.reaction_kind,
        operation_kind=intent.operation_kind,
        preview=preview,
        source_kind="static_payload",
        builder=None,
        payload_path="payload.content",
    )


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _event_payload(event: ScmEvent) -> Mapping[str, Any]:
    payload = event.payload
    return payload if isinstance(payload, Mapping) else {}


def _collapse_whitespace(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def _plain_text_review_summary(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = html.unescape(value)
    text = _MARKDOWN_LINK_RE.sub(
        lambda match: match.group(1) or match.group(2) or " ", text
    )
    text = _REVIEW_HTML_TAG_RE.sub(" ", text)
    text = text.replace("*", " ").replace("`", " ").replace("~", " ")
    text = _REVIEW_BADGE_RE.sub(r"\1", text)
    return _collapse_whitespace(text)


def _trimmed_summary(value: Any, *, limit: int = 120) -> Optional[str]:
    text = _plain_text_review_summary(value)
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def _review_comment_notice_message(tracking: Mapping[str, Any]) -> str:
    subject = _reaction_subject(tracking)
    return (
        f"Started the latest PR review batch for {subject}.\n"
        "The bound agent thread is working on the latest review feedback now."
    )


def _review_comment_notice_failure_message(
    tracking: Mapping[str, Any],
) -> str:
    subject = _reaction_subject(tracking)
    return (
        f"Failed to wake the bound agent thread for {subject}.\n"
        "The SCM-triggered turn never reached a confirmed running state."
    )


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


def _tracking_from_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    tracking = payload.get("scm_reaction")
    return dict(tracking) if isinstance(tracking, Mapping) else {}


def _operation_waiting_for_managed_turn_start(operation: PublishOperation) -> bool:
    if operation.state != "pending" or operation.operation_kind != "notify_chat":
        return False
    payload = _normalize_mapping(operation.payload)
    dependency = _normalize_mapping(payload.get("managed_turn_dependency"))
    if (
        _normalize_text(dependency.get("dependency_kind"))
        != "enqueue_managed_turn_started"
        and _normalize_text(payload.get("managed_turn_id")) is None
    ):
        return False
    error_text = _normalize_text(operation.last_error_text)
    if error_text is None or not error_text.startswith("RetryablePublishError: "):
        return False
    return any(
        phrase in error_text
        for phrase in (
            "Waiting for enqueue_managed_turn to finish",
            "Waiting for managed turn record to become visible",
            "Waiting for queued managed turn to reach front of queue",
            "Waiting for managed turn start confirmation",
            "Managed turn has not confirmed runtime start",
        )
    )


def _ci_failed_head_sha_from_payload(
    payload: Mapping[str, Any] | None,
) -> Optional[str]:
    bundle = extract_feedback_bundle(payload)
    if bundle is None:
        return None
    return _normalize_text(bundle.get("ci_head_sha"))


def _ci_head_already_has_reaction(
    reaction_state_store: ScmReactionStateTracker,
    *,
    binding_id: str,
    head_sha: str,
    fingerprint: str,
) -> bool:
    list_reaction_states = getattr(reaction_state_store, "list_reaction_states", None)
    if not callable(list_reaction_states):
        return False
    try:
        states = list_reaction_states(
            binding_id=binding_id,
            reaction_kind="ci_failed",
            limit=200,
        )
    except Exception:
        _LOGGER.debug(
            "Unable to list SCM reaction state for CI head suppression",
            exc_info=True,
        )
        return False
    for state in states:
        if _normalize_text(getattr(state, "fingerprint", None)) == fingerprint:
            continue
        metadata = getattr(state, "metadata", None)
        if not isinstance(metadata, Mapping):
            continue
        if _normalize_text(metadata.get("ci_head_sha")) == head_sha:
            return True
    return False


def _merged_feedback_publish_payload(
    base_payload: Mapping[str, Any],
    incoming_payload: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    existing_bundle = extract_feedback_bundle(base_payload)
    incoming_bundle = extract_feedback_bundle(incoming_payload)
    if existing_bundle is None or incoming_bundle is None:
        return None
    merged_bundle = merge_feedback_bundles(existing_bundle, incoming_bundle)
    return apply_feedback_bundle_to_publish_payload(base_payload, merged_bundle)


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
    publish_executor_factory: Optional[PublishExecutorFactory] = None,
    checkout_root: Optional[Path] = None,
) -> PublishOperationProcessor:
    raw_config = load_hub_config(hub_root).raw
    executors: dict[str, PublishActionExecutor] = {
        "enqueue_managed_turn": build_enqueue_managed_turn_executor(hub_root=hub_root),
        "notify_chat": build_notify_chat_executor(hub_root=hub_root),
    }
    if publish_executor_factory is not None:
        executors.update(
            {
                str(operation_kind): executor
                for operation_kind, executor in publish_executor_factory(
                    hub_root=hub_root,
                    checkout_root=checkout_root or hub_root,
                    raw_config=raw_config,
                ).items()
            }
        )
    return PublishOperationProcessor(
        journal,
        executors=executors,
        retry_delays_seconds=_SCM_PUBLISH_RETRY_DELAYS_SECONDS,
        mutation_policy_config=raw_config,
    )


@dataclass(frozen=True)
class ScmAutomationIngestResult:
    event: ScmEvent
    binding: Optional[PrBinding]
    reaction_intents: tuple[ReactionIntent, ...]
    publish_operations: tuple[PublishOperation, ...]
    automation_event: Optional[AutomationEvent] = None
    automation_jobs: tuple[AutomationJob, ...] = ()


@dataclass(frozen=True)
class ScmAutomationJobProcessResult:
    automation_jobs: tuple[AutomationJob, ...] = ()
    publish_operations: tuple[PublishOperation, ...] = ()


class ScmAutomationService:
    def __init__(
        self,
        hub_root: Path,
        *,
        event_store: Optional[ScmEventLookup] = None,
        binding_resolver: Optional[ScmBindingResolver] = None,
        action_router: Optional[ScmActionRouter] = None,
        reaction_router: Optional[ScmReactionRouter] = None,
        reaction_config: ScmReactionConfig | Mapping[str, Any] | None = None,
        reaction_state_store: Optional[ScmReactionStateTracker] = None,
        journal: Optional[PublishJournalWriter] = None,
        publish_processor: Optional[PublishOperationDrainer] = None,
        publish_executor_factory: Optional[PublishExecutorFactory] = None,
        checkout_root: Optional[Path] = None,
        automation_store: Optional[AutomationStore] = None,
        automation_rule_engine: Optional[ScmAutomationRuleEvaluator] = None,
        schedule_deferred_publish_drain: bool = False,
    ) -> None:
        self._hub_root = Path(hub_root)
        self._checkout_root = Path(checkout_root).resolve() if checkout_root else None
        self._event_store = event_store or ScmEventStore(self._hub_root)
        self._binding_resolver = binding_resolver or _default_binding_resolver(
            self._hub_root
        )
        self._action_router = action_router or (
            _actions_from_legacy_router(reaction_router)
            if reaction_router is not None
            else route_scm_action_specs
        )
        self._reaction_config = ScmReactionConfig.from_mapping(reaction_config)
        self._automation_store = automation_store or AutomationStore(self._hub_root)
        self._automation_rule_engine = automation_rule_engine or AutomationRuleEngine(
            self._automation_store
        )
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
                publish_executor_factory=publish_executor_factory,
                checkout_root=self._checkout_root,
            )
        else:
            raise TypeError(
                "publish_processor is required when journal is not a PublishJournalStore"
            )
        self._schedule_deferred_publish_drain = schedule_deferred_publish_drain
        self._deferred_drain_timer: Optional[threading.Timer] = None
        self._deferred_drain_lock = threading.Lock()
        self._seed_builtin_scm_rules()

    def _cancel_deferred_publish_drain(self) -> None:
        with self._deferred_drain_lock:
            if self._deferred_drain_timer is not None:
                self._deferred_drain_timer.cancel()
                self._deferred_drain_timer = None

    def _schedule_deferred_publish_drain_at(
        self, next_attempt_at_iso: Optional[str]
    ) -> None:
        if not self._schedule_deferred_publish_drain:
            return
        if not next_attempt_at_iso:
            return
        parsed = _parse_iso_timestamp(next_attempt_at_iso)
        if parsed is None:
            return
        delay = max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        with self._deferred_drain_lock:
            if self._deferred_drain_timer is not None:
                self._deferred_drain_timer.cancel()
                self._deferred_drain_timer = None
            timer = threading.Timer(delay, self._run_deferred_publish_drain)
            timer.daemon = True
            self._deferred_drain_timer = timer
            timer.start()

    def _reschedule_deferred_publish_drain_if_needed(self) -> None:
        if not self._schedule_deferred_publish_drain:
            return
        if not isinstance(self._journal, PublishJournalStore):
            return
        now = datetime.now(timezone.utc)
        pending = self._journal.list_operations(
            state="pending",
            limit=500,
        )
        earliest_future: Optional[datetime] = None
        for op in pending:
            if not op.next_attempt_at:
                continue
            parsed = _parse_iso_timestamp(op.next_attempt_at)
            if parsed is None:
                continue
            if parsed <= now:
                continue
            if earliest_future is None or parsed < earliest_future:
                earliest_future = parsed
        if earliest_future is None:
            self._cancel_deferred_publish_drain()
            return
        self._schedule_deferred_publish_drain_at(_isoformat_z(earliest_future))

    def _run_deferred_publish_drain(self) -> None:
        with self._deferred_drain_lock:
            self._deferred_drain_timer = None
        try:
            processed = self._publish_processor.process_now(limit=10)
            self._record_publish_finished_audit_entries(processed)
            escalations = self._handle_processed_operations(processed)
            if escalations:
                escalation_results = self._publish_processor.process_now(
                    limit=len(escalations)
                )
                self._record_publish_finished_audit_entries(escalation_results)
        except Exception:
            _LOGGER.warning(
                "Deferred publish drain after SCM batch window failed",
                exc_info=True,
            )
        finally:
            self._reschedule_deferred_publish_drain_if_needed()

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

    def _review_comment_enqueue_batch_key(
        self,
        *,
        event: ScmEvent,
        binding: PrBinding,
    ) -> str:
        batch_window_seconds = self._reaction_config.review_comment_batch_window_seconds
        if batch_window_seconds <= 0:
            return stable_reaction_operation_key(
                provider=event.provider,
                event_id=event.event_id,
                reaction_kind="review_comment",
                operation_kind="enqueue_managed_turn",
                repo_slug=binding.repo_slug,
                repo_id=binding.repo_id or event.repo_id,
                pr_number=binding.pr_number,
                binding_id=binding.binding_id,
                thread_target_id=binding.thread_target_id,
            )
        event_time = (
            _parse_iso_timestamp(event.received_at)
            or _parse_iso_timestamp(event.occurred_at)
            or _parse_iso_timestamp(event.created_at)
            or datetime.now(timezone.utc)
        )
        bucket_start_seconds = (
            int(event_time.timestamp()) // batch_window_seconds
        ) * batch_window_seconds
        batch_end = datetime.fromtimestamp(
            bucket_start_seconds + batch_window_seconds,
            tz=timezone.utc,
        )
        return stable_reaction_operation_key(
            provider=event.provider,
            event_id=f"review-batch:{_isoformat_z(batch_end)}",
            reaction_kind="review_comment",
            operation_kind="enqueue_managed_turn",
            repo_slug=binding.repo_slug,
            repo_id=binding.repo_id or event.repo_id,
            pr_number=binding.pr_number,
            binding_id=binding.binding_id,
            thread_target_id=binding.thread_target_id,
        )

    def _review_comment_enqueue_next_attempt_at(
        self,
        *,
        event: ScmEvent,
    ) -> Optional[str]:
        batch_window_seconds = self._reaction_config.review_comment_batch_window_seconds
        if batch_window_seconds <= 0:
            return None
        event_time = (
            _parse_iso_timestamp(event.received_at)
            or _parse_iso_timestamp(event.occurred_at)
            or _parse_iso_timestamp(event.created_at)
            or datetime.now(timezone.utc)
        )
        bucket_start_seconds = (
            int(event_time.timestamp()) // batch_window_seconds
        ) * batch_window_seconds
        return _isoformat_z(
            datetime.fromtimestamp(
                bucket_start_seconds + batch_window_seconds,
                tz=timezone.utc,
            )
        )

    def _ci_failed_enqueue_next_attempt_at(
        self,
        *,
        event: ScmEvent,
        bundle: Mapping[str, Any],
    ) -> Optional[str]:
        batch_window_seconds = self._reaction_config.ci_failed_batch_window_seconds
        if batch_window_seconds <= 0:
            return None
        event_time = (
            _parse_iso_timestamp(event.received_at)
            or _parse_iso_timestamp(event.occurred_at)
            or _parse_iso_timestamp(event.created_at)
            or datetime.now(timezone.utc)
        )
        next_attempt_at = event_time + timedelta(seconds=batch_window_seconds)
        max_window_seconds = self._reaction_config.ci_failed_batch_max_window_seconds
        opened_at = _parse_iso_timestamp(bundle.get("opened_at")) or event_time
        if max_window_seconds > 0:
            max_attempt_at = opened_at + timedelta(seconds=max_window_seconds)
            if next_attempt_at > max_attempt_at:
                next_attempt_at = max_attempt_at
        return _isoformat_z(next_attempt_at)

    def _build_feedback_payload(
        self,
        *,
        event: ScmEvent,
        intent: ReactionIntent,
        binding: Optional[PrBinding],
        payload: Mapping[str, Any],
        tracking: Mapping[str, Any],
    ) -> dict[str, Any]:
        if intent.operation_kind != "enqueue_managed_turn":
            return copy.deepcopy(dict(payload))
        request_payload = payload.get("request")
        request_mapping = (
            request_payload if isinstance(request_payload, Mapping) else {}
        )
        bundle = build_feedback_bundle(
            event=event,
            intent=intent,
            binding=binding,
            message_text=_normalize_text(request_mapping.get("message_text")) or "",
            tracking=tracking,
        )
        return apply_feedback_bundle_to_publish_payload(payload, bundle)

    def _find_pending_ci_failed_batch_operation(
        self,
        *,
        binding: PrBinding,
        head_sha: str,
    ) -> Optional[PublishOperation]:
        for operation in self._journal.list_operations(
            state="pending",
            operation_kind="enqueue_managed_turn",
            limit=500,
            newest_first=True,
        ):
            tracking = _tracking_from_payload(operation.payload)
            if _normalize_text(tracking.get("binding_id")) != binding.binding_id:
                continue
            bundle = extract_feedback_bundle(operation.payload)
            if bundle is None:
                continue
            if _normalize_text(bundle.get("batch_mode")) != "ci_failed":
                continue
            if _normalize_text(bundle.get("ci_head_sha")) != head_sha:
                continue
            return operation
        return None

    def _merge_pending_feedback_operation(
        self,
        *,
        operation: PublishOperation,
        incoming_payload: Mapping[str, Any],
        next_attempt_at: Optional[str] = None,
        operation_key: str,
        operation_kind: str,
    ) -> PublishOperation:
        merged_payload = _merged_feedback_publish_payload(
            operation.payload,
            incoming_payload,
        )
        if merged_payload is None:
            return operation
        updated = self._journal.update_pending_operation(
            operation.operation_id,
            payload=merged_payload,
            next_attempt_at=next_attempt_at,
        )
        if updated is not None:
            return updated
        created, _deduped = self._journal.create_operation(
            operation_key=operation_key,
            operation_kind=operation_kind,
            payload=merged_payload,
            next_attempt_at=next_attempt_at,
        )
        return created

    def _review_comment_notice_key(
        self,
        *,
        enqueue_operation_key: str,
    ) -> str:
        return stable_reaction_operation_key(
            provider="scm",
            event_id=enqueue_operation_key,
            reaction_kind="review_comment",
            operation_kind="notify_chat",
        )

    def _create_review_comment_notice_operation(
        self,
        *,
        tracking: Mapping[str, Any],
        enqueue_operation: PublishOperation,
        seen_operation_keys: set[str],
        next_attempt_at: Optional[str] = None,
    ) -> Optional[PublishOperation]:
        thread_target_id = _normalize_text(tracking.get("thread_target_id"))
        if thread_target_id is None:
            return None
        operation_key = self._review_comment_notice_key(
            enqueue_operation_key=enqueue_operation.operation_key
        )
        if operation_key in seen_operation_keys:
            return None
        seen_operation_keys.add(operation_key)
        correlation_id = correlation_id_for_operation(
            enqueue_operation
        ) or _normalize_text(tracking.get("correlation_id"))
        if correlation_id is None:
            correlation_id = f"scm:{enqueue_operation.operation_id}"
        notice_correlation_id = _auxiliary_correlation_id(
            correlation_id=correlation_id,
            operation_key=operation_key,
        )
        payload = with_correlation_id(
            {
                **_publish_notice_payload(
                    hub_root=self._hub_root,
                    thread_target_id=thread_target_id,
                    message=_review_comment_notice_message(tracking),
                ),
                "managed_turn_dependency": {
                    "dependency_kind": "enqueue_managed_turn_started",
                    "operation_id": enqueue_operation.operation_id,
                    "thread_target_id": thread_target_id,
                    "failure_message": _review_comment_notice_failure_message(tracking),
                },
            },
            correlation_id=notice_correlation_id,
        )
        operation, deduped = self._journal.create_operation(
            operation_key=operation_key,
            operation_kind="notify_chat",
            payload=payload,
            next_attempt_at=next_attempt_at,
        )
        if deduped and operation.state == "pending":
            updated = self._journal.update_pending_operation(
                operation.operation_id,
                payload=payload,
                next_attempt_at=next_attempt_at,
            )
            if updated is not None:
                operation = updated
        self._audit_recorder.record(
            action_type=SCM_AUDIT_PUBLISH_CREATED,
            correlation_id=correlation_id,
            operation=operation,
            payload={
                "deduped": deduped,
                "auxiliary": True,
                "enqueue_notice": True,
                "source_operation_key": enqueue_operation.operation_key,
                "dependency_operation_id": enqueue_operation.operation_id,
            },
        )
        return operation

    def ingest_event(
        self,
        event_or_id: ScmEvent | str,
        *,
        thread_target_id: Optional[str] = None,
        execute_automation_jobs: bool = True,
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
        scm_actions = _normalize_action_descriptors(
            self._action_router(
                event,
                binding=binding,
                config=self._reaction_config,
            )
        )
        if (
            binding is None
            and not scm_actions
            and _scm_event_requires_delivery_target(event)
        ):
            self._audit_recorder.record(
                action_type=SCM_AUDIT_BINDING_DELIVERY_FAILED,
                correlation_id=correlation_id,
                event=event,
                binding=binding,
                payload={
                    "reason": "no_bound_thread_or_repo_notification_target",
                    "fallback_order": (
                        "thread_target_id",
                        "repo_id_bound_chat",
                        "durable_audit",
                    ),
                },
            )
        elif binding is None and _scm_event_requires_delivery_target(event):
            notify_only = all(
                action.operation_kind == "notify_chat" for action in scm_actions
            )
            if notify_only:
                self._audit_recorder.record(
                    action_type=SCM_AUDIT_BINDING_DELIVERY_FAILED,
                    correlation_id=correlation_id,
                    event=event,
                    binding=binding,
                    payload={
                        "reason": "repo_notification_target_must_be_confirmed_at_publish",
                        "fallback_order": (
                            "thread_target_id",
                            "repo_id_bound_chat",
                            "durable_audit",
                        ),
                    },
                )
        reaction_intents = _legacy_intents_from_actions(scm_actions)
        automation_event = self._automation_event_for_scm_event(
            event=event,
            binding=binding,
            actions=scm_actions,
        )
        automation_jobs = self._record_automation_event_and_jobs(automation_event)
        publish_operations: tuple[PublishOperation, ...] = ()
        if execute_automation_jobs:
            processed = self.process_scm_automation_jobs(
                automation_jobs=automation_jobs,
                event_context=(
                    {automation_event.event_id: (event, binding, scm_actions)}
                    if automation_event is not None
                    else None
                ),
            )
            automation_jobs = processed.automation_jobs
            publish_operations = processed.publish_operations
        return ScmAutomationIngestResult(
            event=event,
            binding=binding,
            reaction_intents=reaction_intents,
            publish_operations=publish_operations,
            automation_event=automation_event,
            automation_jobs=automation_jobs,
        )

    def _record_automation_event_and_jobs(
        self,
        automation_event: Optional[AutomationEvent],
    ) -> tuple[AutomationJob, ...]:
        if automation_event is None:
            return ()
        self._automation_rule_engine.record_event_and_enqueue_jobs(automation_event)
        return tuple(
            job
            for job in self._automation_store.list_jobs(limit=1000)
            if job.event_id == automation_event.event_id
        )

    def scm_event_needs_processing(self, event_id: str) -> bool:
        normalized_event_id = _normalize_text(event_id)
        if normalized_event_id is None:
            return False
        automation_event_id = f"scm:{normalized_event_id}"
        automation_event = self._automation_store.get_event(automation_event_id)
        if automation_event is None:
            return True
        event_jobs = tuple(
            job
            for job in self._automation_store.list_jobs(limit=1000)
            if job.event_id == automation_event_id and _is_scm_publish_bridge_job(job)
        )
        if not event_jobs:
            actions = automation_event.payload.get("actions")
            return isinstance(actions, list) and bool(actions)
        return any(job.state == "pending" for job in event_jobs)

    def process_scm_automation_jobs(
        self,
        *,
        automation_jobs: tuple[AutomationJob, ...] = (),
        event_id: Optional[str] = None,
        event_context: Optional[
            Mapping[
                str,
                tuple[ScmEvent, Optional[PrBinding], tuple[ScmActionDescriptor, ...]],
            ]
        ] = None,
        limit: int = 100,
    ) -> "ScmAutomationJobProcessResult":
        if not automation_jobs:
            jobs = tuple(
                job
                for job in self._automation_store.list_jobs(
                    state="pending",
                    limit=max(0, int(limit)),
                )
                if (event_id is None or job.event_id == event_id)
                and _is_scm_publish_bridge_job(job)
            )
        else:
            jobs = tuple(
                job
                for job in (
                    self._automation_store.get_job(item.job_id)
                    for item in automation_jobs[: max(0, int(limit))]
                )
                if job is not None and _is_scm_publish_bridge_job(job)
            )
        bridge_executor = ScmCompatibilityPublishBridgeExecutor(
            service=self,
            event_context=event_context,
        )
        if event_context and not any(job.state == "pending" for job in jobs):
            for event, binding, actions in event_context.values():
                bridge_executor.publish_for_event(
                    event=event,
                    binding=binding,
                    actions=actions,
                )
        processed_jobs = self._mark_automation_jobs_bridged(
            automation_jobs=jobs,
            publish_operations=(),
            executor=bridge_executor,
        )
        return ScmAutomationJobProcessResult(
            automation_jobs=processed_jobs,
            publish_operations=bridge_executor.publish_operations,
        )

    def _mark_automation_jobs_bridged(
        self,
        *,
        automation_jobs: tuple[AutomationJob, ...],
        publish_operations: tuple[PublishOperation, ...],
        executor: Optional[ScmCompatibilityPublishBridgeExecutor] = None,
    ) -> tuple[AutomationJob, ...]:
        if executor is None:
            executor = ScmCompatibilityPublishBridgeExecutor(publish_operations)
        updated_jobs: list[AutomationJob] = []
        for job in automation_jobs:
            if job.state != "pending":
                updated_jobs.append(job)
                continue
            started = self._automation_store.start_job(job.job_id)
            result = executor.execute(started)
            execution_refs = dict(result.execution_refs)
            operation_id = _normalize_text(result.data.get("publish_operation_id"))
            if operation_id is not None:
                edge = self._automation_store.upsert_child_execution_edge(
                    AutomationChildExecutionEdge.create(
                        parent_job_id=started.job_id,
                        child_kind=AUTOMATION_CHILD_KIND_PUBLISH_OPERATION,
                        child_id=operation_id,
                        requested_runtime=AutomationRuntimeContract(
                            input_ref={
                                "kind": "automation_job",
                                "job_id": started.job_id,
                            },
                            workspace_scope={"kind": "publish_operation"},
                        ),
                        actual_runtime=AutomationRuntimeContract(
                            input_ref={
                                "kind": "automation_job",
                                "job_id": started.job_id,
                            },
                            workspace_scope={"kind": "publish_operation"},
                        ),
                        authoritative_for_parent_completion=True,
                        terminal_state="succeeded",
                    )
                )
                execution_refs["automation_child_edge_id"] = edge.edge_id
            if result.status == JOB_SKIPPED:
                completed = self._automation_store.skip_job(
                    started.job_id,
                    result_summary=result.summary,
                )
            else:
                completed = self._automation_store.complete_job(
                    started.job_id,
                    result_summary=result.summary,
                    execution_refs=execution_refs,
                )
            self._automation_store.record_attempt(
                AutomationJobAttempt.create(
                    job_id=completed.job_id,
                    attempt_number=completed.attempt_count,
                    status=result.status,
                    started_at=completed.started_at,
                    finished_at=completed.finished_at,
                    executor_result=result.data,
                    execution_refs=execution_refs,
                )
            )
            updated_jobs.append(completed)
        return tuple(updated_jobs)

    def _automation_event_for_scm_event(
        self,
        *,
        event: ScmEvent,
        binding: Optional[PrBinding],
        actions: tuple[ScmActionDescriptor, ...],
    ) -> Optional[AutomationEvent]:
        event_type = _automation_event_type_for_scm_event(event)
        if event_type is None:
            return None
        reaction_kind = actions[0].reaction_kind if actions else None
        return AutomationEvent.create(
            event_id=f"scm:{event.event_id}",
            event_type=event_type,
            observed_at=event.received_at or event.occurred_at,
            source=f"scm.{event.provider}",
            repo_id=event.repo_id or (binding.repo_id if binding is not None else None),
            target=_compact_mapping(
                {
                    "provider": event.provider,
                    "repo_slug": event.repo_slug
                    or (binding.repo_slug if binding is not None else None),
                    "repo_id": event.repo_id
                    or (binding.repo_id if binding is not None else None),
                    "pr_number": event.pr_number
                    or (binding.pr_number if binding is not None else None),
                    "binding_id": binding.binding_id if binding is not None else None,
                    "thread_target_id": (
                        binding.thread_target_id if binding is not None else None
                    ),
                }
            ),
            payload=_compact_mapping(
                {
                    "provider": event.provider,
                    "source_event_id": event.event_id,
                    "source_event_type": event.event_type,
                    "delivery_id": event.delivery_id,
                    "correlation_id": correlation_id_for_event(event),
                    "repo": _compact_mapping(
                        {
                            "slug": event.repo_slug
                            or (binding.repo_slug if binding is not None else None),
                            "repo_id": event.repo_id
                            or (binding.repo_id if binding is not None else None),
                        }
                    ),
                    "pr": _compact_mapping(
                        {
                            "number": event.pr_number
                            or (binding.pr_number if binding is not None else None),
                            "state": binding.pr_state if binding is not None else None,
                            "head_branch": (
                                binding.head_branch if binding is not None else None
                            ),
                            "base_branch": (
                                binding.base_branch if binding is not None else None
                            ),
                        }
                    ),
                    "binding": (
                        _compact_mapping(
                            {
                                "binding_id": binding.binding_id,
                                "thread_target_id": binding.thread_target_id,
                            }
                        )
                        if binding is not None
                        else None
                    ),
                    "reaction_kind": reaction_kind,
                    "actions": [action.to_dict() for action in actions],
                    "operation_kinds": [action.operation_kind for action in actions],
                    "scm_payload": copy.deepcopy(event.payload),
                }
            ),
            raw_payload=copy.deepcopy(event.raw_payload or event.payload),
            metadata=_compact_mapping(
                {
                    "scm_event_id": event.event_id,
                    "scm_correlation_id": correlation_id_for_event(event),
                }
            ),
        )

    def _seed_builtin_scm_rules(self) -> None:
        for reaction_kind in (
            "ci_failed",
            "changes_requested",
            "review_comment",
            "approved_and_green",
            "merged",
        ):
            self._automation_store.upsert_rule(
                _builtin_scm_reaction_rule(
                    reaction_kind=reaction_kind,
                    config=self._reaction_config,
                    existing=self._automation_store.get_rule(
                        _builtin_scm_rule_id(reaction_kind)
                    ),
                )
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
        self._reschedule_deferred_publish_drain_if_needed()
        return processed

    def _handle_processed_operations(
        self,
        operations: list[PublishOperation],
    ) -> list[PublishOperation]:
        seen_operation_keys: set[str] = set()
        escalations: list[PublishOperation] = []
        for operation in operations:
            tracking = _tracking_from_payload(operation.payload)
            binding_id = _normalize_text(tracking.get("binding_id"))
            rsk = tracking_reaction_state_kind(tracking)
            fingerprint = _normalize_text(tracking.get("fingerprint"))
            if binding_id is None or rsk is None or fingerprint is None:
                continue
            event_id = _normalize_text(tracking.get("event_id"))
            try:
                if operation.state == "succeeded":
                    self._reaction_state_store.mark_reaction_delivery_succeeded(
                        binding_id=binding_id,
                        reaction_kind=rsk,
                        fingerprint=fingerprint,
                        event_id=event_id,
                        operation_key=operation.operation_key,
                        metadata=tracking,
                    )
                    continue
                if (
                    operation.state == "pending"
                    and _operation_waiting_for_managed_turn_start(operation)
                ):
                    continue
                if operation.state not in {"failed", "pending"}:
                    continue
                failed_state = self._reaction_state_store.mark_reaction_delivery_failed(
                    binding_id=binding_id,
                    reaction_kind=rsk,
                    fingerprint=fingerprint,
                    event_id=event_id,
                    error_text=operation.last_error_text,
                    metadata=tracking,
                )
            except Exception:
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
            escalation_operation = create_escalation_operation(
                journal=self._journal,
                reaction_state_store=self._reaction_state_store,
                audit_recorder=self._audit_recorder,
                binding_id=binding_id,
                reaction_kind=rsk,
                fingerprint=fingerprint,
                tracking=tracking,
                message=format_failure_escalation_message(
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
            except Exception:
                _LOGGER.warning(
                    "SCM publish-finished audit recording failed for %s",
                    operation.operation_id,
                    exc_info=True,
                )


__all__ = [
    "PublishJournalWriter",
    "PublishOperationDrainer",
    "ScmAutomationIngestResult",
    "ScmAutomationJobProcessResult",
    "ScmAutomationService",
    "ScmBindingResolver",
    "ScmEventLookup",
    "ScmReactionRouter",
    "ScmReactionStateTracker",
]
