from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import FlowTarget


@dataclass(frozen=True)
class PausedFlowTarget:
    """Resolved paused flow target that can accept a conversational reply."""

    flow_target: FlowTarget
    run_id: str
    status: Optional[str] = None
    workspace_root: Optional[Path] = None


__all__ = ["PausedFlowTarget"]
