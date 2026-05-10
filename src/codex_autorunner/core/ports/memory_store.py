from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol

from ..domain.refs import MemoryRef, ScopeRef


@dataclass(frozen=True)
class MemoryDoc:
    key: str
    content: str
    content_type: str = "text/plain"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryDocs:
    scope: ScopeRef
    docs: List[MemoryDoc] = field(default_factory=list)


class MemoryStore(Protocol):
    async def load(self, ref: MemoryRef) -> Optional[MemoryDoc]: ...

    async def load_scope(self, scope: ScopeRef) -> MemoryDocs: ...

    async def save(self, ref: MemoryRef, doc: MemoryDoc) -> MemoryDoc: ...

    async def delete(self, ref: MemoryRef) -> bool: ...


__all__ = [
    "MemoryDoc",
    "MemoryDocs",
    "MemoryStore",
]
