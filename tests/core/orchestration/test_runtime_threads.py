from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.agents.types import TerminalTurnResult
from codex_autorunner.core.hub_control_plane import (
    ExecutionResponse,
    RemoteThreadExecutionStore,
    ThreadTargetResponse,
)
from codex_autorunner.core.orchestration import (
    HarnessBackedOrchestrationService,
    MappingAgentDefinitionCatalog,
    MessageRequest,
    PmaThreadExecutionStore,
)
from codex_autorunner.core.orchestration.models import ExecutionRecord, ThreadTarget
from codex_autorunner.core.orchestration.runtime_threads import (
    RUNTIME_THREAD_MISSING_BACKEND_IDS_ERROR,
    await_runtime_thread_outcome,
    begin_next_queued_runtime_thread_execution,
    begin_runtime_thread_execution,
    stream_runtime_thread_events,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore


@dataclass
class _FakeConversation:
    id: str


@dataclass
class _FakeTurn:
    conversation_id: str
    turn_id: str


@dataclass
class _WaitResult:
    agent_messages: list[str]
    raw_events: list[dict[str, Any]]
    errors: list[str]


@dataclass
class _HarnessWithWait:
    display_name: str = "Codex"
    capabilities: frozenset[str] = frozenset(
        ["durable_threads", "message_turns", "interrupt", "review"]
    )
    ensure_ready_calls: list[Path] = field(default_factory=list)
    start_turn_calls: list[dict[str, Any]] = field(default_factory=list)
    interrupt_calls: list[tuple[Path, str, Optional[str]]] = field(default_factory=list)
    wait_calls: list[tuple[Path, str, Optional[str]]] = field(default_factory=list)

    async def ensure_ready(self, workspace_root: Path) -> None:
        self.ensure_ready_calls.append(workspace_root)

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> _FakeConversation:
        _ = workspace_root, title
        return _FakeConversation(id="backend-thread-1")

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> _FakeConversation:
        _ = workspace_root
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
        self.start_turn_calls.append(
            {
                "workspace_root": workspace_root,
                "conversation_id": conversation_id,
                "prompt": prompt,
                "model": model,
                "reasoning": reasoning,
                "approval_mode": approval_mode,
                "sandbox_policy": sandbox_policy,
                "input_items": input_items,
            }
        )
        return _FakeTurn(conversation_id=conversation_id, turn_id="backend-turn-1")

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
        return await self.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        self.interrupt_calls.append((workspace_root, conversation_id, turn_id))

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = timeout
        self.wait_calls.append((workspace_root, conversation_id, turn_id))
        return TerminalTurnResult(
            status="ok",
            assistant_text="assistant-output",
            raw_events=[],
            errors=[],
        )

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        if False:
            yield f"{workspace_root}:{conversation_id}:{turn_id}"


@dataclass
class _HarnessWithBlockingWait(_HarnessWithWait):
    wait_started: asyncio.Event = field(default_factory=asyncio.Event)
    wait_cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = timeout
        self.wait_calls.append((workspace_root, conversation_id, turn_id))
        self.wait_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.wait_cancelled.set()
            raise


@dataclass
class _HarnessWithStream:
    display_name: str = "OpenCode"
    capabilities: frozenset[str] = frozenset(
        ["durable_threads", "message_turns", "interrupt", "event_streaming"]
    )
    ensure_ready_calls: list[Path] = field(default_factory=list)
    interrupt_calls: list[tuple[Path, str, Optional[str]]] = field(default_factory=list)
    wait_calls: list[tuple[Path, str, Optional[str]]] = field(default_factory=list)

    async def ensure_ready(self, workspace_root: Path) -> None:
        self.ensure_ready_calls.append(workspace_root)

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> _FakeConversation:
        _ = workspace_root, title
        return _FakeConversation(id="session-1")

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> _FakeConversation:
        _ = workspace_root
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
        _ = (
            workspace_root,
            prompt,
            model,
            reasoning,
            approval_mode,
            sandbox_policy,
            input_items,
        )
        return _FakeTurn(conversation_id=conversation_id, turn_id="stream-turn-1")

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
        return await self.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        self.interrupt_calls.append((workspace_root, conversation_id, turn_id))

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = timeout
        self.wait_calls.append((workspace_root, conversation_id, turn_id))
        return TerminalTurnResult(status="ok", assistant_text="hello world")

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        _ = workspace_root, conversation_id, turn_id
        yield {"message": {"method": "message.delta", "params": {"delta": "hello "}}}
        yield {"message": {"method": "message.delta", "params": {"delta": "world"}}}
        yield {
            "message": {
                "method": "message.completed",
                "params": {"text": "hello world"},
            }
        }


def _make_descriptor(
    agent_id: str = "codex", *, name: str = "Codex"
) -> AgentDescriptor:
    return AgentDescriptor(
        id=agent_id,
        name=name,
        capabilities=frozenset(["threads", "turns", "review"]),
        make_harness=lambda _ctx: None,  # type: ignore[return-value]
    )


def _build_service(
    tmp_path: Path,
    harness: Any,
    *,
    agent_id: str = "codex",
    name: str = "Codex",
) -> HarnessBackedOrchestrationService:
    descriptors = {agent_id: _make_descriptor(agent_id, name=name)}
    return HarnessBackedOrchestrationService(
        definition_catalog=MappingAgentDefinitionCatalog(descriptors),
        thread_store=PmaThreadExecutionStore(PmaThreadStore(tmp_path / "hub")),
        harness_factory=lambda resolved_agent_id: harness,
    )


class _SerializedHubClient:
    def __init__(self, store: PmaThreadExecutionStore) -> None:
        self._store = store

    @staticmethod
    def _serialize_thread(thread: ThreadTarget | None) -> ThreadTargetResponse:
        return ThreadTargetResponse.from_mapping(
            ThreadTargetResponse(thread=thread).to_dict()
        )

    @staticmethod
    def _serialize_execution(execution: ExecutionRecord | None) -> ExecutionResponse:
        payload = (
            None
            if execution is None
            else ExecutionResponse(execution=execution).to_dict()["execution"]
        )
        return ExecutionResponse.from_mapping({"execution": payload})

    async def create_thread_target(self, request: Any) -> ThreadTargetResponse:
        return self._serialize_thread(
            self._store.create_thread_target(
                request.agent_id,
                Path(request.workspace_root),
                repo_id=request.repo_id,
                resource_kind=request.resource_kind,
                resource_id=request.resource_id,
                display_name=request.display_name,
                backend_thread_id=request.backend_thread_id,
                context_profile=request.metadata.get("context_profile"),
                metadata=request.metadata,
            )
        )

    async def get_thread_target(self, request: Any) -> ThreadTargetResponse:
        return self._serialize_thread(
            self._store.get_thread_target(request.thread_target_id)
        )

    async def create_execution(self, request: Any) -> ExecutionResponse:
        return self._serialize_execution(
            self._store.create_execution(
                request.thread_target_id,
                prompt=request.prompt,
                request_kind=request.request_kind,
                busy_policy=request.busy_policy,
                model=request.model,
                reasoning=request.reasoning,
                client_request_id=request.client_request_id,
                queue_payload=request.queue_payload,
            )
        )

    async def get_execution(self, request: Any) -> ExecutionResponse:
        return self._serialize_execution(
            self._store.get_execution(request.thread_target_id, request.execution_id)
        )

    async def get_running_execution(self, request: Any) -> ExecutionResponse:
        return self._serialize_execution(
            self._store.get_running_execution(request.thread_target_id)
        )

    async def set_thread_backend_id(self, request: Any) -> None:
        self._store.set_thread_backend_id(
            request.thread_target_id,
            request.backend_thread_id,
            backend_runtime_instance_id=request.backend_runtime_instance_id,
        )

    async def set_execution_backend_id(self, request: Any) -> None:
        self._store.set_execution_backend_id(
            request.execution_id,
            request.backend_turn_id,
        )

    async def record_thread_activity(self, request: Any) -> None:
        self._store.record_thread_activity(
            request.thread_target_id,
            execution_id=request.execution_id,
            message_preview=request.message_preview,
        )


async def test_runtime_threads_begin_and_wait_with_agent_harness(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
            metadata={"runtime_prompt": "expanded orchestration prompt"},
        ),
        sandbox_policy="dangerFullAccess",
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=asyncio.Event(),
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert harness.ensure_ready_calls == [workspace_root]
    assert harness.start_turn_calls[0]["prompt"] == "expanded orchestration prompt"
    assert harness.wait_calls == [
        (workspace_root, "backend-thread-1", "backend-turn-1")
    ]
    assert outcome.status == "ok"
    assert outcome.assistant_text == "assistant-output"


async def test_runtime_threads_use_wait_for_turn_contract_for_session_runtimes(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithStream()
    service = _build_service(tmp_path, harness, agent_id="opencode", name="OpenCode")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("opencode", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="hello world",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=asyncio.Event(),
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert harness.wait_calls == [(workspace_root, "session-1", "stream-turn-1")]
    assert outcome.assistant_text == "hello world"


async def test_runtime_threads_begin_and_wait_via_serialized_remote_store(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    local_store = PmaThreadExecutionStore(PmaThreadStore(tmp_path / "hub"))
    remote_store = RemoteThreadExecutionStore(_SerializedHubClient(local_store))
    descriptors = {"codex": _make_descriptor("codex")}
    service = HarnessBackedOrchestrationService(
        definition_catalog=MappingAgentDefinitionCatalog(descriptors),
        thread_store=remote_store,
        harness_factory=lambda resolved_agent_id: harness,
    )
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=asyncio.Event(),
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert started.thread.agent_id == "codex"
    assert started.thread.backend_thread_id == "backend-thread-1"
    assert started.execution.backend_id == "backend-turn-1"
    assert harness.wait_calls == [
        (workspace_root, "backend-thread-1", "backend-turn-1")
    ]
    assert outcome.status == "ok"


async def test_runtime_threads_allow_missing_interrupt_event(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "assistant-output"


async def test_begin_next_queued_runtime_thread_execution_clears_claimed_turn_on_cancellation(
    tmp_path: Path,
) -> None:
    class _ClaimedStartAborted(BaseException):
        pass

    harness = _HarnessWithWait()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    first = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="first",
        )
    )
    queued = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="second",
        )
    )
    service.record_execution_result(
        thread.thread_target_id,
        first.execution_id,
        status="ok",
        assistant_text="done",
        backend_turn_id="backend-turn-1",
    )

    async def _cancelled_start_turn(
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
        _ = (
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode,
            sandbox_policy,
            input_items,
        )
        raise _ClaimedStartAborted("queue worker stopped")

    harness.start_turn = _cancelled_start_turn  # type: ignore[method-assign]

    try:
        await begin_next_queued_runtime_thread_execution(
            service, thread.thread_target_id
        )
    except _ClaimedStartAborted as exc:
        assert str(exc) == "queue worker stopped"
    else:
        raise AssertionError("Expected claimed queued turn start to be cancelled")

    refreshed = service.get_execution(thread.thread_target_id, queued.execution_id)
    assert refreshed is not None
    assert refreshed.status == "error"
    assert refreshed.error == "queue worker stopped"
    assert service.get_running_execution(thread.thread_target_id) is None
    assert service.get_queue_depth(thread.thread_target_id) == 0


async def test_runtime_threads_fail_cleanly_when_backend_ids_are_missing(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    broken_started = replace(
        started,
        execution=replace(started.execution, backend_id=None),
    )
    outcome = await await_runtime_thread_outcome(
        broken_started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "error"
    assert outcome.error == RUNTIME_THREAD_MISSING_BACKEND_IDS_ERROR
    assert harness.wait_calls == []


async def test_runtime_threads_stream_events_support_hermes_harness(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithStream()
    service = _build_service(tmp_path, harness, agent_id="hermes", name="Hermes")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("hermes", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="hello hermes",
        ),
    )
    events = [event async for event in stream_runtime_thread_events(started)]

    assert started.thread.agent_id == "hermes"
    assert started.thread.backend_thread_id == "session-1"
    assert events
    assert "message.completed" in events[-1]


async def test_runtime_threads_allow_wait_results_without_raw_events(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()

    async def _wait_without_raw_events(
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = timeout
        harness.wait_calls.append((workspace_root, conversation_id, turn_id))
        return SimpleNamespace(
            status="ok", assistant_text="assistant-output", errors=[]
        )

    harness.wait_for_turn = _wait_without_raw_events  # type: ignore[method-assign]
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "assistant-output"
    assert outcome.raw_events == ()


async def test_runtime_threads_preserve_wait_exception_detail(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()

    async def _raising_wait(
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = timeout
        harness.wait_calls.append((workspace_root, conversation_id, turn_id))
        raise RuntimeError("transport disconnected")

    harness.wait_for_turn = _raising_wait  # type: ignore[method-assign]
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "error"
    assert outcome.error == "transport disconnected"
    assert outcome.raw_events == ()


async def test_runtime_threads_prefer_final_output_over_post_completion_errors(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()

    async def _wait_with_final_output_and_error(
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = timeout
        harness.wait_calls.append((workspace_root, conversation_id, turn_id))
        return SimpleNamespace(
            status="completed",
            assistant_text="assistant-output",
            errors=["transport disconnected after completion"],
            raw_events=[
                {
                    "message": {
                        "method": "turn/completed",
                        "params": {"status": "completed"},
                    }
                }
            ],
        )

    harness.wait_for_turn = _wait_with_final_output_and_error  # type: ignore[method-assign]
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "assistant-output"
    assert outcome.error is None


async def test_runtime_threads_prefer_final_output_when_session_idle_in_raw_events(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()

    async def _wait_with_idle_completion(
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = timeout
        harness.wait_calls.append((workspace_root, conversation_id, turn_id))
        return SimpleNamespace(
            status="completed",
            assistant_text="assistant-output",
            errors=["transport disconnected after completion"],
            raw_events=[
                {
                    "message": {
                        "method": "session.idle",
                        "params": {"sessionID": "s1"},
                    }
                }
            ],
        )

    harness.wait_for_turn = _wait_with_idle_completion  # type: ignore[method-assign]
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "assistant-output"
    assert outcome.error is None


async def test_runtime_threads_recovers_from_top_level_completion_raw_event(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()

    async def _wait_with_top_level_completion(
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = timeout
        harness.wait_calls.append((workspace_root, conversation_id, turn_id))
        return SimpleNamespace(
            status="completed",
            assistant_text="assistant-output",
            errors=["transport disconnected after completion"],
            raw_events=[
                {"method": "turn/completed", "params": {"status": "completed"}}
            ],
        )

    harness.wait_for_turn = _wait_with_top_level_completion  # type: ignore[method-assign]
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "assistant-output"
    assert outcome.error is None


async def test_runtime_threads_preserve_assistant_text_when_secondary_errors_opencode_style(
    tmp_path: Path,
) -> None:
    """OpenCode can set output.error while still returning assistant text; chat must see text."""
    harness = _HarnessWithWait()

    async def _wait_with_text_and_errors(
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = timeout
        harness.wait_calls.append((workspace_root, conversation_id, turn_id))
        return SimpleNamespace(
            status="error",
            assistant_text="hello from model",
            errors=["secondary transport detail"],
            raw_events=[
                {"message": {"method": "session.idle", "params": {"sessionID": "s1"}}}
            ],
        )

    harness.wait_for_turn = _wait_with_text_and_errors  # type: ignore[method-assign]
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "hello from model"
    assert outcome.error == "secondary transport detail"


async def test_runtime_threads_recovers_from_acp_prompt_completion_raw_event(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()

    async def _wait_with_prompt_completion(
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = timeout
        harness.wait_calls.append((workspace_root, conversation_id, turn_id))
        return SimpleNamespace(
            status="completed",
            assistant_text="assistant-output",
            errors=["transport disconnected after completion"],
            raw_events=[
                {"method": "prompt/completed", "params": {"status": "completed"}}
            ],
        )

    harness.wait_for_turn = _wait_with_prompt_completion  # type: ignore[method-assign]
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "assistant-output"
    assert outcome.error is None


async def test_runtime_threads_recovers_from_session_status_idle_state_field(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()

    async def _wait_with_session_status_state(
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = timeout
        harness.wait_calls.append((workspace_root, conversation_id, turn_id))
        return SimpleNamespace(
            status="completed",
            assistant_text="assistant-output",
            errors=["transport disconnected after completion"],
            raw_events=[
                {
                    "method": "session.status",
                    "params": {"status": {"state": "idle"}},
                }
            ],
        )

    harness.wait_for_turn = _wait_with_session_status_state  # type: ignore[method-assign]
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "assistant-output"
    assert outcome.error is None


async def test_runtime_threads_recovers_from_session_status_idle_in_properties(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()

    async def _wait_with_session_status_properties(
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = timeout
        harness.wait_calls.append((workspace_root, conversation_id, turn_id))
        return SimpleNamespace(
            status="completed",
            assistant_text="assistant-output",
            errors=["transport disconnected after completion"],
            raw_events=[
                {
                    "method": "session.status",
                    "params": {"properties": {"status": {"type": "idle"}}},
                }
            ],
        )

    harness.wait_for_turn = _wait_with_session_status_properties  # type: ignore[method-assign]
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "assistant-output"
    assert outcome.error is None


async def test_stream_runtime_thread_events_proxies_harness_stream(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithStream()
    service = _build_service(tmp_path, harness, agent_id="opencode", name="OpenCode")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("opencode", workspace_root)
    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="hello world",
        ),
    )

    events = [event async for event in stream_runtime_thread_events(started)]

    assert len(events) == 3
    assert "message.delta" in events[0]


async def test_runtime_thread_stream_terminal_event_finishes_before_wait_return(
    tmp_path: Path,
) -> None:
    @dataclass
    class _HarnessWithTerminalStream(_HarnessWithBlockingWait):
        capabilities: frozenset[str] = frozenset(
            ["durable_threads", "message_turns", "interrupt", "event_streaming"]
        )

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield {"message": {"method": "message.delta", "params": {"delta": "done"}}}
            yield {
                "message": {
                    "method": "turn/completed",
                    "params": {"status": "completed"},
                }
            }
            await asyncio.Future()

    harness = _HarnessWithTerminalStream()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await asyncio.wait_for(
        await_runtime_thread_outcome(
            started,
            interrupt_event=None,
            timeout_seconds=5,
            execution_error_message="Managed thread execution failed",
        ),
        timeout=1,
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "done"
    assert outcome.completion_source == "stream_terminal_event"
    assert outcome.transport_request_return_timestamp is None
    assert outcome.terminal_signals
    assert outcome.terminal_signals[0].source == "turn/completed"
    assert harness.interrupt_calls == []
    assert harness.wait_cancelled.is_set()


async def test_runtime_thread_stream_terminal_event_uses_final_output_payload(
    tmp_path: Path,
) -> None:
    @dataclass
    class _HarnessWithIdleTerminalStream(_HarnessWithBlockingWait):
        capabilities: frozenset[str] = frozenset(
            ["durable_threads", "message_turns", "interrupt", "event_streaming"]
        )

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield {
                "message": {
                    "method": "session.idle",
                    "params": {"sessionId": "session-1", "finalOutput": "done"},
                }
            }
            await asyncio.Future()

    harness = _HarnessWithIdleTerminalStream()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await asyncio.wait_for(
        await_runtime_thread_outcome(
            started,
            interrupt_event=None,
            timeout_seconds=5,
            execution_error_message="Managed thread execution failed",
        ),
        timeout=1,
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "done"
    assert outcome.completion_source == "stream_terminal_event"
    assert outcome.transport_request_return_timestamp is None
    assert harness.wait_cancelled.is_set()


async def test_runtime_thread_prompt_cancelled_terminal_event_finishes_before_wait_return(
    tmp_path: Path,
) -> None:
    @dataclass
    class _HarnessWithPromptCancelledStream(_HarnessWithBlockingWait):
        capabilities: frozenset[str] = frozenset(
            ["durable_threads", "message_turns", "interrupt", "event_streaming"]
        )

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield {
                "message": {"method": "prompt/delta", "params": {"delta": "partial"}}
            }
            yield {
                "message": {
                    "method": "prompt/cancelled",
                    "params": {
                        "status": "cancelled",
                        "message": "request cancelled",
                    },
                }
            }
            await asyncio.Future()

    harness = _HarnessWithPromptCancelledStream()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await asyncio.wait_for(
        await_runtime_thread_outcome(
            started,
            interrupt_event=None,
            timeout_seconds=5,
            execution_error_message="Managed thread execution failed",
        ),
        timeout=1,
    )

    assert outcome.status == "interrupted"
    assert outcome.assistant_text == ""
    assert outcome.error == "request cancelled"
    assert outcome.completion_source == "stream_terminal_event"
    assert outcome.transport_request_return_timestamp is None
    assert outcome.terminal_signals
    assert outcome.terminal_signals[0].source == "prompt/cancelled"
    assert harness.interrupt_calls == []
    assert harness.wait_cancelled.is_set()


async def test_runtime_thread_turn_cancelled_terminal_event_finishes_before_wait_return(
    tmp_path: Path,
) -> None:
    @dataclass
    class _HarnessWithTurnCancelledStream(_HarnessWithBlockingWait):
        capabilities: frozenset[str] = frozenset(
            ["durable_threads", "message_turns", "interrupt", "event_streaming"]
        )

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield {
                "message": {"method": "turn/progress", "params": {"delta": "partial"}}
            }
            yield {
                "message": {
                    "method": "turn/cancelled",
                    "params": {"status": "cancelled"},
                }
            }
            await asyncio.Future()

    harness = _HarnessWithTurnCancelledStream()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await asyncio.wait_for(
        await_runtime_thread_outcome(
            started,
            interrupt_event=None,
            timeout_seconds=5,
            execution_error_message="Managed thread execution failed",
        ),
        timeout=1,
    )

    assert outcome.status == "interrupted"
    assert outcome.assistant_text == ""
    assert outcome.error == "Runtime thread interrupted"
    assert outcome.completion_source == "stream_terminal_event"
    assert outcome.transport_request_return_timestamp is None
    assert outcome.terminal_signals
    assert outcome.terminal_signals[0].source == "turn/cancelled"
    assert harness.interrupt_calls == []
    assert harness.wait_cancelled.is_set()


async def test_runtime_thread_prompt_failed_terminal_event_finishes_before_wait_return(
    tmp_path: Path,
) -> None:
    @dataclass
    class _HarnessWithPromptFailedStream(_HarnessWithBlockingWait):
        capabilities: frozenset[str] = frozenset(
            ["durable_threads", "message_turns", "interrupt", "event_streaming"]
        )

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield {
                "message": {"method": "prompt/delta", "params": {"delta": "partial"}}
            }
            yield {
                "message": {
                    "method": "prompt/failed",
                    "params": {
                        "status": "failed",
                        "message": "permission denied",
                    },
                }
            }
            await asyncio.Future()

    harness = _HarnessWithPromptFailedStream()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await asyncio.wait_for(
        await_runtime_thread_outcome(
            started,
            interrupt_event=None,
            timeout_seconds=5,
            execution_error_message="Managed thread execution failed",
        ),
        timeout=1,
    )

    assert outcome.status == "error"
    assert outcome.assistant_text == ""
    assert outcome.error == "permission denied"
    assert outcome.failure_cause == "permission denied"
    assert outcome.completion_source == "stream_terminal_event"
    assert outcome.transport_request_return_timestamp is None
    assert outcome.terminal_signals
    assert outcome.terminal_signals[0].source == "prompt/failed"
    assert harness.interrupt_calls == []
    assert harness.wait_cancelled.is_set()


async def test_runtime_thread_prompt_completed_without_final_payload_uses_prior_output_delta(
    tmp_path: Path,
) -> None:
    @dataclass
    class _HarnessWithPromptCompletedDeltaOnly(_HarnessWithBlockingWait):
        capabilities: frozenset[str] = frozenset(
            ["durable_threads", "message_turns", "interrupt", "event_streaming"]
        )

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield {
                "message": {
                    "method": "prompt/delta",
                    "params": {"delta": "delta-only completion"},
                }
            }
            yield {
                "message": {
                    "method": "prompt/completed",
                    "params": {"status": "completed"},
                }
            }
            await asyncio.Future()

    harness = _HarnessWithPromptCompletedDeltaOnly()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await asyncio.wait_for(
        await_runtime_thread_outcome(
            started,
            interrupt_event=None,
            timeout_seconds=5,
            execution_error_message="Managed thread execution failed",
        ),
        timeout=1,
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "delta-only completion"
    assert outcome.error is None
    assert outcome.completion_source == "stream_terminal_event"
    assert outcome.transport_request_return_timestamp is None
    assert outcome.terminal_signals
    assert outcome.terminal_signals[0].source == "prompt/completed"
    assert harness.interrupt_calls == []
    assert harness.wait_cancelled.is_set()


async def test_runtime_thread_session_status_idle_without_request_return_uses_prior_output_delta(
    tmp_path: Path,
) -> None:
    @dataclass
    class _HarnessWithSessionStatusIdle(_HarnessWithBlockingWait):
        capabilities: frozenset[str] = frozenset(
            ["durable_threads", "message_turns", "interrupt", "event_streaming"]
        )

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield {
                "message": {
                    "method": "prompt/delta",
                    "params": {"delta": "idle completion"},
                }
            }
            yield {
                "message": {
                    "method": "session.status",
                    "params": {"status": {"type": "idle"}},
                }
            }
            await asyncio.Future()

    harness = _HarnessWithSessionStatusIdle()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await asyncio.wait_for(
        await_runtime_thread_outcome(
            started,
            interrupt_event=None,
            timeout_seconds=5,
            execution_error_message="Managed thread execution failed",
        ),
        timeout=1,
    )

    assert outcome.status == "ok"
    assert outcome.assistant_text == "idle completion"
    assert outcome.error is None
    assert outcome.completion_source == "stream_terminal_event"
    assert outcome.transport_request_return_timestamp is None
    assert outcome.terminal_signals
    assert outcome.terminal_signals[0].source == "session.status"
    assert harness.interrupt_calls == []
    assert harness.wait_cancelled.is_set()


async def test_runtime_threads_prompt_return_tracks_completion_metadata(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithWait()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome = await await_runtime_thread_outcome(
        started,
        interrupt_event=None,
        timeout_seconds=5,
        execution_error_message="Managed thread execution failed",
    )

    assert outcome.status == "ok"
    assert outcome.completion_source == "prompt_return"
    assert outcome.transport_request_return_timestamp is not None
    assert outcome.last_progress_timestamp is None
    assert outcome.terminal_signals == ()


async def test_runtime_thread_timeout_cancels_wait_collector(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithBlockingWait()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )
    outcome_task = asyncio.create_task(
        await_runtime_thread_outcome(
            started,
            interrupt_event=asyncio.Event(),
            timeout_seconds=0.01,
            execution_error_message="Managed thread execution failed",
        )
    )
    await harness.wait_started.wait()
    outcome = await outcome_task

    assert outcome.status == "error"
    assert outcome.error == "Runtime thread timed out"
    assert outcome.completion_source == "timeout"
    assert outcome.failure_cause == "Runtime thread timed out"
    assert outcome.terminal_signals[-1].source == "timeout"
    assert harness.interrupt_calls == [
        (workspace_root, "backend-thread-1", "backend-turn-1")
    ]
    assert harness.wait_cancelled.is_set()


async def test_runtime_thread_interrupt_event_can_be_bound_to_foreign_loop(
    tmp_path: Path,
) -> None:
    harness = _HarnessWithBlockingWait()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    started = await begin_runtime_thread_execution(
        service,
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="user-visible prompt",
        ),
    )

    ready = threading.Event()
    foreign_state: dict[str, Any] = {}

    def _run_foreign_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        interrupt_event = asyncio.Event()

        async def _bind_event() -> None:
            waiter = asyncio.create_task(interrupt_event.wait())
            await asyncio.sleep(0)
            waiter.cancel()
            try:
                await waiter
            except asyncio.CancelledError:
                pass

        loop.run_until_complete(_bind_event())
        foreign_state["loop"] = loop
        foreign_state["interrupt_event"] = interrupt_event
        ready.set()
        loop.run_forever()
        loop.close()

    foreign_thread = threading.Thread(target=_run_foreign_loop, daemon=True)
    foreign_thread.start()
    assert ready.wait(timeout=1)
    foreign_loop = foreign_state["loop"]
    interrupt_event = foreign_state["interrupt_event"]

    try:
        outcome_task = asyncio.create_task(
            await_runtime_thread_outcome(
                started,
                interrupt_event=interrupt_event,
                timeout_seconds=5,
                execution_error_message="Managed thread execution failed",
            )
        )
        await harness.wait_started.wait()
        foreign_loop.call_soon_threadsafe(interrupt_event.set)
        outcome = await asyncio.wait_for(outcome_task, timeout=1)
    finally:
        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        foreign_thread.join(timeout=1)

    assert outcome.status == "interrupted"
    assert outcome.error == "Runtime thread interrupted"
    assert harness.interrupt_calls == [
        (workspace_root, "backend-thread-1", "backend-turn-1")
    ]
    assert harness.wait_cancelled.is_set()
