from __future__ import annotations

import copy
import hashlib
import html
import logging
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from .chat_bindings import active_chat_binding_metadata_by_thread
from .config import load_hub_config
from .pr_binding_resolver import resolve_binding_for_scm_event
from .pr_bindings import PrBinding
from .publish_executor import PublishActionExecutor, PublishOperationProcessor
from .publish_journal import PublishJournalStore, PublishOperation
from .publish_operation_executors import (
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
from .scm_reaction_state import (
    ScmReactionStateStore,
    reaction_state_kind,
    tracking_reaction_state_kind,
)
from .scm_reaction_types import (
    ReactionIntent,
    ScmReactionConfig,
    stable_reaction_operation_key,
)
from .text_utils import _normalize_text

_LOGGER = logging.getLogger(__name__)
_MARKDOWN_LINK_RE = re.compile(r"!\[([^\]\n]*)\]\([^)]+\)|\[([^\]\n]+)\]\([^)]+\)")
_REVIEW_BADGE_RE = re.compile(r"!\s*(P\d+)\s+Badge\b", re.IGNORECASE)
_REVIEW_HTML_TAG_RE = re.compile(
    r"</?(?:sub|sup|strong|b|em|i|code|br)\b[^>\n]*>", re.IGNORECASE
)


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
        repo_root: Path,
        raw_config: Optional[dict[str, Any]] = None,
    ) -> Mapping[str, PublishActionExecutor]: ...


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


def _intent_priority(intent: ReactionIntent) -> tuple[int, str]:
    priorities = {
        "react_pr_review_comment": 0,
        "enqueue_managed_turn": 1,
        "notify_chat": 2,
    }
    return (
        priorities.get(intent.operation_kind, 50),
        intent.operation_key,
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


def _parse_iso_datetime(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _auxiliary_correlation_id(*, correlation_id: str, operation_key: str) -> str:
    digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:12]
    return f"{correlation_id}:aux:{digest}"


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


def _ci_failed_head_sha_from_payload(
    payload: Mapping[str, Any] | None,
) -> Optional[str]:
    bundle = extract_feedback_bundle(payload)
    if bundle is None:
        return None
    return _normalize_text(bundle.get("ci_head_sha"))


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
                    repo_root=hub_root,
                    raw_config=raw_config,
                ).items()
            }
        )
    return PublishOperationProcessor(
        journal,
        executors=executors,
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
        publish_executor_factory: Optional[PublishExecutorFactory] = None,
        schedule_deferred_publish_drain: bool = False,
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
                publish_executor_factory=publish_executor_factory,
            )
        else:
            raise TypeError(
                "publish_processor is required when journal is not a PublishJournalStore"
            )
        self._schedule_deferred_publish_drain = schedule_deferred_publish_drain
        self._deferred_drain_timer: Optional[threading.Timer] = None
        self._deferred_drain_lock = threading.Lock()

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
        parsed = _parse_iso_datetime(next_attempt_at_iso)
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
        pending_kinds = ("enqueue_managed_turn", "notify_chat")
        earliest_future: Optional[datetime] = None
        for operation_kind in pending_kinds:
            pending = self._journal.list_operations(
                state="pending",
                operation_kind=operation_kind,
                limit=500,
            )
            for op in pending:
                if not op.next_attempt_at:
                    continue
                parsed = _parse_iso_datetime(op.next_attempt_at)
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
            _parse_iso_datetime(event.received_at)
            or _parse_iso_datetime(event.occurred_at)
            or _parse_iso_datetime(event.created_at)
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
            _parse_iso_datetime(event.received_at)
            or _parse_iso_datetime(event.occurred_at)
            or _parse_iso_datetime(event.created_at)
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
            _parse_iso_datetime(event.received_at)
            or _parse_iso_datetime(event.occurred_at)
            or _parse_iso_datetime(event.created_at)
            or datetime.now(timezone.utc)
        )
        next_attempt_at = event_time + timedelta(seconds=batch_window_seconds)
        max_window_seconds = self._reaction_config.ci_failed_batch_max_window_seconds
        opened_at = _parse_iso_datetime(bundle.get("opened_at")) or event_time
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
            sorted(
                self._reaction_router(
                    event,
                    binding=binding,
                    config=self._reaction_config,
                ),
                key=_intent_priority,
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
            rsk: Optional[str] = None
            tracking: dict[str, Any] = {}
            if binding is not None and intent.binding_id is not None:
                binding_id = intent.binding_id
                fingerprint = self._reaction_state_store.compute_reaction_fingerprint(
                    event,
                    binding=binding,
                    intent=intent,
                )
                rsk = reaction_state_kind(
                    reaction_kind=intent.reaction_kind,
                    operation_kind=intent.operation_kind,
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
                        "reaction_state_kind": rsk,
                        "repo_id": binding.repo_id or event.repo_id,
                        "repo_slug": binding.repo_slug or event.repo_slug,
                        "head_branch": binding.head_branch,
                        "base_branch": binding.base_branch,
                        "thread_target_id": binding.thread_target_id,
                    }
                )
                self._reaction_state_store.resolve_other_active_reactions(
                    binding_id=binding_id,
                    reaction_kind=rsk,
                    keep_fingerprint=fingerprint,
                    event_id=intent.event_id or event.event_id,
                    metadata=tracking,
                )
                existing = self._reaction_state_store.get_reaction_state(
                    binding_id=binding_id,
                    reaction_kind=rsk,
                    fingerprint=fingerprint,
                )
                if not self._reaction_state_store.should_emit_reaction(
                    binding_id=binding_id,
                    reaction_kind=rsk,
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
                        escalation_operation = create_escalation_operation(
                            journal=self._journal,
                            reaction_state_store=self._reaction_state_store,
                            audit_recorder=self._audit_recorder,
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
                            reaction_kind=rsk,
                            fingerprint=fingerprint,
                            event_id=intent.event_id or event.event_id,
                            metadata=tracking,
                        )
                    continue
            operation_key = intent.operation_key
            next_attempt_at: Optional[str] = None
            if (
                binding is not None
                and intent.reaction_kind == "review_comment"
                and intent.operation_kind == "enqueue_managed_turn"
            ):
                operation_key = self._review_comment_enqueue_batch_key(
                    event=event,
                    binding=binding,
                )
                next_attempt_at = self._review_comment_enqueue_next_attempt_at(
                    event=event
                )
            if operation_key in seen_operation_keys:
                continue
            seen_operation_keys.add(operation_key)
            payload = self._build_feedback_payload(
                event=event,
                intent=intent,
                binding=binding,
                payload=with_correlation_id(
                    copy.deepcopy(intent.payload),
                    correlation_id=correlation_id,
                ),
                tracking=tracking,
            )
            if tracking:
                payload["scm_reaction"] = tracking
            operation: PublishOperation
            deduped = False
            if (
                binding is not None
                and intent.reaction_kind == "ci_failed"
                and intent.operation_kind == "enqueue_managed_turn"
            ):
                head_sha = _ci_failed_head_sha_from_payload(payload)
                if head_sha is not None:
                    existing_ci_batch = self._find_pending_ci_failed_batch_operation(
                        binding=binding,
                        head_sha=head_sha,
                    )
                    if existing_ci_batch is not None:
                        next_attempt_at = self._ci_failed_enqueue_next_attempt_at(
                            event=event,
                            bundle=merge_feedback_bundles(
                                extract_feedback_bundle(existing_ci_batch.payload)
                                or {},
                                extract_feedback_bundle(payload) or {},
                            ),
                        )
                        operation = self._merge_pending_feedback_operation(
                            operation=existing_ci_batch,
                            incoming_payload=payload,
                            next_attempt_at=next_attempt_at,
                            operation_key=operation_key,
                            operation_kind=intent.operation_kind,
                        )
                        deduped = True
                    else:
                        next_attempt_at = self._ci_failed_enqueue_next_attempt_at(
                            event=event,
                            bundle=extract_feedback_bundle(payload) or {},
                        )
                        operation, deduped = self._journal.create_operation(
                            operation_key=operation_key,
                            operation_kind=intent.operation_kind,
                            payload=payload,
                            next_attempt_at=next_attempt_at,
                        )
                else:
                    operation, deduped = self._journal.create_operation(
                        operation_key=operation_key,
                        operation_kind=intent.operation_kind,
                        payload=payload,
                        next_attempt_at=next_attempt_at,
                    )
            else:
                operation, deduped = self._journal.create_operation(
                    operation_key=operation_key,
                    operation_kind=intent.operation_kind,
                    payload=payload,
                    next_attempt_at=next_attempt_at,
                )
                if (
                    deduped
                    and operation.state == "pending"
                    and intent.operation_kind == "enqueue_managed_turn"
                    and extract_feedback_bundle(payload) is not None
                ):
                    operation = self._merge_pending_feedback_operation(
                        operation=operation,
                        incoming_payload=payload,
                        next_attempt_at=next_attempt_at,
                        operation_key=operation_key,
                        operation_kind=intent.operation_kind,
                    )
            if (
                next_attempt_at is not None
                and intent.operation_kind == "enqueue_managed_turn"
            ):
                drain_at = operation.next_attempt_at
                if drain_at is not None:
                    self._schedule_deferred_publish_drain_at(drain_at)
            self._audit_recorder.record(
                action_type=SCM_AUDIT_PUBLISH_CREATED,
                correlation_id=correlation_id,
                event=event,
                binding=binding,
                intent=intent,
                operation=operation,
                payload={"deduped": deduped, "coalesced": deduped},
            )
            if fingerprint is not None and binding_id is not None and rsk is not None:
                self._reaction_state_store.mark_reaction_emitted(
                    binding_id=binding_id,
                    reaction_kind=rsk,
                    fingerprint=fingerprint,
                    event_id=intent.event_id or event.event_id,
                    operation_key=operation_key,
                    metadata=tracking,
                )
            publish_operations.append(operation)
            if (
                binding is not None
                and intent.reaction_kind == "review_comment"
                and intent.operation_kind == "enqueue_managed_turn"
            ):
                notice_operation = self._create_review_comment_notice_operation(
                    tracking=tracking,
                    enqueue_operation=operation,
                    seen_operation_keys=seen_operation_keys,
                    next_attempt_at=operation.next_attempt_at,
                )
                if notice_operation is not None:
                    publish_operations.append(notice_operation)

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
    "ScmAutomationService",
    "ScmBindingResolver",
    "ScmEventLookup",
    "ScmReactionRouter",
    "ScmReactionStateTracker",
]
