from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.agents.types import TerminalTurnResult
from codex_autorunner.core.orchestration import (
    HarnessBackedOrchestrationService,
    MappingAgentDefinitionCatalog,
    MessageRequest,
    PmaThreadExecutionStore,
)
from codex_autorunner.core.orchestration.runtime_threads import (
    RUNTIME_THREAD_MISSING_BACKEND_IDS_ERROR,
    await_runtime_thread_outcome,
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
