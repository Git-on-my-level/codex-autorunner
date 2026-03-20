"""Helpers for rendering forwarded inbound messages."""

from __future__ import annotations

from typing import Optional

from .models import ChatForwardInfo


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
