from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ...core.pr_bindings import PrBinding
from ...core.scm_events import ScmEvent, ScmEventStore
from ...core.scm_polling_watches import ScmPollingWatch
from ...core.text_utils import _mapping, _normalize_text
from .polling_snapshot import _comment_timestamp, snapshot_map

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollEventRecord:
    event: ScmEvent
    created: bool


def _canonical_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_payload(mapped_value)
            for key, mapped_value in sorted(
                value.items(), key=lambda item: str(item[0])
            )
            if mapped_value is not None
        }
    if isinstance(value, list):
        return [_canonical_payload(item) for item in value]
    return value


def _stable_poll_event_id(
    *,
    event_type: str,
    watch: ScmPollingWatch,
    binding: PrBinding,
    payload: Mapping[str, Any],
) -> str:
    payload_map = _mapping(payload)
    identity_payload: dict[str, Any] = {
        "binding": {
            "binding_id": binding.binding_id,
            "thread_target_id": binding.thread_target_id,
        },
        "event_type": event_type,
        "payload": _canonical_payload(payload_map),
        "pr_number": watch.pr_number,
        "provider": "github",
        "repo_slug": watch.repo_slug,
    }
    if event_type in {"issue_comment", "pull_request_review_comment"}:
        identity_payload["subject"] = {
            "comment_id": _normalize_text(payload_map.get("comment_id")),
            "updated_at": _comment_timestamp(payload_map),
        }
    elif event_type == "pull_request_review":
        identity_payload["subject"] = {
            "review_id": _normalize_text(payload_map.get("review_id")),
            "submitted_at": _normalize_text(payload_map.get("submitted_at")),
        }
    elif event_type == "check_run":
        identity_payload["subject"] = {
            "conclusion": _normalize_text(payload_map.get("conclusion")),
            "details_url": _normalize_text(payload_map.get("details_url")),
            "head_sha": _normalize_text(payload_map.get("head_sha")),
            "name": _normalize_text(payload_map.get("name")),
        }
    encoded = json.dumps(
        _canonical_payload(identity_payload),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"github:poll:{event_type}:{digest}"


def _record_poll_event(
    *,
    event_store: ScmEventStore,
    watch: ScmPollingWatch,
    binding: PrBinding,
    event_type: str,
    occurred_at: str,
    received_at: str,
    payload: Mapping[str, Any],
) -> PollEventRecord:
    event_id = _stable_poll_event_id(
        event_type=event_type,
        watch=watch,
        binding=binding,
        payload=payload,
    )
    event = event_store.record_event_if_new(
        event_id=event_id,
        provider="github",
        event_type=event_type,
        occurred_at=occurred_at,
        received_at=received_at,
        repo_slug=watch.repo_slug,
        repo_id=binding.repo_id or watch.repo_id,
        pr_number=watch.pr_number,
        correlation_id=f"scm-poll:{watch.watch_id}",
        payload=dict(payload),
    )
    if event is not None:
        return PollEventRecord(event=event, created=True)
    existing = event_store.get_event(event_id)
    if existing is None:
        existing = event_store.get_event_by_comment_identity(
            event_type=event_type,
            repo_slug=watch.repo_slug,
            pr_number=watch.pr_number,
            comment_id=_mapping(payload).get("comment_id"),
        )
    if existing is None:
        raise RuntimeError("SCM event row missing after duplicate poll event")
    return PollEventRecord(event=existing, created=False)


def _automation_needs_poll_event(
    *,
    automation_service: Any,
    event: ScmEvent,
) -> bool:
    checker = getattr(automation_service, "scm_event_needs_processing", None)
    if not callable(checker):
        return False
    return bool(checker(event.event_id))


def _ingest_poll_event(
    *,
    automation_service: Any,
    record: PollEventRecord,
) -> bool:
    if not record.created and not _automation_needs_poll_event(
        automation_service=automation_service,
        event=record.event,
    ):
        return False
    _ingest_and_process_scm_automation_jobs(automation_service, record.event)
    return True


def _ingest_and_process_scm_automation_jobs(automation_service: Any, event: Any) -> Any:
    processor = getattr(automation_service, "process_scm_automation_jobs", None)
    if not callable(processor):
        return automation_service.ingest_event(event)
    result = automation_service.ingest_event(event, execute_automation_jobs=False)
    processed = processor(automation_jobs=getattr(result, "automation_jobs", ()))
    if hasattr(result, "automation_jobs"):
        result.automation_jobs = getattr(processed, "automation_jobs", ())
    if hasattr(result, "publish_operations"):
        result.publish_operations = getattr(processed, "publish_operations", ())
    return result


def _check_targets_current_head(
    payload: Mapping[str, Any],
    *,
    current_head_sha: str | None,
) -> bool:
    if current_head_sha is None:
        return True
    check_head_sha = _normalize_text(payload.get("head_sha"))
    return check_head_sha == current_head_sha


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

    has_new = False
    for key in current_reviews:
        if key not in previous_reviews:
            has_new = True
            break
    if not has_new:
        for key, payload in current_checks.items():
            if key not in previous_checks:
                has_new = _check_targets_current_head(
                    _mapping(payload),
                    current_head_sha=current_head_sha,
                )
                if has_new:
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
        record = _record_poll_event(
            event_store=event_store,
            watch=watch,
            binding=binding,
            event_type="pull_request_review",
            occurred_at=_normalize_text(payload.get("submitted_at")) or now_iso_fn(),
            received_at=now_iso_fn(),
            payload=_mapping(payload),
        )
        if _ingest_poll_event(automation_service=automation_service, record=record):
            emitted += 1

    for key, payload in current_checks.items():
        if key in previous_checks:
            continue
        if not _check_targets_current_head(
            _mapping(payload),
            current_head_sha=current_head_sha,
        ):
            continue
        record = _record_poll_event(
            event_store=event_store,
            watch=watch,
            binding=binding,
            event_type="check_run",
            occurred_at=now_iso_fn(),
            received_at=now_iso_fn(),
            payload=_mapping(payload),
        )
        if _ingest_poll_event(automation_service=automation_service, record=record):
            emitted += 1

    for key, payload in current_issue_comments.items():
        if key in previous_issue_comments:
            continue
        record = _record_poll_event(
            event_store=event_store,
            watch=watch,
            binding=binding,
            event_type="issue_comment",
            occurred_at=_comment_timestamp(payload) or now_iso_fn(),
            received_at=now_iso_fn(),
            payload=_mapping(payload),
        )
        if _ingest_poll_event(automation_service=automation_service, record=record):
            emitted += 1

    for key, payload in current_review_thread_comments.items():
        if bool(payload.get("thread_resolved")):
            continue
        if key in previous_review_thread_comments:
            continue
        payload_mapping = _mapping(payload)
        record = _record_poll_event(
            event_store=event_store,
            watch=watch,
            binding=binding,
            event_type="pull_request_review_comment",
            occurred_at=_comment_timestamp(payload) or now_iso_fn(),
            received_at=now_iso_fn(),
            payload=payload_mapping,
        )
        if not _ingest_poll_event(automation_service=automation_service, record=record):
            _LOGGER.info(
                "Suppressed duplicate SCM polling review comment event "
                "watch_id=%s thread_target_id=%s comment_id=%s",
                watch.watch_id,
                binding.thread_target_id,
                _normalize_text(payload_mapping.get("comment_id")),
            )
            continue
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
