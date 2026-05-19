from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
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
_CAR_TICKET_ID_RE = re.compile(r"\bTICKET-\d+[A-Za-z0-9_-]*\b")
_THREAD_ID_RE = re.compile(r"^(?:thread|chat|run|exec|turn)[-_:][A-Za-z0-9_.:-]+$")
_PROTOCOL_ID_RE = re.compile(r"^(?:discord|telegram):\S+$", re.IGNORECASE)
_TRANSPORT_MARKER_RE = re.compile(
    r"(?is)<injected context>.*?</injected context>|"
    r"<CAR_TICKET_FLOW_PROMPT.*?(?:</CAR_TICKET_FLOW_PROMPT>|$)"
)


@dataclass(frozen=True)
class ManagedThreadTitleInputs:
    stored_title: Any = None
    provider_title: Any = None
    user_visible_title_seed: Any = None
    chat_display_name: Any = None
    ticket_title: Any = None
    ticket_id: Any = None
    run_title: Any = None
    run_id: Any = None
    fallback_id: Any = None


def normalize_thread_title(value: Any, *, limit: int = _TITLE_LIMIT) -> Optional[str]:
    """Return a compact one-line title, or ``None`` for empty/non-text values."""

    if not isinstance(value, str):
        return None
    value = _TRANSPORT_MARKER_RE.sub(" ", value)
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


def is_deprioritized_thread_title(value: Any) -> bool:
    title = normalize_thread_title(value)
    if title is None:
        return True
    lowered = title.casefold()
    if lowered.startswith("ticket-flow:"):
        return True
    if _PROTOCOL_ID_RE.match(title) or _THREAD_ID_RE.match(title):
        return True
    if _CAR_TICKET_ID_RE.fullmatch(title):
        return True
    try:
        uuid.UUID(title)
    except (TypeError, ValueError):
        return False
    return True


def is_usable_managed_thread_title(value: Any) -> bool:
    return not is_generic_thread_title(value) and not is_deprioritized_thread_title(
        value
    )


def resolve_managed_thread_display_title(
    inputs: ManagedThreadTitleInputs,
) -> Optional[str]:
    """Resolve backend-owned managed-thread display title precedence.

    Precedence favors explicit human titles, then provider titles, then visible
    user seeds. Technical ids and transport/control prompts are retained only as
    last-resort fallbacks.
    """

    fallback_id = normalize_thread_title(inputs.fallback_id)
    for candidate in (
        inputs.stored_title,
        inputs.provider_title,
        inputs.user_visible_title_seed,
        inputs.chat_display_name,
        inputs.ticket_title,
        inputs.run_title,
    ):
        title = normalize_thread_title(candidate)
        if (
            title is not None
            and title != f"Thread {fallback_id}"
            and is_usable_managed_thread_title(title)
        ):
            return title

    ticket_id = normalize_thread_title(inputs.ticket_id)
    if ticket_id:
        return f"Ticket flow · {ticket_id}"

    for candidate in (inputs.run_id, fallback_id, inputs.stored_title):
        title = normalize_thread_title(candidate)
        if title is not None:
            return title
    return None


def choose_owned_thread_title(
    current_title: Any,
    *,
    provider_title: Any = None,
    message_preview: Any = None,
    fallback: Any = None,
) -> Optional[str]:
    """Choose CAR's durable title without overwriting explicit existing titles."""

    return resolve_managed_thread_display_title(
        ManagedThreadTitleInputs(
            stored_title=current_title,
            provider_title=provider_title,
            user_visible_title_seed=message_preview,
            fallback_id=fallback,
        )
    )


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
