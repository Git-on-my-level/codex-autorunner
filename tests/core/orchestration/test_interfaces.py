from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from codex_autorunner.agents.types import TerminalTurnResult
from codex_autorunner.core.orchestration import (
    AgentDefinition,
    AgentDefinitionCatalog,
    ExecutionRecord,
    MessageRequest,
    OrchestrationThreadService,
    RuntimeThreadHarness,
    ThreadExecutionStore,
    ThreadTarget,
)


@dataclass
class _FakeCatalog:
    definitions: list[AgentDefinition]

    def list_definitions(self) -> list[AgentDefinition]:
        return list(self.definitions)

    def get_definition(self, agent_id: str) -> Optional[AgentDefinition]:
        for definition in self.definitions:
            if definition.agent_id == agent_id:
                return definition
        return None


@dataclass
class _FakeStore:
    thread: ThreadTarget
    execution: ExecutionRecord

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
    ) -> ThreadTarget:
        return self.thread

    def get_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        return self.thread if thread_target_id == self.thread.thread_target_id else None

    def list_thread_targets(
        self,
        *,
        agent_id: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        runtime_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ThreadTarget]:
        return [self.thread]

    def resume_thread_target(
        self, thread_target_id: str, *, backend_thread_id: str
    ) -> Optional[ThreadTarget]:
        return self.thread

    def archive_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        return self.thread

    def set_thread_backend_id(
        self, thread_target_id: str, backend_thread_id: Optional[str]
    ) -> None:
        return None

    def create_execution(
        self,
        thread_target_id: str,
        *,
        prompt: str,
        busy_policy: str = "reject",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_request_id: Optional[str] = None,
        queue_payload: Optional[dict[str, Any]] = None,
    ) -> ExecutionRecord:
        _ = busy_policy, queue_payload
        return self.execution

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]:
        return self.execution

    def get_running_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        return self.execution

    def get_latest_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        return self.execution

    def list_queued_executions(
        self, thread_target_id: str, *, limit: int = 200
    ) -> list[ExecutionRecord]:
        _ = thread_target_id, limit
        return []

    def get_queue_depth(self, thread_target_id: str) -> int:
        _ = thread_target_id
        return 0

    def claim_next_queued_execution(
        self, thread_target_id: str
    ) -> Optional[tuple[ExecutionRecord, dict[str, Any]]]:
        _ = thread_target_id
        return None

    def set_execution_backend_id(
        self, execution_id: str, backend_turn_id: Optional[str]
    ) -> None:
        return None

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
    ) -> ExecutionRecord:
        return self.execution

    def record_execution_interrupted(
        self, thread_target_id: str, execution_id: str
    ) -> ExecutionRecord:
        return self.execution

    def cancel_queued_executions(self, thread_target_id: str) -> int:
        _ = thread_target_id
        return 0

    def record_thread_activity(
        self,
        thread_target_id: str,
        *,
        execution_id: Optional[str],
        message_preview: Optional[str],
    ) -> None:
        return None


@dataclass
class _FakeConversation:
    id: str


@dataclass
class _FakeTurn:
    turn_id: str


@dataclass
class _FakeHarness:
    display_name: str = "Codex"
    capabilities: frozenset[str] = frozenset(
        ["durable_threads", "message_turns", "interrupt", "review"]
    )
    interrupted: list[tuple[Path, str, Optional[str]]] = field(default_factory=list)

    async def ensure_ready(self, workspace_root: Path) -> None:
        return None

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> _FakeConversation:
        return _FakeConversation(id="conversation-1")

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> _FakeConversation:
        return _FakeConversation(id=conversation_id)

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
    ) -> _FakeTurn:
        _ = input_items
        return _FakeTurn(turn_id="turn-1")

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
    ) -> _FakeTurn:
        return _FakeTurn(turn_id="turn-2")

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        self.interrupted.append((workspace_root, conversation_id, turn_id))

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = workspace_root, conversation_id, turn_id, timeout
        return TerminalTurnResult(status="ok", assistant_text="done")

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        if False:
            yield f"{workspace_root}:{conversation_id}:{turn_id}"


@dataclass
class _FakeService:
    catalog: _FakeCatalog
    store: _FakeStore

    def list_agent_definitions(self) -> list[AgentDefinition]:
        return self.catalog.list_definitions()

    def get_agent_definition(self, agent_id: str) -> Optional[AgentDefinition]:
        return self.catalog.get_definition(agent_id)

    def get_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        return self.store.get_thread_target(thread_target_id)

    def list_thread_targets(
        self,
        *,
        agent_id: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        runtime_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ThreadTarget]:
        return self.store.list_thread_targets(
            agent_id=agent_id,
            lifecycle_status=lifecycle_status,
            runtime_status=runtime_status,
            repo_id=repo_id,
            limit=limit,
        )

    def get_thread_status(self, thread_target_id: str) -> Optional[str]:
        target = self.get_thread_target(thread_target_id)
        return None if target is None else target.status

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
    ) -> ThreadTarget:
        return self.store.create_thread_target(
            agent_id,
            workspace_root,
            repo_id=repo_id,
            display_name=display_name,
            backend_thread_id=backend_thread_id,
        )

    def resume_thread_target(
        self, thread_target_id: str, *, backend_thread_id: str
    ) -> ThreadTarget:
        return (
            self.store.resume_thread_target(
                thread_target_id,
                backend_thread_id=backend_thread_id,
            )
            or self.store.thread
        )

    def archive_thread_target(self, thread_target_id: str) -> ThreadTarget:
        return self.store.archive_thread_target(thread_target_id) or self.store.thread

    def resolve_thread_target(
        self,
        *,
        thread_target_id: Optional[str],
        agent_id: str,
        workspace_root: Path,
        repo_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
    ) -> ThreadTarget:
        if thread_target_id:
            return self.store.get_thread_target(thread_target_id) or self.store.thread
        return self.create_thread_target(
            agent_id,
            workspace_root,
            repo_id=repo_id,
            display_name=display_name,
            backend_thread_id=backend_thread_id,
        )

    async def send_message(
        self,
        request: MessageRequest,
        *,
        client_request_id: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
    ) -> ExecutionRecord:
        return self.store.execution

    async def interrupt_thread(self, thread_target_id: str) -> ExecutionRecord:
        return self.store.execution

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]:
        return self.store.execution

    def get_running_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        return self.store.execution

    def get_latest_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        return self.store.execution

    def list_queued_executions(
        self, thread_target_id: str, *, limit: int = 200
    ) -> list[ExecutionRecord]:
        _ = thread_target_id, limit
        return []

    def get_queue_depth(self, thread_target_id: str) -> int:
        _ = thread_target_id
        return 0

    def cancel_queued_executions(self, thread_target_id: str) -> int:
        _ = thread_target_id
        return 0

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
    ) -> ExecutionRecord:
        return self.store.execution

    def record_execution_interrupted(
        self, thread_target_id: str, execution_id: str
    ) -> ExecutionRecord:
        return self.store.execution


def test_runtime_protocols_match_orchestration_contract() -> None:
    definition = AgentDefinition(
        agent_id="codex",
        display_name="Codex",
        runtime_kind="codex",
    )
    thread = ThreadTarget(
        thread_target_id="thread-1",
        agent_id="codex",
        backend_thread_id="backend-1",
        workspace_root="/tmp/workspace",
        status="running",
    )
    execution = ExecutionRecord(
        execution_id="exec-1",
        target_id="thread-1",
        target_kind="thread",
        status="running",
        backend_id="turn-1",
    )

    catalog = _FakeCatalog([definition])
    store = _FakeStore(thread=thread, execution=execution)
    harness = _FakeHarness()
    service = _FakeService(catalog=catalog, store=store)

    assert isinstance(catalog, AgentDefinitionCatalog)
    assert isinstance(store, ThreadExecutionStore)
    assert isinstance(harness, RuntimeThreadHarness)
    assert isinstance(service, OrchestrationThreadService)


def test_thread_service_contract_leaves_room_for_flow_targets() -> None:
    request = MessageRequest(
        target_id="flow-1",
        target_kind="flow",
        message_text="not a thread",
    )

    assert request.target_kind == "flow"
    assert request.to_dict()["target_kind"] == "flow"
