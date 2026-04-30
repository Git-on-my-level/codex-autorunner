from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from .pr_bindings import PrBinding
from .scm_events import ScmEvent
from .scm_reaction_types import ReactionIntent
from .text_utils import _normalize_text, _parse_iso_timestamp

_GENERIC_CI_CHECK_NAMES = frozenset({"check", "aggregate"})


def _event_payload(event: ScmEvent) -> Mapping[str, Any]:
    payload = event.payload
    return payload if isinstance(payload, Mapping) else {}


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _max_iso_datetime(*values: Any) -> Optional[str]:
    parsed = [
        candidate
        for candidate in (_parse_iso_timestamp(v) for v in values)
        if candidate
    ]
    if not parsed:
        return None
    return _isoformat_z(max(parsed))


def _subject(*, binding: Optional[PrBinding], event: ScmEvent) -> str:
    repo_slug = (
        _normalize_text(binding.repo_slug) if binding is not None else None
    ) or _normalize_text(event.repo_slug)
    pr_number = binding.pr_number if binding is not None else event.pr_number
    if repo_slug is not None and isinstance(pr_number, int):
        return f"{repo_slug}#{pr_number}"
    return repo_slug or "SCM binding"


def _tracking_value(
    tracking: Optional[Mapping[str, Any]],
    key: str,
    fallback: Any = None,
) -> Any:
    if isinstance(tracking, Mapping) and key in tracking:
        return tracking.get(key)
    return fallback


def build_feedback_bundle(
    *,
    event: ScmEvent,
    intent: ReactionIntent,
    binding: Optional[PrBinding],
    message_text: str,
    tracking: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    payload = _event_payload(event)
    event_at = (
        _normalize_text(event.received_at)
        or _normalize_text(event.occurred_at)
        or _normalize_text(event.created_at)
        or _isoformat_z(datetime.now(timezone.utc))
    )
    item = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "reaction_kind": intent.reaction_kind,
        "message_text": message_text,
        "received_at": _normalize_text(event.received_at),
        "occurred_at": _normalize_text(event.occurred_at),
    }
    if intent.reaction_kind == "ci_failed":
        check_name = _normalize_text(payload.get("name")) or _normalize_text(
            payload.get("check_name")
        )
        if check_name is not None:
            item["check_name"] = check_name
        conclusion = _normalize_text(payload.get("conclusion"))
        if conclusion is not None:
            item["conclusion"] = conclusion
        head_sha = _normalize_text(payload.get("head_sha"))
        if head_sha is not None:
            item["head_sha"] = head_sha

    bundle = {
        "version": 1,
        "subject": _subject(binding=binding, event=event),
        "binding_id": _tracking_value(
            tracking, "binding_id", binding.binding_id if binding is not None else None
        ),
        "thread_target_id": _tracking_value(
            tracking,
            "thread_target_id",
            binding.thread_target_id if binding is not None else None,
        ),
        "repo_slug": _tracking_value(
            tracking,
            "repo_slug",
            binding.repo_slug if binding is not None else event.repo_slug,
        ),
        "pr_number": _tracking_value(
            tracking,
            "pr_number",
            binding.pr_number if binding is not None else event.pr_number,
        ),
        "opened_at": event_at,
        "last_event_at": event_at,
        "batch_mode": "ci_failed" if intent.reaction_kind == "ci_failed" else "general",
        "items": [item],
    }
    if intent.reaction_kind == "ci_failed":
        head_sha = item.get("head_sha")
        if isinstance(head_sha, str) and head_sha:
            bundle["ci_head_sha"] = head_sha
    return {key: value for key, value in bundle.items() if value is not None}


def extract_feedback_bundle(
    payload: Mapping[str, Any] | None,
) -> Optional[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return None
    request = payload.get("request")
    if not isinstance(request, Mapping):
        return None
    metadata = request.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    bundle = metadata.get("scm_feedback_bundle")
    return copy.deepcopy(dict(bundle)) if isinstance(bundle, Mapping) else None


def _bundle_item_identity(item: Mapping[str, Any]) -> str:
    event_id = _normalize_text(item.get("event_id"))
    if event_id is not None:
        return event_id
    reaction_kind = _normalize_text(item.get("reaction_kind")) or ""
    message_text = _normalize_text(item.get("message_text")) or ""
    return f"{reaction_kind}:{message_text}"


def merge_feedback_bundles(
    existing: Mapping[str, Any],
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(dict(existing))
    existing_items_raw = merged.get("items")
    incoming_items_raw = incoming.get("items")
    existing_items = (
        [dict(item) for item in existing_items_raw if isinstance(item, Mapping)]
        if isinstance(existing_items_raw, list)
        else []
    )
    incoming_items = (
        [dict(item) for item in incoming_items_raw if isinstance(item, Mapping)]
        if isinstance(incoming_items_raw, list)
        else []
    )
    seen = {_bundle_item_identity(item) for item in existing_items}
    for item in incoming_items:
        identity = _bundle_item_identity(item)
        if identity in seen:
            continue
        existing_items.append(item)
        seen.add(identity)
    merged["items"] = existing_items
    merged["last_event_at"] = (
        _max_iso_datetime(
            existing.get("last_event_at"),
            incoming.get("last_event_at"),
        )
        or _normalize_text(existing.get("last_event_at"))
        or _normalize_text(incoming.get("last_event_at"))
    )
    if _normalize_text(merged.get("opened_at")) is None:
        merged["opened_at"] = _normalize_text(incoming.get("opened_at"))
    if _normalize_text(merged.get("subject")) is None:
        merged["subject"] = _normalize_text(incoming.get("subject"))
    if _normalize_text(merged.get("binding_id")) is None:
        merged["binding_id"] = _normalize_text(incoming.get("binding_id"))
    if _normalize_text(merged.get("thread_target_id")) is None:
        merged["thread_target_id"] = _normalize_text(incoming.get("thread_target_id"))
    if _normalize_text(merged.get("repo_slug")) is None:
        merged["repo_slug"] = _normalize_text(incoming.get("repo_slug"))
    if merged.get("pr_number") is None and incoming.get("pr_number") is not None:
        merged["pr_number"] = incoming.get("pr_number")
    if _normalize_text(merged.get("batch_mode")) is None:
        merged["batch_mode"] = _normalize_text(incoming.get("batch_mode"))
    if _normalize_text(merged.get("ci_head_sha")) is None:
        merged["ci_head_sha"] = _normalize_text(incoming.get("ci_head_sha"))
    return {key: value for key, value in merged.items() if value is not None}


def _ci_action_text(check_count: int) -> str:
    return (
        "Inspect the failing check and push a fix."
        if check_count == 1
        else "Inspect the failing checks and push a fix."
    )


def _render_ci_failed_bundle(bundle: Mapping[str, Any]) -> str:
    items_raw = bundle.get("items")
    items = (
        [item for item in items_raw if isinstance(item, Mapping)]
        if isinstance(items_raw, list)
        else []
    )
    if len(items) <= 1:
        message_text = _normalize_text(items[0].get("message_text")) if items else None
        if message_text is not None:
            return message_text
    subject = _normalize_text(bundle.get("subject")) or "SCM binding"
    checks: list[str] = []
    for item in items:
        check_name = _normalize_text(item.get("check_name"))
        if check_name is None or check_name in checks:
            continue
        checks.append(check_name)
    if len(checks) > 1:
        filtered = [
            check_name
            for check_name in checks
            if check_name.lower() not in _GENERIC_CI_CHECK_NAMES
        ]
        if filtered:
            checks = filtered
    conclusions = {
        _normalize_text(item.get("conclusion"))
        for item in items
        if _normalize_text(item.get("conclusion")) is not None
    }
    conclusion = conclusions.pop() if len(conclusions) == 1 else "failure"
    if checks:
        return (
            f"CI failed for {subject}: {', '.join(checks)} ({conclusion}). "
            f"{_ci_action_text(len(checks))}"
        )
    return f"CI failed for {subject}. {_ci_action_text(1)}"


def render_feedback_bundle_message(bundle: Mapping[str, Any]) -> str:
    items_raw = bundle.get("items")
    items = (
        [item for item in items_raw if isinstance(item, Mapping)]
        if isinstance(items_raw, list)
        else []
    )
    if not items:
        subject = _normalize_text(bundle.get("subject")) or "SCM binding"
        return f"SCM updates arrived for {subject}."
    if len(items) == 1:
        message_text = _normalize_text(items[0].get("message_text"))
        if message_text is not None:
            return message_text
    reaction_kinds = {
        _normalize_text(item.get("reaction_kind"))
        for item in items
        if _normalize_text(item.get("reaction_kind")) is not None
    }
    if reaction_kinds == {"ci_failed"}:
        return _render_ci_failed_bundle(bundle)
    subject = _normalize_text(bundle.get("subject")) or "SCM binding"
    lines = [f"Multiple SCM updates arrived for {subject}:"]
    seen_messages: set[str] = set()
    for item in items:
        message_text = _normalize_text(item.get("message_text"))
        if message_text is None or message_text in seen_messages:
            continue
        lines.append(f"- {message_text}")
        seen_messages.add(message_text)
    if len(lines) == 1:
        return f"SCM updates arrived for {subject}."
    return "\n".join(lines)


def apply_feedback_bundle_to_publish_payload(
    payload: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    updated = copy.deepcopy(dict(payload))
    request_value = updated.get("request")
    request = (
        copy.deepcopy(dict(request_value)) if isinstance(request_value, Mapping) else {}
    )
    metadata_value = request.get("metadata")
    metadata = (
        copy.deepcopy(dict(metadata_value))
        if isinstance(metadata_value, Mapping)
        else {}
    )
    materialized_bundle = copy.deepcopy(dict(bundle))
    metadata["scm_feedback_bundle"] = materialized_bundle
    request["metadata"] = metadata
    request["message_text"] = render_feedback_bundle_message(materialized_bundle)
    updated["request"] = request
    if "message_text" in updated:
        updated["message_text"] = request["message_text"]
    return updated


__all__ = [
    "apply_feedback_bundle_to_publish_payload",
    "build_feedback_bundle",
    "extract_feedback_bundle",
    "merge_feedback_bundles",
    "render_feedback_bundle_message",
]
