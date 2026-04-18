from __future__ import annotations

from typing import Any, Dict, Iterator, Optional

from .....core.sse import format_sse


def shape_stream_events(
    result: Dict[str, Any],
    *,
    client_turn_id: Optional[str] = None,
) -> Iterator[str]:
    status = result.get("status")

    if status == "ok":
        raw_events = result.pop("raw_events", None) or []
        for event in raw_events:
            yield format_sse("app-server", event)
        usage_parts = result.pop("usage_parts", None) or []
        for usage in usage_parts:
            yield format_sse("token_usage", usage)
        result["client_turn_id"] = client_turn_id or ""
        yield format_sse("update", result)
        yield format_sse("done", {"status": "ok"})
    elif status == "interrupted":
        yield format_sse(
            "interrupted",
            {"detail": result.get("detail") or "File chat interrupted"},
        )
    else:
        yield format_sse(
            "error",
            {"detail": result.get("detail") or "File chat failed"},
        )


def shape_stream_error(detail: str = "File chat failed") -> str:
    return format_sse("error", {"detail": detail})


def shape_stream_queued() -> str:
    return format_sse("status", {"status": "queued"})
