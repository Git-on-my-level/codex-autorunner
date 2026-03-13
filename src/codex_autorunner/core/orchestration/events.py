from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

from .models import TargetKind

OrchestrationEventType = Literal[
    "ingress.received",
    "ingress.target_resolved",
    "ingress.thread_submitted",
    "ingress.flow_resumed",
    "ingress.control_requested",
    "ingress.control_completed",
]


@dataclass(frozen=True)
class OrchestrationEvent:
    """Normalized event emitted by the shared orchestration ingress."""

    event_type: OrchestrationEventType
    target_kind: TargetKind
    surface_kind: str
    target_id: Optional[str] = None
    status: Optional[str] = None
    detail: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["OrchestrationEvent", "OrchestrationEventType"]
