from __future__ import annotations

from typing import Iterable, NewType

RuntimeCapability = NewType("RuntimeCapability", str)

RUNTIME_CAPABILITIES = frozenset(
    [
        RuntimeCapability("durable_threads"),
        RuntimeCapability("message_turns"),
        RuntimeCapability("interrupt"),
        RuntimeCapability("active_thread_discovery"),
        RuntimeCapability("transcript_history"),
        RuntimeCapability("review"),
        RuntimeCapability("model_listing"),
        RuntimeCapability("event_streaming"),
        RuntimeCapability("approvals"),
    ]
)


def normalize_runtime_capabilities(
    capabilities: Iterable[str],
) -> frozenset[RuntimeCapability]:
    normalized: set[RuntimeCapability] = set()
    for capability in capabilities:
        text = str(capability or "").strip().lower()
        if not text:
            continue
        normalized.add(RuntimeCapability(text))
    return frozenset(normalized)


__all__ = [
    "RUNTIME_CAPABILITIES",
    "RuntimeCapability",
    "normalize_runtime_capabilities",
]
