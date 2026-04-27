from __future__ import annotations

from typing import Any


def harness_supports_event_streaming(harness: Any) -> bool:
    supports = getattr(harness, "supports", None)
    if not callable(supports) or not bool(supports("event_streaming")):
        return False
    gate = getattr(harness, "allows_parallel_event_stream", None)
    if callable(gate):
        return bool(gate())
    return True


__all__ = ["harness_supports_event_streaming"]
