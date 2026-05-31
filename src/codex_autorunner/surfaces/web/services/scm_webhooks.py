from __future__ import annotations

import inspect
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ....adapters.github.publisher import build_github_publish_executors
from ....core.pr_bindings import PrBindingStore
from ....core.publish_journal import PublishJournalStore
from ....core.scm_automation_service import ScmAutomationService
from ....core.scm_events import ScmEvent, ScmEventStore
from ....core.scm_observability import (
    SCM_AUDIT_INGEST,
    ScmAuditRecorder,
    create_or_preserve_correlation_id,
)
from ....core.scm_reaction_state import ScmReactionStateStore
from ....core.scm_webhook_config import github_automation_config
from .pma.managed_thread_runtime import ensure_managed_thread_queue_worker

ScmDrainCallback = Callable[[object, ScmEvent], object]

_DEFAULT_INSPECT_LIMIT = 50
_MAX_INSPECT_LIMIT = 200
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScmWebhookIngestRequest:
    hub_root: Path
    raw_config: object
    event: ScmEvent
    headers: Mapping[str, Any]
    store_raw_payload: bool
    max_raw_payload_bytes: int
    drain_inline: bool
    request_context: object
    app: object
    app_drain_callback: Optional[ScmDrainCallback] = None
    route_drain_callback: Optional[ScmDrainCallback] = None
    logger: Optional[logging.Logger] = None


@dataclass(frozen=True)
class ScmWebhookIngestOutcome:
    status: str
    event_id: Optional[str] = None
    provider: Optional[str] = None
    event_type: Optional[str] = None
    repo_slug: Optional[str] = None
    repo_id: Optional[str] = None
    pr_number: Optional[int] = None
    delivery_id: Optional[str] = None
    correlation_id: Optional[str] = None
    drained_inline: bool = False
    deduped: bool = False
    audit_error: Optional[str] = None
    drain_error: Optional[str] = None
    rejection_reason: Optional[str] = None
    rejection_detail: Optional[str] = None
    status_code: int = 200

    def to_response_payload(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "event_id": self.event_id,
            "provider": self.provider,
            "event_type": self.event_type,
            "repo_slug": self.repo_slug,
            "repo_id": self.repo_id,
            "pr_number": self.pr_number,
            "delivery_id": self.delivery_id,
            "correlation_id": self.correlation_id,
            "drained_inline": (
                self.drained_inline if self.status == "accepted" else None
            ),
            "deduped": self.deduped or None,
            "audit_error": self.audit_error,
            "drain_error": self.drain_error,
            "reason": self.rejection_reason,
            "detail": self.rejection_detail,
        }
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(frozen=True)
class ScmDrainOutcome:
    processed_operation_count: int
    ensured_managed_thread_ids: tuple[str, ...] = ()
    ensure_worker_errors: tuple[str, ...] = ()


def resolve_inspect_limit(
    value: object, *, default: int = _DEFAULT_INSPECT_LIMIT
) -> int:
    if not (isinstance(value, int) and value > 0):
        value = default
    return min(value, _MAX_INSPECT_LIMIT)


def _serialize_items(items: list[Any]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for item in items:
        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            serialized.append(to_dict())
    return serialized


class ScmWebhookInspectService:
    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def list_events(
        self,
        *,
        provider: Optional[str] = None,
        event_type: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_id: Optional[str] = None,
        pr_number: Optional[int] = None,
        delivery_id: Optional[str] = None,
        occurred_after: Optional[str] = None,
        occurred_before: Optional[str] = None,
        limit: int = _DEFAULT_INSPECT_LIMIT,
    ) -> dict[str, Any]:
        resolved_limit = resolve_inspect_limit(limit)
        events = ScmEventStore(self._hub_root).list_events(
            provider=provider,
            event_type=event_type,
            repo_slug=repo_slug,
            repo_id=repo_id,
            pr_number=pr_number,
            delivery_id=delivery_id,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
            limit=resolved_limit,
        )
        return {"events": _serialize_items(events), "limit": resolved_limit}

    def list_pr_bindings(
        self,
        *,
        provider: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_id: Optional[str] = None,
        pr_state: Optional[str] = None,
        head_branch: Optional[str] = None,
        thread_target_id: Optional[str] = None,
        limit: int = _DEFAULT_INSPECT_LIMIT,
    ) -> dict[str, Any]:
        resolved_limit = resolve_inspect_limit(limit)
        bindings = PrBindingStore(self._hub_root).list_bindings(
            provider=provider,
            repo_slug=repo_slug,
            repo_id=repo_id,
            pr_state=pr_state,
            head_branch=head_branch,
            thread_target_id=thread_target_id,
            limit=resolved_limit,
        )
        return {"bindings": _serialize_items(bindings), "limit": resolved_limit}

    def list_reaction_states(
        self,
        *,
        binding_id: Optional[str] = None,
        reaction_kind: Optional[str] = None,
        state: Optional[str] = None,
        last_event_id: Optional[str] = None,
        limit: int = _DEFAULT_INSPECT_LIMIT,
    ) -> dict[str, Any]:
        resolved_limit = resolve_inspect_limit(limit)
        reactions = ScmReactionStateStore(self._hub_root).list_reaction_states(
            binding_id=binding_id,
            reaction_kind=reaction_kind,
            state=state,
            last_event_id=last_event_id,
            limit=resolved_limit,
        )
        return {"reactions": _serialize_items(reactions), "limit": resolved_limit}

    def list_publish_operations(
        self,
        *,
        state: Optional[str] = None,
        operation_kind: Optional[str] = None,
        limit: int = _DEFAULT_INSPECT_LIMIT,
    ) -> dict[str, Any]:
        resolved_limit = resolve_inspect_limit(limit)
        operations = PublishJournalStore(self._hub_root).list_operations(
            state=state,
            operation_kind=operation_kind,
            limit=resolved_limit,
            newest_first=True,
        )
        return {"operations": _serialize_items(operations), "limit": resolved_limit}


async def ingest_scm_webhook_event(
    request: ScmWebhookIngestRequest,
) -> ScmWebhookIngestOutcome:
    correlation_id = create_or_preserve_correlation_id(
        provider=request.event.provider,
        event_id=request.event.event_id,
        correlation_id=request.event.correlation_id,
        headers=request.headers,
        payload=request.event.payload,
    )
    try:
        persisted = ScmEventStore(request.hub_root).record_event(
            event_id=request.event.event_id,
            provider=request.event.provider,
            event_type=request.event.event_type,
            source=request.event.source,
            occurred_at=request.event.occurred_at,
            received_at=request.event.received_at,
            repo_slug=request.event.repo_slug,
            repo_id=request.event.repo_id,
            pr_number=request.event.pr_number,
            delivery_id=request.event.delivery_id,
            correlation_id=correlation_id,
            payload=request.event.payload,
            raw_payload=(
                request.event.raw_payload if request.store_raw_payload else None
            ),
            max_raw_payload_bytes=request.max_raw_payload_bytes,
        )
    except sqlite3.IntegrityError:
        return _accepted_outcome(
            request.event, correlation_id=correlation_id, deduped=True
        )
    except ValueError as exc:
        reason = (
            "raw_payload_too_large"
            if "raw_payload exceeds" in str(exc)
            else "invalid_event"
        )
        detail = (
            "SCM event payload exceeds configured storage limits"
            if reason == "raw_payload_too_large"
            else "SCM event payload could not be processed"
        )
        return ScmWebhookIngestOutcome(
            status="rejected",
            rejection_reason=reason,
            rejection_detail=detail,
            status_code=413 if reason == "raw_payload_too_large" else 400,
        )

    audit_error: Optional[str] = None
    drain_error: Optional[str] = None
    drained_inline = False
    try:
        ScmAuditRecorder(request.hub_root).record(
            action_type=SCM_AUDIT_INGEST,
            correlation_id=correlation_id,
            event=persisted,
        )
    except (
        OSError,
        ValueError,
        sqlite3.Error,
    ):  # pragma: no cover - defensive audit logging
        if isinstance(request.logger, logging.Logger):
            request.logger.warning(
                "SCM ingest audit recording failed for %s",
                persisted.event_id,
                exc_info=True,
            )
        audit_error = "ingest_audit_failed"

    if request.drain_inline:
        try:
            await run_scm_webhook_drain(request, persisted)
            drained_inline = True
        except Exception as exc:  # pragma: no cover - callback exception types unknown
            if isinstance(request.logger, logging.Logger):
                request.logger.warning(
                    "SCM inline drain failed for %s: %s",
                    persisted.event_id,
                    exc,
                    exc_info=True,
                )
            drain_error = "inline_drain_failed"

    return _accepted_outcome(
        persisted,
        correlation_id=persisted.correlation_id,
        drained_inline=drained_inline,
        audit_error=audit_error,
        drain_error=drain_error,
    )


async def run_scm_webhook_drain(
    request: ScmWebhookIngestRequest,
    event: ScmEvent,
) -> Optional[ScmDrainOutcome]:
    callback = request.app_drain_callback
    if not callable(callback):
        callback = request.route_drain_callback
    if not callable(callback):
        callback = _default_drain_callback_factory(
            hub_root=request.hub_root,
            raw_config=request.raw_config,
            app=request.app,
        )
    result = callback(request.request_context, event)
    if inspect.isawaitable(result):
        result = await result
    return result if isinstance(result, ScmDrainOutcome) else None


def _default_drain_callback_factory(
    *,
    hub_root: Path,
    raw_config: object,
    app: object,
) -> ScmDrainCallback:
    service = ScmAutomationService(
        hub_root,
        reaction_config=github_automation_config(raw_config),
        publish_executor_factory=build_github_publish_executors,
        schedule_deferred_publish_drain=True,
    )

    def callback(_request_context: object, event: ScmEvent) -> ScmDrainOutcome:
        ingested = service.ingest_event(event, execute_automation_jobs=False)
        service.process_scm_automation_jobs(automation_jobs=ingested.automation_jobs)
        processed = service.process_now()
        return ensure_managed_thread_queue_workers_for_scm_operations(app, processed)

    return callback


def ensure_managed_thread_queue_workers_for_scm_operations(
    app: object,
    processed_operations: object,
) -> ScmDrainOutcome:
    if not isinstance(processed_operations, (list, tuple)):
        return ScmDrainOutcome(processed_operation_count=0)
    ensured_thread_ids: set[str] = set()
    ensure_errors: list[str] = []
    for operation in processed_operations:
        if getattr(operation, "operation_kind", None) != "enqueue_managed_turn":
            continue
        if getattr(operation, "state", None) not in {"succeeded", "effect_applied"}:
            continue
        response = getattr(operation, "response", None)
        if not isinstance(response, dict):
            continue
        thread_target_id = str(response.get("thread_target_id") or "").strip()
        if not thread_target_id or thread_target_id in ensured_thread_ids:
            continue
        ensured_thread_ids.add(thread_target_id)
        try:
            ensure_managed_thread_queue_worker(app, thread_target_id)
        except Exception:
            operation_id = getattr(operation, "operation_id", None)
            ensure_errors.append(str(operation_id or thread_target_id))
            logger.exception(
                "Failed to ensure managed-thread queue worker after SCM enqueue "
                "(thread_target_id=%s, operation_id=%s)",
                thread_target_id,
                operation_id,
            )
    return ScmDrainOutcome(
        processed_operation_count=len(processed_operations),
        ensured_managed_thread_ids=tuple(sorted(ensured_thread_ids)),
        ensure_worker_errors=tuple(ensure_errors),
    )


def _accepted_outcome(
    event: ScmEvent,
    *,
    correlation_id: Optional[str],
    drained_inline: bool = False,
    deduped: bool = False,
    audit_error: Optional[str] = None,
    drain_error: Optional[str] = None,
) -> ScmWebhookIngestOutcome:
    return ScmWebhookIngestOutcome(
        status="accepted",
        event_id=event.event_id,
        provider=event.provider,
        event_type=event.event_type,
        repo_slug=event.repo_slug,
        repo_id=event.repo_id,
        pr_number=event.pr_number,
        delivery_id=event.delivery_id,
        correlation_id=correlation_id,
        drained_inline=drained_inline,
        deduped=deduped,
        audit_error=audit_error,
        drain_error=drain_error,
    )


__all__ = [
    "ScmDrainCallback",
    "ScmDrainOutcome",
    "ScmWebhookIngestOutcome",
    "ScmWebhookIngestRequest",
    "ScmWebhookInspectService",
    "ensure_managed_thread_queue_workers_for_scm_operations",
    "ingest_scm_webhook_event",
    "resolve_inspect_limit",
]
