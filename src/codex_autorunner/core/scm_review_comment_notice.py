from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Mapping
from typing import Any, Optional, Protocol

from .pr_bindings import PrBinding
from .publish_journal import PublishOperation
from .scm_events import ScmEvent
from .scm_observability import (
    SCM_AUDIT_PUBLISH_CREATED,
    ScmAuditRecorder,
    with_correlation_id,
)
from .scm_reaction_types import stable_reaction_operation_key
from .text_utils import _normalize_text

_MARKDOWN_LINK_RE = re.compile(r"!\[([^\]\n]*)\]\([^)]+\)|\[([^\]\n]+)\]\([^)]+\)")
_REVIEW_BADGE_RE = re.compile(r"!\s*(P\d+)\s+Badge\b", re.IGNORECASE)
_REVIEW_HTML_TAG_RE = re.compile(
    r"</?(?:sub|sup|strong|b|em|i|code|br)\b[^>\n]*>", re.IGNORECASE
)


class _JournalCreator(Protocol):
    def create_operation(
        self,
        *,
        operation_key: str,
        operation_kind: str,
        payload: Optional[dict[str, Any]] = None,
        next_attempt_at: Optional[str] = None,
    ) -> tuple[PublishOperation, bool]: ...


def _event_payload(event: ScmEvent) -> Mapping[str, Any]:
    payload = event.payload
    return payload if isinstance(payload, Mapping) else {}


def collapse_whitespace(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def plain_text_review_summary(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = html.unescape(value)
    text = _MARKDOWN_LINK_RE.sub(
        lambda match: match.group(1) or match.group(2) or " ", text
    )
    text = _REVIEW_HTML_TAG_RE.sub(" ", text)
    text = text.replace("*", " ").replace("`", " ").replace("~", " ")
    text = _REVIEW_BADGE_RE.sub(r"\1", text)
    return collapse_whitespace(text)


def trimmed_summary(value: Any, *, limit: int = 120) -> Optional[str]:
    text = plain_text_review_summary(value)
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def review_comment_location(payload: Mapping[str, Any]) -> Optional[str]:
    path = collapse_whitespace(payload.get("path"))
    line = payload.get("line")
    if path is None:
        return None
    if isinstance(line, int):
        return f"{path}:{line}"
    return path


def review_comment_url(payload: Mapping[str, Any]) -> Optional[str]:
    url = collapse_whitespace(payload.get("html_url"))
    if url is None or not url.startswith(("http://", "https://")):
        return None
    return url


def format_review_comment_wakeup_message(
    *,
    event: ScmEvent,
    binding: PrBinding,
) -> str:
    payload = _event_payload(event)
    subject = f"{binding.repo_slug}#{binding.pr_number}"
    commenter_login = collapse_whitespace(payload.get("author_login"))
    location = review_comment_location(payload)
    comment_summary = trimmed_summary(payload.get("body"))
    review_url = review_comment_url(payload)

    lines = [f"PR review feedback on {subject}"]
    if commenter_login is not None:
        lines.append(f"From: {commenter_login}")
    if location is not None:
        lines.append(f"Location: {location}")
    if comment_summary is not None:
        lines.append(f"Summary: {comment_summary}")
    if review_url is not None:
        lines.append(f"Link: <{review_url}>")
    lines.append("The bound agent thread is taking a look.")
    return "\n".join(lines)


def build_notice_payload(
    *,
    thread_target_id: str,
    message: str,
) -> dict[str, Any]:
    return {
        "delivery": "bound",
        "thread_target_id": thread_target_id,
        "message": message,
    }


def auxiliary_correlation_id(
    *,
    correlation_id: str,
    operation_key: str,
) -> str:
    digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:12]
    return f"{correlation_id}:aux:{digest}"


def review_comment_notice_key(
    *,
    event: ScmEvent,
    binding: PrBinding,
) -> str:
    return stable_reaction_operation_key(
        provider=event.provider,
        event_id=event.event_id,
        reaction_kind="review_comment",
        operation_kind="notify_chat",
        repo_slug=binding.repo_slug,
        repo_id=binding.repo_id or event.repo_id,
        pr_number=binding.pr_number,
        binding_id=binding.binding_id,
        thread_target_id=binding.thread_target_id,
    )


def create_review_comment_notice_operation(
    *,
    journal: _JournalCreator,
    audit_recorder: ScmAuditRecorder,
    event: ScmEvent,
    binding: PrBinding,
    correlation_id: str,
    seen_operation_keys: set[str],
    publish_operations: list[PublishOperation],
) -> None:
    thread_target_id = _normalize_text(binding.thread_target_id)
    if thread_target_id is None:
        return
    operation_key = review_comment_notice_key(event=event, binding=binding)
    if operation_key in seen_operation_keys:
        return
    seen_operation_keys.add(operation_key)
    notice_correlation_id = auxiliary_correlation_id(
        correlation_id=correlation_id,
        operation_key=operation_key,
    )
    payload = with_correlation_id(
        build_notice_payload(
            thread_target_id=thread_target_id,
            message=format_review_comment_wakeup_message(
                event=event,
                binding=binding,
            ),
        ),
        correlation_id=notice_correlation_id,
    )
    operation, deduped = journal.create_operation(
        operation_key=operation_key,
        operation_kind="notify_chat",
        payload=payload,
    )
    audit_recorder.record(
        action_type=SCM_AUDIT_PUBLISH_CREATED,
        correlation_id=correlation_id,
        event=event,
        binding=binding,
        operation=operation,
        payload={
            "deduped": deduped,
            "auxiliary": True,
            "wake_notice": True,
        },
    )
    publish_operations.append(operation)


__all__ = [
    "build_notice_payload",
    "collapse_whitespace",
    "create_review_comment_notice_operation",
    "format_review_comment_wakeup_message",
    "plain_text_review_summary",
    "review_comment_location",
    "review_comment_notice_key",
    "review_comment_url",
    "trimmed_summary",
]
