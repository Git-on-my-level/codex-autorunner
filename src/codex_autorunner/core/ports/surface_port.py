from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, List, Optional, Protocol

from ..domain.refs import ParticipantRef, SurfaceRef


class SurfaceHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SurfaceCapabilities:
    surface: SurfaceRef
    supports_threads: bool = False
    supports_reactions: bool = False
    supports_files: bool = False
    supports_typing_indicator: bool = False
    max_message_length: Optional[int] = None
    features: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SurfaceHealth:
    surface: SurfaceRef
    status: SurfaceHealthStatus = SurfaceHealthStatus.HEALTHY
    message: str = ""
    checked_at: str = ""


@dataclass(frozen=True)
class InboundEvent:
    surface: SurfaceRef
    event_id: str
    event_type: str
    timestamp: str
    participant: Optional[ParticipantRef] = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineCommand:
    command_type: str
    target: Optional[SurfaceRef] = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundDelivery:
    delivery_id: str
    surface: SurfaceRef
    status: str = "pending"
    error: Optional[str] = None


class SurfacePort(Protocol):
    async def capabilities(self, surface: SurfaceRef) -> SurfaceCapabilities: ...

    async def health(self, surface: SurfaceRef) -> SurfaceHealth: ...

    async def send(self, command: EngineCommand) -> OutboundDelivery: ...

    def receive(self, surface: SurfaceRef) -> AsyncGenerator[InboundEvent, None]: ...


__all__ = [
    "EngineCommand",
    "InboundEvent",
    "OutboundDelivery",
    "SurfaceCapabilities",
    "SurfaceHealth",
    "SurfaceHealthStatus",
    "SurfacePort",
]
