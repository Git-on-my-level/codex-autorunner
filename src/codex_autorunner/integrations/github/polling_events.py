from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Mapping

from ...core.pr_bindings import PrBinding
from ...core.publish_journal import PublishOperation
from ...core.scm_events import ScmEventStore
from ...core.scm_polling_watches import ScmPollingWatch
from ...core.text_utils import _mapping, _normalize_text
from .polling_snapshot import _comment_timestamp, snapshot_map

_LOGGER = logging.getLogger(__name__)
_PROCESSED_REVIEW_COMMENTS_KEY = "processed_review_comment_ids"
_MAX_PROCESSED_REVIEW_COMMENTS_PER_SCOPE = 500


def _processed_review_comment_scope(binding: PrBinding) -> str:
    return _normalize_text(binding.thread_target_id) or "unbound"


def _processed_review_comments(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    processed = snapshot.get(_PROCESSED_REVIEW_COMMENTS_KEY)
    return dict(processed) if isinstance(processed, Mapping) else {}


def _processed_review_comments_for_scope(
    snapshot: Mapping[str, Any],
    *,
    binding: PrBinding,
) -> dict[str, str]:
    scoped = _processed_review_comments(snapshot)
    scope = _processed_review_comment_scope(binding)
    values = scoped.get(scope)
    if not isinstance(values, Mapping):
        return {}
    normalized: dict[str, str] = {}
    for key, value in values.items():
        comment_id = _normalize_text(key)
        processed_at = _normalize_text(value)
        if comment_id is not None and processed_at is not None:
            normalized[comment_id] = processed_at
    return normalized


def _inherit_processed_review_comments(
    *,
    previous_snapshot: Mapping[str, Any],
    snapshot: dict[str, Any],
) -> None:
    processed = _processed_review_comments(previous_snapshot)
    if processed:
        snapshot[_PROCESSED_REVIEW_COMMENTS_KEY] = processed


def _review_comment_was_processed(
    payload: Mapping[str, Any],
    *,
    processed: Mapping[str, str],
) -> bool:
    comment_id = _normalize_text(payload.get("comment_id"))
    if comment_id is None:
        return False
    processed_at = _normalize_text(processed.get(comment_id))
    if processed_at is None:
        return False
    updated_at = _comment_timestamp(payload)
    if updated_at is None:
        return True
    return updated_at <= processed_at


def _mark_review_comment_processed(
    snapshot: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    binding: PrBinding,
) -> None:
    comment_id = _normalize_text(payload.get("comment_id"))
    if comment_id is None:
        return
    processed_at = _comment_timestamp(payload) or _normalize_text(
        payload.get("created_at")
    )
    if processed_at is None:
        return
    scoped = _processed_review_comments(snapshot)
    scope = _processed_review_comment_scope(binding)
    existing_scope_values = scoped.get(scope)
    scope_values = (
        dict(existing_scope_values)
        if isinstance(existing_scope_values, Mapping)
        else {}
    )
    scope_values[comment_id] = processed_at
    if len(scope_values) > _MAX_PROCESSED_REVIEW_COMMENTS_PER_SCOPE:
        scope_values = dict(
            sorted(scope_values.items(), key=lambda item: item[1], reverse=True)[
                :_MAX_PROCESSED_REVIEW_COMMENTS_PER_SCOPE
            ]
        )
    scoped[scope] = scope_values
    snapshot[_PROCESSED_REVIEW_COMMENTS_KEY] = scoped


def _ingest_created_enqueue(result: Any) -> bool:
    operations = getattr(result, "publish_operations", None)
    if operations is None:
        return True
    for operation in operations:
        if (
            isinstance(operation, PublishOperation)
            and operation.operation_kind == "enqueue_managed_turn"
        ):
            return True
        if getattr(operation, "operation_kind", None) == "enqueue_managed_turn":
            return True
    return False


def emit_new_conditions(
    *,
    event_store: ScmEventStore,
    watch: ScmPollingWatch,
    binding: PrBinding,
    previous_snapshot: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    automation_service_factory: Callable[[], Any],
    now_iso_fn: Any,
) -> int:
    snapshot = snapshot if isinstance(snapshot, dict) else dict(snapshot)
    _inherit_processed_review_comments(
        previous_snapshot=previous_snapshot,
        snapshot=snapshot,
    )
    current_head_sha = _normalize_text(snapshot.get("head_sha"))
    previous_reviews = snapshot_map(previous_snapshot, "changes_requested_reviews")
    current_reviews = snapshot_map(snapshot, "changes_requested_reviews")
    previous_checks = snapshot_map(previous_snapshot, "failed_checks")
    current_checks = snapshot_map(snapshot, "failed_checks")
    previous_issue_comments = snapshot_map(previous_snapshot, "issue_comments")
    current_issue_comments = snapshot_map(snapshot, "issue_comments")
    previous_review_thread_comments = snapshot_map(
        previous_snapshot, "review_thread_comments"
    )
    current_review_thread_comments = snapshot_map(snapshot, "review_thread_comments")
    processed_review_comments = _processed_review_comments_for_scope(
        snapshot,
        binding=binding,
    )

    has_new = False
    for key in current_reviews:
        if key not in previous_reviews:
            has_new = True
            break
    if not has_new:
        for key in current_checks:
            if key not in previous_checks:
                has_new = True
                break
    if not has_new:
        for key in current_issue_comments:
            if key not in previous_issue_comments:
                has_new = True
                break
    if not has_new:
        for key, payload in current_review_thread_comments.items():
            if (
                not bool(payload.get("thread_resolved"))
                and key not in previous_review_thread_comments
                and not _review_comment_was_processed(
                    _mapping(payload),
                    processed=processed_review_comments,
                )
            ):
                has_new = True
                break

    if not has_new:
        return 0

    automation_service = automation_service_factory()
    emitted = 0
    for key, payload in current_reviews.items():
        if key in previous_reviews:
            continue
        event = event_store.record_event(
            event_id=f"github:poll:review:{watch.watch_id}:{uuid.uuid4().hex[:12]}",
            provider="github",
            event_type="pull_request_review",
            occurred_at=_normalize_text(payload.get("submitted_at")) or now_iso_fn(),
            received_at=now_iso_fn(),
            repo_slug=watch.repo_slug,
            repo_id=binding.repo_id or watch.repo_id,
            pr_number=watch.pr_number,
            correlation_id=f"scm-poll:{watch.watch_id}",
            payload=dict(payload),
        )
        automation_service.ingest_event(event)
        emitted += 1

    for key, payload in current_checks.items():
        if key in previous_checks:
            continue
        check_head_sha = _normalize_text(payload.get("head_sha"))
        if (
            current_head_sha is not None
            and check_head_sha is not None
            and check_head_sha != current_head_sha
        ):
            continue
        event = event_store.record_event(
            event_id=f"github:poll:check:{watch.watch_id}:{uuid.uuid4().hex[:12]}",
            provider="github",
            event_type="check_run",
            occurred_at=now_iso_fn(),
            received_at=now_iso_fn(),
            repo_slug=watch.repo_slug,
            repo_id=binding.repo_id or watch.repo_id,
            pr_number=watch.pr_number,
            correlation_id=f"scm-poll:{watch.watch_id}",
            payload=dict(payload),
        )
        automation_service.ingest_event(event)
        emitted += 1

    for key, payload in current_issue_comments.items():
        if key in previous_issue_comments:
            continue
        event = event_store.record_event(
            event_id=(
                f"github:poll:issue-comment:{watch.watch_id}:{uuid.uuid4().hex[:12]}"
            ),
            provider="github",
            event_type="issue_comment",
            occurred_at=_comment_timestamp(payload) or now_iso_fn(),
            received_at=now_iso_fn(),
            repo_slug=watch.repo_slug,
            repo_id=binding.repo_id or watch.repo_id,
            pr_number=watch.pr_number,
            correlation_id=f"scm-poll:{watch.watch_id}",
            payload=dict(payload),
        )
        automation_service.ingest_event(event)
        emitted += 1

    for key, payload in current_review_thread_comments.items():
        if bool(payload.get("thread_resolved")):
            continue
        if key in previous_review_thread_comments:
            continue
        payload_mapping = _mapping(payload)
        if _review_comment_was_processed(
            payload_mapping,
            processed=processed_review_comments,
        ):
            _LOGGER.info(
                "Suppressed duplicate SCM polling review comment event "
                "watch_id=%s thread_target_id=%s comment_id=%s",
                watch.watch_id,
                binding.thread_target_id,
                _normalize_text(payload_mapping.get("comment_id")),
            )
            continue
        event = event_store.record_event(
            event_id=(
                f"github:poll:review-comment:{watch.watch_id}:{uuid.uuid4().hex[:12]}"
            ),
            provider="github",
            event_type="pull_request_review_comment",
            occurred_at=_comment_timestamp(payload) or now_iso_fn(),
            received_at=now_iso_fn(),
            repo_slug=watch.repo_slug,
            repo_id=binding.repo_id or watch.repo_id,
            pr_number=watch.pr_number,
            correlation_id=f"scm-poll:{watch.watch_id}",
            payload=dict(payload_mapping),
        )
        ingest_result = automation_service.ingest_event(event)
        if _ingest_created_enqueue(ingest_result):
            _mark_review_comment_processed(
                snapshot,
                payload_mapping,
                binding=binding,
            )
            processed_review_comments = _processed_review_comments_for_scope(
                snapshot,
                binding=binding,
            )
        emitted += 1

    if emitted:
        automation_service.process_now()
    return emitted


def emit_comment_backfill(
    *,
    event_store: ScmEventStore,
    watch: ScmPollingWatch,
    binding: PrBinding,
    snapshot: Mapping[str, Any],
    reference_timestamp: str,
    window_seconds: int,
    automation_service_factory: Callable[[], Any],
    parse_optional_iso: Any,
    now_iso_fn: Any,
) -> int:
    from .polling_snapshot import snapshot_without_backfilled_comments

    previous_snapshot = snapshot_without_backfilled_comments(
        snapshot,
        reference_timestamp=reference_timestamp,
        window_seconds=window_seconds,
        parse_optional_iso=parse_optional_iso,
    )
    if snapshot_map(previous_snapshot, "issue_comments") == snapshot_map(
        snapshot, "issue_comments"
    ) and snapshot_map(previous_snapshot, "review_thread_comments") == snapshot_map(
        snapshot, "review_thread_comments"
    ):
        return 0
    return emit_new_conditions(
        event_store=event_store,
        watch=watch,
        binding=binding,
        previous_snapshot=previous_snapshot,
        snapshot=snapshot,
        automation_service_factory=automation_service_factory,
        now_iso_fn=now_iso_fn,
    )


__all__ = [
    "emit_comment_backfill",
    "emit_new_conditions",
]
