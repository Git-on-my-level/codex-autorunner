import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from .models import FlowRunRecord, FlowRunStatus

_logger = logging.getLogger(__name__)


class StepOutcome:
    def __init__(
        self,
        status: FlowRunStatus,
        next_steps: Optional[Set[str]] = None,
        output: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ):
        self.status = status
        self.next_steps = next_steps or set()
        self.output = output or {}
        self.error = error

    @classmethod
    def continue_to(
        cls, next_steps: Set[str], output: Optional[Dict[str, Any]] = None
    ) -> "StepOutcome":
        return cls(status=FlowRunStatus.RUNNING, next_steps=next_steps, output=output)

    @classmethod
    def complete(cls, output: Optional[Dict[str, Any]] = None) -> "StepOutcome":
        return cls(status=FlowRunStatus.COMPLETED, output=output)

    @classmethod
    def fail(cls, error: str) -> "StepOutcome":
        return cls(status=FlowRunStatus.FAILED, error=error)

    @classmethod
    def stop(cls, output: Optional[Dict[str, Any]] = None) -> "StepOutcome":
        return cls(status=FlowRunStatus.STOPPED, output=output)


StepFn = Callable[[FlowRunRecord, Dict[str, Any]], Awaitable[StepOutcome]]


class FlowDefinition:
    def __init__(
        self,
        flow_type: str,
        initial_step: str,
        steps: Dict[str, StepFn],
    ):
        self.flow_type = flow_type
        self.initial_step = initial_step
        self.steps = steps

    def validate(self) -> None:
        if self.initial_step not in self.steps:
            raise ValueError(
                f"Initial step '{self.initial_step}' not found in steps: {list(self.steps.keys())}"
            )

        for step_id, step_fn in self.steps.items():
            if not asyncio.iscoroutinefunction(step_fn):
                raise ValueError(
                    f"Step function for '{step_id}' must be async (coroutine function)"
                )
