from __future__ import annotations

from typing import Iterable, NewType

RuntimeCapability = NewType("RuntimeCapability", str)

_LEGACY_RUNTIME_CAPABILITY_ALIASES = {
    "threads": "durable_threads",
    "turns": "message_turns",
    "session_resume": "durable_threads",
    "pma_thread_reset": "durable_threads",
    "conversation_compaction": "message_turns",
    "code_review": "review",
    "turn_control": "interrupt",
}


def normalize_runtime_capabilities(
    capabilities: Iterable[str],
) -> frozenset[RuntimeCapability]:
    normalized: set[RuntimeCapability] = set()
    for capability in capabilities:
        text = str(capability or "").strip().lower()
        if not text:
            continue
        # Preserve v1 plugin compatibility for older descriptors and call sites
        # while keeping canonical capability names everywhere else.
        text = _LEGACY_RUNTIME_CAPABILITY_ALIASES.get(text, text)
        normalized.add(RuntimeCapability(text))
    return frozenset(normalized)


__all__ = ["RuntimeCapability", "normalize_runtime_capabilities"]
