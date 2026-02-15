from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..tickets import AgentPool
from .flows import FlowController


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
        flow_runtime_builder: Optional[FlowRuntimeBuilder] = None,
    ) -> None:
        self.app_server_supervisor = app_server_supervisor
        self.opencode_supervisor = opencode_supervisor
        self._flow_runtime_builder = flow_runtime_builder
        self._flow_runtimes: dict[Path, _FlowRuntimeResources] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def get_ticket_flow_controller(self, repo_root: Path) -> FlowController:
        repo_root = repo_root.resolve()
        cached = self._flow_runtimes.get(repo_root)
        if cached is not None:
            return cached.controller
        if self._flow_runtime_builder is None:
            raise RuntimeError("ticket-flow runtime builder not configured")
        resources = self._flow_runtime_builder(repo_root)
        self._flow_runtimes[repo_root] = resources
        return resources.controller

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
                except Exception:
                    pass
                try:
                    await resources.agent_pool.close_all()
                except Exception:
                    pass

            for supervisor in {self.app_server_supervisor, self.opencode_supervisor}:
                if supervisor is None:
                    continue
                close_all = getattr(supervisor, "close_all", None)
                if callable(close_all):
                    try:
                        await close_all()
                    except Exception:
                        pass
