from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable

from ..car_context import CarContextProfile
from .models import (
    AgentDefinition,
    BusyThreadPolicy,
    ExecutionRecord,
    FlowRunTarget,
    FlowTarget,
    MessageRequest,
    MessageRequestKind,
    ThreadStopOutcome,
    ThreadTarget,
)


class FreshConversationRequiredError(RuntimeError):
    """Runtime signaled that the stored conversation binding must be replaced."""

    def __init__(
        self,
        message: str = "Stored conversation binding is invalid",
        *,
        conversation_id: Optional[str] = None,
        operation: Optional[str] = None,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.conversation_id = conversation_id
        self.operation = operation
        self.status_code = status_code


@runtime_checkable
class AgentDefinitionCatalog(Protocol):
    """Lookup surface for orchestration-visible agent definitions and capabilities."""

    def list_definitions(self) -> list[AgentDefinition]: ...

    def get_definition(self, agent_id: str) -> Optional[AgentDefinition]: ...


@runtime_checkable
class RuntimeConversationHandle(Protocol):
    """Structural handle returned by runtime backends for durable conversations."""

    id: str


@runtime_checkable
class RuntimeTurnHandle(Protocol):
    """Structural handle returned by runtime backends for individual turns."""

    conversation_id: str
    turn_id: str


@runtime_checkable
class RuntimeThreadHarness(Protocol):
    """Structural mirror of `agents.base.AgentHarness` for orchestration services.

    Core orchestration code depends on this protocol rather than importing agent
    implementations directly. Concrete harness objects returned by the existing
    runtime registry already satisfy this shape.
    """

    display_name: str
    capabilities: frozenset[str]

    async def ensure_ready(self, workspace_root: Path) -> None: ...

    async def backend_runtime_instance_id(
        self, workspace_root: Path
    ) -> Optional[str]: ...

    def supports(self, capability: str) -> bool: ...

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> RuntimeConversationHandle: ...

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> RuntimeConversationHandle: ...

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> RuntimeTurnHandle: ...

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> RuntimeTurnHandle: ...

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> Any: ...

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None: ...

    def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[Any]: ...


@runtime_checkable
class ThreadExecutionStore(Protocol):
    """Persistence boundary for orchestration thread targets and executions.

    `backend_thread_id` / `backend_runtime_instance_id` parameters are runtime
    binding inputs, not durable thread identity.
    """

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget: ...

    def get_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]: ...

    def list_thread_targets(
        self,
        *,
        agent_id: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        runtime_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ThreadTarget]: ...

    def resume_thread_target(
        self,
        thread_target_id: str,
        *,
        backend_thread_id: Optional[str] = None,
        backend_runtime_instance_id: Optional[str] = None,
    ) -> Optional[ThreadTarget]: ...

    def archive_thread_target(
        self, thread_target_id: str
    ) -> Optional[ThreadTarget]: ...

    def set_thread_backend_id(
        self,
        thread_target_id: str,
        backend_thread_id: Optional[str],
        *,
        backend_runtime_instance_id: Optional[str] = None,
    ) -> None: ...

    def create_execution(
        self,
        thread_target_id: str,
        *,
        prompt: str,
        request_kind: MessageRequestKind = "message",
        busy_policy: BusyThreadPolicy = "reject",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_request_id: Optional[str] = None,
        queue_payload: Optional[dict[str, Any]] = None,
    ) -> ExecutionRecord: ...

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]: ...

    def get_running_execution(
        self, thread_target_id: str
    ) -> Optional[ExecutionRecord]: ...

    def get_latest_execution(
        self, thread_target_id: str
    ) -> Optional[ExecutionRecord]: ...

    def list_queued_executions(
        self, thread_target_id: str, *, limit: int = 200
    ) -> list[ExecutionRecord]: ...

    def get_queue_depth(self, thread_target_id: str) -> int: ...

    def claim_next_queued_execution(
        self, thread_target_id: str
    ) -> Optional[tuple[ExecutionRecord, dict[str, Any]]]: ...

    def set_execution_backend_id(
        self, execution_id: str, backend_turn_id: Optional[str]
    ) -> None: ...

    def record_execution_result(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        status: str,
        assistant_text: Optional[str] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
    ) -> ExecutionRecord: ...

    def record_execution_interrupted(
        self, thread_target_id: str, execution_id: str
    ) -> ExecutionRecord: ...

    def cancel_queued_executions(self, thread_target_id: str) -> int: ...

    def record_thread_activity(
        self,
        thread_target_id: str,
        *,
        execution_id: Optional[str],
        message_preview: Optional[str],
    ) -> None: ...


@runtime_checkable
class OrchestrationThreadService(Protocol):
    """Canonical runtime-thread service boundary for CAR orchestration."""

    def list_agent_definitions(self) -> list[AgentDefinition]: ...

    def get_agent_definition(self, agent_id: str) -> Optional[AgentDefinition]: ...

    def get_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]: ...

    def list_thread_targets(
        self,
        *,
        agent_id: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        runtime_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ThreadTarget]: ...

    def get_thread_status(self, thread_target_id: str) -> Optional[str]: ...

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget: ...

    def resolve_thread_target(
        self,
        *,
        thread_target_id: Optional[str],
        agent_id: str,
        workspace_root: Path,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget: ...

    def resume_thread_target(
        self,
        thread_target_id: str,
        *,
        backend_thread_id: Optional[str] = None,
        backend_runtime_instance_id: Optional[str] = None,
    ) -> ThreadTarget: ...

    async def resolve_backend_runtime_instance_id(
        self, agent_id: str, workspace_root: Path
    ) -> Optional[str]: ...

    def archive_thread_target(self, thread_target_id: str) -> ThreadTarget: ...

    async def send_message(
        self,
        request: MessageRequest,
        *,
        client_request_id: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
    ) -> ExecutionRecord: ...

    async def interrupt_thread(self, thread_target_id: str) -> ExecutionRecord: ...

    async def stop_thread(self, thread_target_id: str) -> ThreadStopOutcome: ...

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]: ...

    def get_running_execution(
        self, thread_target_id: str
    ) -> Optional[ExecutionRecord]: ...

    def get_latest_execution(
        self, thread_target_id: str
    ) -> Optional[ExecutionRecord]: ...

    def list_queued_executions(
        self, thread_target_id: str, *, limit: int = 200
    ) -> list[ExecutionRecord]: ...

    def get_queue_depth(self, thread_target_id: str) -> int: ...

    def record_execution_result(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        status: str,
        assistant_text: Optional[str] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
    ) -> ExecutionRecord: ...

    def record_execution_interrupted(
        self, thread_target_id: str, execution_id: str
    ) -> ExecutionRecord: ...

    def cancel_queued_executions(self, thread_target_id: str) -> int: ...


@runtime_checkable
class OrchestrationFlowService(Protocol):
    """Canonical flow-target service boundary for CAR-native orchestration."""

    def list_flow_targets(self) -> list[FlowTarget]: ...

    def get_flow_target(self, flow_target_id: str) -> Optional[FlowTarget]: ...

    async def start_flow_run(
        self,
        flow_target_id: str,
        *,
        input_data: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> FlowRunTarget: ...

    async def resume_flow_run(
        self, run_id: str, *, force: bool = False
    ) -> FlowRunTarget: ...

    async def stop_flow_run(self, run_id: str) -> FlowRunTarget: ...

    def get_flow_run(self, run_id: str) -> Optional[FlowRunTarget]: ...

    def list_flow_runs(
        self, *, flow_target_id: Optional[str] = None
    ) -> list[FlowRunTarget]: ...

    def list_active_flow_runs(
        self, *, flow_target_id: Optional[str] = None
    ) -> list[FlowRunTarget]: ...


__all__ = [
    "AgentDefinitionCatalog",
    "FreshConversationRequiredError",
    "OrchestrationFlowService",
    "OrchestrationThreadService",
    "RuntimeConversationHandle",
    "RuntimeThreadHarness",
    "RuntimeTurnHandle",
    "ThreadExecutionStore",
]
