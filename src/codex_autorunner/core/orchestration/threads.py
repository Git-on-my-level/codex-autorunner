from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..car_context import CarContextProfile


@dataclass(frozen=True)
class SurfaceThreadMessageRequest:
    """Surface-originated runtime-thread message submission."""

    surface_kind: str
    workspace_root: Path
    prompt_text: str
    agent_id: Optional[str] = None
    pma_enabled: bool = False
    input_items: Optional[list[dict[str, Any]]] = None
    context_profile: Optional[CarContextProfile] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThreadControlRequest:
    """Surface-originated thread control action routed through orchestration."""

    surface_kind: str
    action: str
    target_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["SurfaceThreadMessageRequest", "ThreadControlRequest"]
