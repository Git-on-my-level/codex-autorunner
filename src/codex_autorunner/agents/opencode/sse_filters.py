"""OpenCode SSE event classification for CAR progress and stall logic."""

from __future__ import annotations

# Transport and bookkeeping events on /global/event that must not drive
# transcript rows, stall timers, or synthetic busy heartbeats.
_OPENCODE_NOISE_EVENT_TYPES = frozenset(
    {
        "server.connected",
        "server.heartbeat",
        "sync",
    }
)

_OPENCODE_NOISE_EVENT_PREFIXES = ("session.next.",)


def opencode_sse_event_is_noise(event_type: str) -> bool:
    normalized = str(event_type or "").strip().lower()
    if not normalized:
        return True
    if normalized in _OPENCODE_NOISE_EVENT_TYPES:
        return True
    return any(
        normalized.startswith(prefix) for prefix in _OPENCODE_NOISE_EVENT_PREFIXES
    )
