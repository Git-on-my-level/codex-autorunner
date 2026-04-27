from __future__ import annotations

from typing import Any


def harness_supports_event_streaming(harness: Any) -> bool:
    supports = getattr(harness, "supports", None)
    return callable(supports) and bool(supports("event_streaming"))


__all__ = ["harness_supports_event_streaming"]
