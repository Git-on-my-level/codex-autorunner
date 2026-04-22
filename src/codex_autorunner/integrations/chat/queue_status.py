from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

QUEUE_STATUS_ITEM_LIMIT = 5
QUEUE_STATUS_PREVIEW_LIMIT = 72


@dataclass(frozen=True)
class QueueStatusItem:
    item_id: str
    preview: Optional[str] = None


def build_queue_item_preview(
    text: Optional[str],
    *,
    fallback: str = "Untitled request",
    limit: int = QUEUE_STATUS_PREVIEW_LIMIT,
) -> str:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return fallback
    if len(normalized) <= limit:
        return normalized
    clipped = normalized[: max(0, limit - 1)].rstrip()
    return f"{clipped}…" if clipped else fallback


def coerce_queue_status_items(
    raw_items: Iterable[object],
    *,
    fallback_prefix: str = "Request",
) -> list[QueueStatusItem]:
    items: list[QueueStatusItem] = []
    for raw_item in raw_items:
        item_id: Optional[str] = None
        preview: Optional[str] = None
        if isinstance(raw_item, QueueStatusItem):
            item_id = raw_item.item_id
            preview = raw_item.preview
        elif isinstance(raw_item, dict):
            candidate_id = raw_item.get("item_id")
            if isinstance(candidate_id, str):
                item_id = candidate_id.strip()
            candidate_preview = raw_item.get("preview")
            if isinstance(candidate_preview, str):
                preview = candidate_preview.strip() or None
        if not item_id:
            continue
        items.append(
            QueueStatusItem(
                item_id=item_id,
                preview=build_queue_item_preview(
                    preview,
                    fallback=f"{fallback_prefix} {item_id}",
                ),
            )
        )
    return items


def format_queue_status_text(
    items: Sequence[QueueStatusItem],
    *,
    busy_label: Optional[str] = None,
    item_limit: int = QUEUE_STATUS_ITEM_LIMIT,
) -> str:
    visible_items = list(items[: max(1, item_limit)])
    count = len(items)
    header = f"Queued requests ({count})"
    if busy_label:
        header = f"{header} behind {busy_label}"
    lines = [header]
    for index, item in enumerate(visible_items, start=1):
        lines.append(f"{index}. {item.preview or f'Request {item.item_id}'}")
    hidden = count - len(visible_items)
    if hidden > 0:
        noun = "request" if hidden == 1 else "requests"
        lines.append(f"...and {hidden} more {noun}.")
    return "\n".join(lines)
