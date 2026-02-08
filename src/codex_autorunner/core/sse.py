"""Server-Sent Events (SSE) parsing and formatting utilities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass(frozen=True)
class SSEEvent:
    """A Server-Sent Event."""

    event: str
    data: str
    id: Optional[str] = None
    retry: Optional[int] = None


def format_sse(event: str, data: object) -> str:
    """Format a Server-Sent Event message.

    Args:
        event: The event name.
        data: The event data. If a string, it's used as-is; otherwise, it's
            JSON-encoded.

    Returns:
        A formatted SSE message string.
    """
    payload = data if isinstance(data, str) else json.dumps(data)
    lines = payload.splitlines() or [""]
    parts = [f"event: {event}"]
    for line in lines:
        parts.append(f"data: {line}")
    return "\n".join(parts) + "\n\n"


async def parse_sse_lines(lines: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    """Parse Server-Sent Events from an async line iterator.

    Args:
        lines: An async iterator of SSE text lines.

    Yields:
        Parsed SSEEvent instances.
    """
    event_name = "message"
    data_lines: list[str] = []
    event_id: Optional[str] = None
    retry_value: Optional[int] = None

    async for line in lines:
        if not line:
            if data_lines or event_id is not None or retry_value is not None:
                yield SSEEvent(
                    event=event_name or "message",
                    data="\n".join(data_lines),
                    id=event_id,
                    retry=retry_value,
                )
            event_name = "message"
            data_lines = []
            event_id = None
            retry_value = None
            continue

        if line.startswith(":"):
            continue

        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""

        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            event_id = value
        elif field == "retry":
            try:
                retry_value = int(value)
            except ValueError:
                retry_value = None

    if data_lines or event_id is not None or retry_value is not None:
        yield SSEEvent(
            event=event_name or "message",
            data="\n".join(data_lines),
            id=event_id,
            retry=retry_value,
        )


__all__ = ["SSEEvent", "format_sse", "parse_sse_lines"]
