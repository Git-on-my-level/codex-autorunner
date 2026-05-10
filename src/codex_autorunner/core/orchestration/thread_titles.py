from __future__ import annotations

import re
from typing import Any, Optional

_TITLE_LIMIT = 80
_GENERIC_TITLES = {
    "chat",
    "new chat",
    "new pma chat",
    "pma",
    "untitled",
    "untitled chat",
}


def normalize_thread_title(value: Any, *, limit: int = _TITLE_LIMIT) -> Optional[str]:
    """Return a compact one-line title, or ``None`` for empty/non-text values."""

    if not isinstance(value, str):
        return None
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "..."


def is_generic_thread_title(value: Any) -> bool:
    title = normalize_thread_title(value)
    if title is None:
        return True
    lowered = title.casefold()
    if lowered in _GENERIC_TITLES:
        return True
    return lowered.startswith("chat · ")


def choose_owned_thread_title(
    current_title: Any,
    *,
    provider_title: Any = None,
    message_preview: Any = None,
    fallback: Any = None,
) -> Optional[str]:
    """Choose CAR's durable title without overwriting explicit existing titles."""

    current = normalize_thread_title(current_title)
    if current is not None and not is_generic_thread_title(current):
        return current
    for candidate in (provider_title, message_preview, fallback, current):
        title = normalize_thread_title(candidate)
        if title is not None and not is_generic_thread_title(title):
            return title
    return current


def provider_title_metadata(
    *,
    provider_title: Any = None,
    provider_summary: Any = None,
) -> dict[str, str]:
    metadata: dict[str, str] = {}
    title = normalize_thread_title(provider_title)
    if title:
        metadata["provider_conversation_title"] = title
    summary = normalize_thread_title(provider_summary, limit=500)
    if summary:
        metadata["provider_conversation_summary"] = summary
    return metadata
