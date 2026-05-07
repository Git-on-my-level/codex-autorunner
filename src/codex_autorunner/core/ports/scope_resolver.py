from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol

from ..domain.refs import ScopeRef


@dataclass(frozen=True)
class ResolvedScope:
    scope: ScopeRef
    display_name: str = ""
    workspace_root: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ScopeResolver(Protocol):
    def resolve(self, ref: ScopeRef) -> ResolvedScope: ...

    def resolve_parent(self, ref: ScopeRef) -> Optional[ScopeRef]: ...

    def resolve_children(self, ref: ScopeRef) -> List[ScopeRef]: ...


__all__ = [
    "ResolvedScope",
    "ScopeResolver",
]
