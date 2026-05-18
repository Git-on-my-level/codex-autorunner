from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .flows import TicketFlowTargetWrapper
from .interfaces import OrchestrationFlowService
from .models import FlowRunTarget, FlowTarget


@dataclass
class FlowBackedOrchestrationService(OrchestrationFlowService):
    """Canonical orchestration service boundary for CAR-native flow targets."""

    flow_wrappers: Mapping[str, TicketFlowTargetWrapper]

    def list_flow_targets(self) -> list[FlowTarget]:
        return [wrapper.flow_target for wrapper in self.flow_wrappers.values()]

    def get_flow_target(self, flow_target_id: str) -> Optional[FlowTarget]:
        wrapper = self.flow_wrappers.get(flow_target_id)
        if wrapper is None:
            return None
        return wrapper.flow_target

    def _require_wrapper(self, flow_target_id: str) -> TicketFlowTargetWrapper:
        wrapper = self.flow_wrappers.get(flow_target_id)
        if wrapper is None:
            raise KeyError(f"Unknown flow target '{flow_target_id}'")
        return wrapper

    def _find_wrapper_for_run(
        self, run_id: str
    ) -> tuple[Optional[TicketFlowTargetWrapper], Optional[FlowRunTarget]]:
        for wrapper in self.flow_wrappers.values():
            run = wrapper.get_run(run_id)
            if run is not None:
                return wrapper, run
        return None, None

    async def start_flow_run(
        self,
        flow_target_id: str,
        *,
        input_data: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> FlowRunTarget:
        return await self._require_wrapper(flow_target_id).start_run(
            input_data=input_data,
            metadata=metadata,
            run_id=run_id,
        )

    async def resume_flow_run(
        self, run_id: str, *, force: bool = False
    ) -> FlowRunTarget:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return await wrapper.resume_run(existing.run_id, force=force)

    async def stop_flow_run(self, run_id: str) -> FlowRunTarget:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return await wrapper.stop_run(existing.run_id)

    def ensure_flow_run_worker(self, run_id: str, *, is_terminal: bool = False) -> None:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        wrapper.ensure_run_worker(existing.run_id, is_terminal=is_terminal)

    def reconcile_flow_run(self, run_id: str) -> tuple[FlowRunTarget, bool, bool]:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return wrapper.reconcile_run(existing.run_id)

    async def wait_for_flow_run_terminal(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.25,
    ) -> Optional[FlowRunTarget]:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return await wrapper.wait_for_terminal(
            existing.run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def retire_flow_run(
        self,
        run_id: str,
        *,
        force: bool = False,
        delete_run: bool = True,
    ) -> dict[str, Any]:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return wrapper.retire_run(
            existing.run_id,
            force=force,
            delete_run=delete_run,
        )

    def get_flow_run(self, run_id: str) -> Optional[FlowRunTarget]:
        _, run = self._find_wrapper_for_run(run_id)
        return run

    def list_flow_runs(
        self, *, flow_target_id: Optional[str] = None
    ) -> list[FlowRunTarget]:
        if flow_target_id is not None:
            wrapper = self.flow_wrappers.get(flow_target_id)
            return [] if wrapper is None else wrapper.list_runs()

        runs: list[FlowRunTarget] = []
        for wrapper in self.flow_wrappers.values():
            runs.extend(wrapper.list_runs())
        return runs

    def list_active_flow_runs(
        self, *, flow_target_id: Optional[str] = None
    ) -> list[FlowRunTarget]:
        if flow_target_id is not None:
            wrapper = self.flow_wrappers.get(flow_target_id)
            return [] if wrapper is None else wrapper.list_active_runs()

        active_runs: list[FlowRunTarget] = []
        for wrapper in self.flow_wrappers.values():
            active_runs.extend(wrapper.list_active_runs())
        return active_runs
