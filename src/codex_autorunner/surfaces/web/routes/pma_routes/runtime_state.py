from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .....core.pma_audit import PmaAuditLog
from .....core.pma_lane_worker import PmaLaneWorker
from .....core.pma_queue import PmaQueue
from .....core.pma_safety import PmaSafetyChecker
from .....core.pma_state import PmaStateStore


@dataclass
class PmaRuntimeState:
    pma_lock: Optional[asyncio.Lock] = field(default=None, repr=False)
    pma_lock_loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)
    pma_event: Optional[asyncio.Event] = field(default=None, repr=False)
    pma_event_loop: Optional[asyncio.AbstractEventLoop] = field(
        default=None, repr=False
    )
    pma_active: bool = False
    pma_current: Optional[dict[str, Any]] = None
    pma_last_result: Optional[dict[str, Any]] = None
    pma_state_store: Optional[PmaStateStore] = field(default=None, repr=False)
    pma_state_root: Optional[Path] = None
    pma_safety_checker: Optional[PmaSafetyChecker] = field(default=None, repr=False)
    pma_safety_root: Optional[Path] = None
    pma_audit_log: Optional[PmaAuditLog] = field(default=None, repr=False)
    pma_queue: Optional[PmaQueue] = field(default=None, repr=False)
    pma_queue_root: Optional[Path] = None
    pma_automation_store: Optional[Any] = field(default=None, repr=False)
    pma_automation_root: Optional[Path] = None
    lane_workers: dict[str, PmaLaneWorker] = field(default_factory=dict)
    item_futures: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict
    )

    def get_lock(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        if self.pma_lock is None or self.pma_lock_loop != loop:
            self.pma_lock = asyncio.Lock()
            self.pma_lock_loop = loop
        return self.pma_lock

    def get_event(self, loop: asyncio.AbstractEventLoop) -> asyncio.Event:
        if self.pma_event is None or self.pma_event_loop != loop:
            self.pma_event = asyncio.Event()
            self.pma_event_loop = loop
        return self.pma_event
