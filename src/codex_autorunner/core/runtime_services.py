from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..tickets import AgentPool
from .flows import FlowController

logger = logging.getLogger(__name__)


@dataclass
class _FlowRuntimeResources:
    controller: FlowController
    agent_pool: AgentPool


FlowRuntimeBuilder = Callable[[Path], _FlowRuntimeResources]


class RuntimeServices:
    """Shared lifecycle owner for long-lived runtime resources."""

    def __init__(
        self,
        *,
        app_server_supervisor: Optional[object] = None,
        opencode_supervisor: Optional[object] = None,
        preview_service_manager: Optional[object] = None,
        flow_runtime_builder: Optional[FlowRuntimeBuilder] = None,
    ) -> None:
        self.app_server_supervisor = app_server_supervisor
        self.opencode_supervisor = opencode_supervisor
        self.preview_service_manager = preview_service_manager
        self._flow_runtime_builder = flow_runtime_builder
        self._flow_runtimes: dict[Path, _FlowRuntimeResources] = {}
        self._owned_supervisors: list[object] = []
        self._lock = asyncio.Lock()
        self._closed = False

    def register_owned_supervisor(self, supervisor: object) -> None:
        """Register an additional long-lived supervisor closed by this service."""
        if not any(supervisor is owned for owned in self._owned_supervisors):
            self._owned_supervisors.append(supervisor)

    def ensure_ticket_flow_controller(self, repo_root: Path) -> FlowController:
        repo_root = repo_root.resolve()
        cached = self._flow_runtimes.get(repo_root)
        if cached is not None:
            return cached.controller
        if self._flow_runtime_builder is None:
            raise RuntimeError("ticket-flow runtime builder not configured")
        resources = self._flow_runtime_builder(repo_root)
        self._flow_runtimes[repo_root] = resources
        return resources.controller

    def get_ticket_flow_controller(self, repo_root: Path) -> FlowController:
        return self.ensure_ticket_flow_controller(repo_root)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True

            flow_runtimes = list(self._flow_runtimes.values())
            self._flow_runtimes.clear()

            for resources in flow_runtimes:
                try:
                    resources.controller.shutdown()
                except Exception:  # intentional: cleanup must not propagate exceptions
                    logger.debug("error shutting down flow controller", exc_info=True)
                try:
                    await resources.agent_pool.close_all()
                except Exception:  # intentional: cleanup must not propagate exceptions
                    logger.debug("error closing agent pool", exc_info=True)

            supervisors: list[object] = []
            for supervisor in (
                self.app_server_supervisor,
                self.opencode_supervisor,
                self.preview_service_manager,
                *self._owned_supervisors,
            ):
                if supervisor is None:
                    continue
                if any(supervisor is existing for existing in supervisors):
                    continue
                supervisors.append(supervisor)
            self._owned_supervisors.clear()

            for supervisor in supervisors:
                close_all = getattr(supervisor, "close_all", None)
                if callable(close_all):
                    try:
                        await close_all()
                    except (
                        Exception
                    ):  # intentional: cleanup must not propagate exceptions
                        logger.debug("error closing supervisor", exc_info=True)
