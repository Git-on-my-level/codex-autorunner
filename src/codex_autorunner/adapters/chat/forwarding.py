"""Helpers for rendering forwarded inbound messages."""

from __future__ import annotations

from typing import Optional

from .models import ChatForwardInfo, ChatReplyInfo

_MAX_REPLY_CONTEXT_CHARS = 500
_REPLY_UNAVAILABLE_TEXT = "(original message text unavailable)"


def _truncate_context_text(text: str, *, max_chars: int) -> str:
    normalized = text.strip()
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 1].rstrip()}..."


def _format_reply_context(reply_context: Optional[ChatReplyInfo]) -> str:
    if reply_context is None:
        return ""
    header = "Replying to message"
    if reply_context.author_label:
        header += f" from {reply_context.author_label}"
    if reply_context.message.message_id:
        header += f" [message {reply_context.message.message_id}]"
    header += ":"
    reply_text = (reply_context.text or "").strip() or _REPLY_UNAVAILABLE_TEXT
    return "\n".join(
        [header, _truncate_context_text(reply_text, max_chars=_MAX_REPLY_CONTEXT_CHARS)]
    )


def compose_forwarded_message_text(
    text: Optional[str],
    forwarded_from: Optional[ChatForwardInfo] = None,
) -> str:
    """Render user text plus forwarded context into a single prompt string."""

    base_text = (text or "").strip()
    if forwarded_from is None:
        return base_text

    forwarded_text = (forwarded_from.text or "").strip()
    header = "Forwarded message"
    if forwarded_from.is_automatic:
        header += " (automatic)"
    if forwarded_from.source_label:
        header += f" from {forwarded_from.source_label}"
    if forwarded_from.message_id:
        header += f" [message {forwarded_from.message_id}]"
    header += ":"

    lines: list[str] = []
    if base_text:
        lines.append(base_text)
        lines.append("")
    lines.append(header)
    if forwarded_text:
        lines.append(forwarded_text)
    return "\n".join(lines).strip()


def compose_inbound_message_text(
    text: Optional[str],
    *,
    forwarded_from: Optional[ChatForwardInfo] = None,
    reply_context: Optional[ChatReplyInfo] = None,
    include_reply_context: bool = True,
) -> str:
    """Render user text plus supported inbound context into a single prompt string."""

    composed = compose_forwarded_message_text(text, forwarded_from)
    sections: list[str] = []
    if composed:
        sections.append(composed)
    if include_reply_context:
        reply_block = _format_reply_context(reply_context)
        if reply_block:
            sections.append(reply_block)
    return "\n\n".join(section for section in sections if section).strip()
