from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.core.orchestration import (
    HarnessBackedOrchestrationService,
    MappingAgentDefinitionCatalog,
    MessageRequest,
    PmaThreadExecutionStore,
)
from codex_autorunner.core.orchestration.runtime_threads import (
    await_runtime_thread_outcome,
    begin_runtime_thread_execution,
    stream_runtime_thread_events,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.sse import format_sse


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
    ensure_ready_calls: list[Path] = field(default_factory=list)
    start_turn_calls: list[dict[str, Any]] = field(default_factory=list)
    interrupt_calls: list[tuple[Path, str, Optional[str]]] = field(default_factory=list)
    wait_calls: list[tuple[Path, str, Optional[str]]] = field(default_factory=list)

    async def ensure_ready(self, workspace_root: Path) -> None:
        self.ensure_ready_calls.append(workspace_root)

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
    ) -> _WaitResult:
        _ = timeout
        self.wait_calls.append((workspace_root, conversation_id, turn_id))
        return _WaitResult(
            agent_messages=["assistant-output"],
            raw_events=[],
            errors=[],
        )

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        if False:
            yield f"{workspace_root}:{conversation_id}:{turn_id}"


@dataclass
class _HarnessWithStream:
    display_name: str = "OpenCode"
    ensure_ready_calls: list[Path] = field(default_factory=list)
    interrupt_calls: list[tuple[Path, str, Optional[str]]] = field(default_factory=list)

    async def ensure_ready(self, workspace_root: Path) -> None:
        self.ensure_ready_calls.append(workspace_root)

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
    ) -> _FakeTurn:
        _ = workspace_root, prompt, model, reasoning, approval_mode, sandbox_policy
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

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        _ = workspace_root, conversation_id, turn_id
        yield format_sse(
            "app-server",
            {"message": {"method": "message.delta", "params": {"delta": "hello "}}},
        )
        yield format_sse(
            "app-server",
            {"message": {"method": "message.delta", "params": {"delta": "world"}}},
        )
        yield format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.completed",
                    "params": {"text": "hello world"},
                }
            },
        )


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


async def test_runtime_threads_collect_streamed_output_when_wait_helper_missing(
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
    assert outcome.assistant_text == "hello world"


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
