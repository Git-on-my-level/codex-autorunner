from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .....core.pma_audit import PmaAuditLog
from .....core.pma_lane_worker import PmaLaneWorker
from .....core.pma_queue import PmaQueue
from .....core.pma_safety import PmaSafetyChecker, PmaSafetyConfig
from .....core.pma_state import PmaStateStore

logger = logging.getLogger(__name__)


def _resolve_context_hub_root(context: Any) -> Path:
    hub_root = getattr(context, "hub_root", None)
    if isinstance(hub_root, Path):
        return hub_root
    app = getattr(context, "app", None)
    config = getattr(getattr(app, "state", None), "config", None)
    hub_root = getattr(config, "root", None)
    if isinstance(hub_root, Path):
        return hub_root
    raise AttributeError("PMA context does not provide hub_root")


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

    async def get_pma_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        return self.get_lock(loop)

    async def get_interrupt_event(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        async with await self.get_pma_lock():
            if (
                self.pma_event is None
                or self.pma_event.is_set()
                or self.pma_event_loop is None
                or self.pma_event_loop is not loop
                or self.pma_event_loop.is_closed()
            ):
                self.pma_event = asyncio.Event()
                self.pma_event_loop = loop
            return self.pma_event

    async def clear_interrupt_event(self) -> None:
        async with await self.get_pma_lock():
            self.pma_event = None
            self.pma_event_loop = None

    def get_state_store(self, hub_root: Path) -> PmaStateStore:
        if self.pma_state_store is None or self.pma_state_root != hub_root:
            self.pma_state_store = PmaStateStore(hub_root)
            self.pma_state_root = hub_root
        return self.pma_state_store

    def get_safety_checker(
        self, hub_root: Path, context: Optional[Any] = None
    ) -> PmaSafetyChecker:
        supervisor = None
        if context is not None:
            supervisor = getattr(context, "hub_supervisor", None)
        if supervisor is not None:
            try:
                checker = supervisor.ensure_pma_safety_checker()
                if isinstance(checker, PmaSafetyChecker):
                    return checker
            except (
                Exception
            ):  # intentional: supervisor is external, fallback to local checker
                logger.debug(
                    "Failed to get safety checker from supervisor", exc_info=True
                )

        if self.pma_safety_checker is None or self.pma_safety_root != hub_root:
            raw = getattr(context, "raw_config", {}) if context else {}
            pma_config = raw.get("pma", {}) if isinstance(raw, dict) else {}
            safety_config = PmaSafetyConfig(
                dedup_window_seconds=pma_config.get("dedup_window_seconds", 300),
                max_duplicate_actions=pma_config.get("max_duplicate_actions", 3),
                rate_limit_window_seconds=pma_config.get(
                    "rate_limit_window_seconds", 60
                ),
                max_actions_per_window=pma_config.get("max_actions_per_window", 20),
                circuit_breaker_threshold=pma_config.get(
                    "circuit_breaker_threshold", 5
                ),
                circuit_breaker_cooldown_seconds=pma_config.get(
                    "circuit_breaker_cooldown_seconds", 600
                ),
                enable_dedup=pma_config.get("enable_dedup", True),
                enable_rate_limit=pma_config.get("enable_rate_limit", True),
                enable_circuit_breaker=pma_config.get("enable_circuit_breaker", True),
            )
            self.pma_audit_log = PmaAuditLog(hub_root)
            self.pma_safety_checker = PmaSafetyChecker(hub_root, config=safety_config)
            self.pma_safety_root = hub_root
        return self.pma_safety_checker

    def get_pma_queue(self, hub_root: Path) -> PmaQueue:
        if self.pma_queue is None or self.pma_queue_root != hub_root:
            self.pma_queue = PmaQueue(hub_root)
            self.pma_queue_root = hub_root
        return self.pma_queue

    async def set_active(
        self, active: bool, *, store: Optional[PmaStateStore] = None
    ) -> None:
        async with await self.get_pma_lock():
            self.pma_active = active
        if store is not None:
            await self._persist_state(store)

    async def begin_turn(
        self,
        client_turn_id: Optional[str],
        *,
        store: Optional[PmaStateStore] = None,
        lane_id: Optional[str] = None,
    ) -> bool:
        async with await self.get_pma_lock():
            if self.pma_active:
                return False
            self.pma_active = True
            self.pma_current = {
                "client_turn_id": client_turn_id or "",
                "status": "starting",
                "agent": None,
                "thread_id": None,
                "turn_id": None,
                "lane_id": lane_id or "",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        if store is not None:
            await self._persist_state(store)
        return True

    async def update_current(
        self, *, store: Optional[PmaStateStore] = None, **updates: Any
    ) -> None:
        async with await self.get_pma_lock():
            if self.pma_current is None:
                self.pma_current = {}
            self.pma_current.update(updates)
        if store is not None:
            await self._persist_state(store)

    async def get_current_snapshot(self) -> dict[str, Any]:
        async with await self.get_pma_lock():
            return dict(self.pma_current or {})

    async def _persist_state(self, store: PmaStateStore) -> None:
        from .....core.time_utils import now_iso

        async with await self.get_pma_lock():
            state = {
                "version": 1,
                "active": bool(self.pma_active),
                "current": dict(self.pma_current or {}),
                "last_result": dict(self.pma_last_result or {}),
                "updated_at": now_iso(),
            }
        try:
            store.save(state)
        except (OSError, RuntimeError, ValueError):
            logger.exception("Failed to persist PMA state")

    def cancel_background_task(self, task: asyncio.Task[Any], *, name: str) -> None:
        def consume_result(fut: asyncio.Future[Any]) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                return
            except Exception:  # intentional: consume arbitrary task failure
                logger.exception("PMA task failed: %s", name)

        if task.done():
            consume_result(task)
            return

        task.add_done_callback(consume_result)
        task.cancel()

    async def ensure_lane_worker(
        self,
        lane_id: str,
        context: Any,
        execute_callback: Any,
    ) -> None:
        existing = self.lane_workers.get(lane_id)
        if existing is not None and existing.is_running:
            return

        def _on_result(item: Any, result: dict[str, Any]) -> None:
            result_future = self.item_futures.get(item.item_id)
            if result_future and not result_future.done():
                result_future.set_result(result)
            self.item_futures.pop(item.item_id, None)

        queue = self.get_pma_queue(_resolve_context_hub_root(context))
        worker = PmaLaneWorker(
            lane_id,
            queue,
            execute_callback,
            log=logger,
            on_result=_on_result,
        )
        self.lane_workers[lane_id] = worker
        await worker.start()

    async def stop_lane_worker(self, lane_id: str) -> None:
        worker = self.lane_workers.get(lane_id)
        if worker is None:
            return
        await worker.stop()
        self.lane_workers.pop(lane_id, None)

    async def stop_all_lane_workers(self) -> None:
        for lane_id in list(self.lane_workers.keys()):
            await self.stop_lane_worker(lane_id)
