from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Protocol

from ..domain.refs import ScopeRef, TicketRef


class TicketStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TicketRecord:
    ref: TicketRef
    title: str
    status: TicketStatus = TicketStatus.PENDING
    agent: Optional[str] = None
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class TicketStore(Protocol):
    async def create(self, record: TicketRecord) -> TicketRecord: ...

    async def get(self, ref: TicketRef) -> Optional[TicketRecord]: ...

    async def list_by_scope(self, scope: ScopeRef) -> List[TicketRecord]: ...

    async def update_status(
        self, ref: TicketRef, status: TicketStatus
    ) -> Optional[TicketRecord]: ...

    async def delete(self, ref: TicketRef) -> bool: ...


__all__ = [
    "TicketRecord",
    "TicketStatus",
    "TicketStore",
]
