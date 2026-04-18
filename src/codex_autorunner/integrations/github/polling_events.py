from __future__ import annotations

import uuid
from typing import Any, Callable, Mapping

from ...core.pr_bindings import PrBinding
from ...core.scm_events import ScmEventStore
from ...core.scm_polling_watches import ScmPollingWatch
from .polling_snapshot import _comment_timestamp, snapshot_map


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
    from ...core.text_utils import _normalize_text

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
            payload=dict(payload),
        )
        automation_service.ingest_event(event)
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
