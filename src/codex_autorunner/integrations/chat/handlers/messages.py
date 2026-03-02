"""Platform-agnostic message handler helpers."""

from __future__ import annotations

from typing import Any, Optional, Sequence


def message_text_candidate(
    *,
    text: Optional[str],
    caption: Optional[str],
    entities: Sequence[Any],
    caption_entities: Sequence[Any],
) -> tuple[str, str, Sequence[Any]]:
    """Pick message text/caption and the matching entity set."""

    raw_text = text or ""
    raw_caption = caption or ""
    text_candidate = raw_text if raw_text.strip() else raw_caption
    active_entities = entities if raw_text.strip() else caption_entities
    return raw_text, text_candidate, active_entities
