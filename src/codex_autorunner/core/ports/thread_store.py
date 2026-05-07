from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Protocol

from ..domain.refs import AgentRef, ScopeRef, SurfaceRef


class ThreadStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class ThreadRecord:
    thread_id: str
    scope: ScopeRef
    agent: AgentRef
    surface: Optional[SurfaceRef] = None
    backend_binding: dict[str, Any] = field(default_factory=dict)
    status: ThreadStatus = ThreadStatus.PENDING
    display_name: str = ""
    last_turn_id: Optional[str] = None
    last_execution_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ThreadStore(Protocol):
    async def create(self, record: ThreadRecord) -> ThreadRecord: ...

    async def get(self, thread_id: str) -> Optional[ThreadRecord]: ...

    async def list_by_scope(self, scope: ScopeRef) -> List[ThreadRecord]: ...

    async def update_status(
        self, thread_id: str, status: ThreadStatus
    ) -> Optional[ThreadRecord]: ...

    async def delete(self, thread_id: str) -> bool: ...


__all__ = [
    "ThreadRecord",
    "ThreadStatus",
    "ThreadStore",
]
