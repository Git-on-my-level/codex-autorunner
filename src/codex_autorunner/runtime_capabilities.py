from __future__ import annotations

from typing import Iterable, NewType

RuntimeCapability = NewType("RuntimeCapability", str)


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


__all__ = ["RuntimeCapability", "normalize_runtime_capabilities"]
