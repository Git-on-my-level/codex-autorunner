from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str
    id: Optional[str] = None
    retry: Optional[int] = None


async def parse_sse_lines(lines: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
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


__all__ = ["SSEEvent", "parse_sse_lines"]
