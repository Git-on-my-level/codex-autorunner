"""Domain-owned publish message construction and suppression policy.

This module owns two responsibilities that were previously split across the
delivery control plane (``pma_chat_delivery``), the dispatch decision builder
(``pma_dispatch_decision``), and the web surface (``publish.py``):

1. **Duplicate/noop suppression** -- deciding whether a publish notice should
   be suppressed because it represents a redundant no-op delivered to the same
   channel that owns the active thread binding.
2. **Publish notice message construction** -- building the notification message
   text from structured domain inputs rather than open-coded rules in the
   surface layer.

Both policies are pure functions with no I/O, filesystem access, or SQLite.
"""

from __future__ import annotations

from typing import Optional

from .constants import (
    NOTICE_KIND_ESCALATION,
    NOTICE_KIND_NOOP,
    NOTICE_KIND_TERMINAL_FOLLOWUP,
)
from .models import PublishNoticeContext, PublishSuppressionDecision


def is_noop_duplicate_message(message: str) -> bool:
    normalized = " ".join(str(message or "").lower().split())
    if not normalized:
        return False
    return "already handled" in normalized and "no action" in normalized


def classify_notice_kind(*, source_kind: str, status: str, message_text: str) -> str:
    if status == "ok" and is_noop_duplicate_message(message_text):
        return NOTICE_KIND_NOOP
    if status == "ok":
        return NOTICE_KIND_TERMINAL_FOLLOWUP
    if status == "error":
        return NOTICE_KIND_ESCALATION
    return source_kind


def evaluate_publish_suppression(
    *,
    source_kind: str,
    message_text: str,
    managed_thread_id: Optional[str],
    target_matches_thread_binding: bool,
) -> PublishSuppressionDecision:
    notice_kind = classify_notice_kind(
        source_kind=source_kind,
        status="ok",
        message_text=message_text,
    )
    return PublishSuppressionDecision.evaluate(
        source_kind=source_kind,
        managed_thread_id=managed_thread_id,
        target_matches_thread_binding=target_matches_thread_binding,
        notice_kind=notice_kind,
    )


def build_publish_notice_message(context: PublishNoticeContext) -> str:
    lines: list[str] = [f"PMA update ({context.trigger})"]
    if context.repo_id:
        lines.append(f"repo_id: {context.repo_id}")
    if context.run_id:
        lines.append(f"run_id: {context.run_id}")
    if context.thread_id:
        lines.append(f"thread_id: {context.thread_id}")
    lines.append(f"correlation_id: {context.correlation_id}")
    lines.append("")

    if context.status == "ok":
        lines.append(context.output or "Turn completed with no assistant output.")
        if context.token_usage_footer:
            lines.extend(["", context.token_usage_footer])
    else:
        lines.append(f"status: {context.status}")
        lines.append(f"error: {context.detail or 'Turn failed without detail.'}")
        lines.append("next_action: run /pma status and inspect PMA history if needed.")
        if context.token_usage_footer:
            lines.extend(["", context.token_usage_footer])
    return "\n".join(lines).strip()


__all__ = [
    "PublishNoticeContext",
    "PublishSuppressionDecision",
    "build_publish_notice_message",
    "classify_notice_kind",
    "evaluate_publish_suppression",
    "is_noop_duplicate_message",
]
