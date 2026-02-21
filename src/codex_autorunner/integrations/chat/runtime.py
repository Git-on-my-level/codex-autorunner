"""Chat-core runtime helper utilities."""

from __future__ import annotations

from typing import Optional


def iter_exception_chain(exc: BaseException) -> list[BaseException]:
    """Collect exception causes/contexts from outermost to innermost."""

    chain: list[BaseException] = []
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain
