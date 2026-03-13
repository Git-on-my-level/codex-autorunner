from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.core.orchestration import (
    HarnessBackedOrchestrationService,
    MappingAgentDefinitionCatalog,
    MessageRequest,
    PmaThreadExecutionStore,
)
from codex_autorunner.core.orchestration.service import (
    build_harness_backed_orchestration_service,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore


@dataclass
class _FakeConversation:
    id: str


@dataclass
class _FakeTurn:
    turn_id: str


@dataclass
class _FakeHarness:
    display_name: str = "Codex"
    next_conversation_id: str = "backend-conversation-1"
    next_turn_id: str = "backend-turn-1"
    ensure_ready_calls: list[Path] = field(default_factory=list)
    new_conversation_calls: list[tuple[Path, Optional[str]]] = field(
        default_factory=list
    )
    resume_conversation_calls: list[tuple[Path, str]] = field(default_factory=list)
    start_turn_calls: list[dict[str, Any]] = field(default_factory=list)
    start_review_calls: list[dict[str, Any]] = field(default_factory=list)
    interrupt_calls: list[tuple[Path, str, Optional[str]]] = field(default_factory=list)

    async def ensure_ready(self, workspace_root: Path) -> None:
        self.ensure_ready_calls.append(workspace_root)

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> _FakeConversation:
        self.new_conversation_calls.append((workspace_root, title))
        return _FakeConversation(id=self.next_conversation_id)

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> _FakeConversation:
        self.resume_conversation_calls.append((workspace_root, conversation_id))
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
        return _FakeTurn(turn_id=self.next_turn_id)

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
        self.start_review_calls.append(
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
        return _FakeTurn(turn_id=self.next_turn_id)

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        self.interrupt_calls.append((workspace_root, conversation_id, turn_id))

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        if False:
            yield f"{workspace_root}:{conversation_id}:{turn_id}"


def _make_descriptor(
    agent_id: str = "codex",
    *,
    name: str = "Codex",
    capabilities: Optional[frozenset[str]] = None,
) -> AgentDescriptor:
    return AgentDescriptor(
        id=agent_id,
        name=name,
        capabilities=capabilities
        or frozenset(["threads", "turns", "review", "approvals"]),
        make_harness=lambda _ctx: None,  # type: ignore[return-value]
    )


def _build_service(
    tmp_path: Path, harness: _FakeHarness
) -> HarnessBackedOrchestrationService:
    descriptors = {"codex": _make_descriptor()}
    catalog = MappingAgentDefinitionCatalog(descriptors)
    store = PmaThreadExecutionStore(PmaThreadStore(tmp_path / "hub"))
    return HarnessBackedOrchestrationService(
        definition_catalog=catalog,
        thread_store=store,
        harness_factory=lambda agent_id: harness,
    )


def test_service_lists_definitions_and_resolves_thread_targets(tmp_path: Path) -> None:
    harness = _FakeHarness()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    definitions = service.list_agent_definitions()
    assert [definition.agent_id for definition in definitions] == ["codex"]

    created = service.create_thread_target(
        "codex",
        workspace_root,
        repo_id="repo-1",
        display_name="Backlog",
    )
    resolved = service.resolve_thread_target(
        thread_target_id=created.thread_target_id,
        agent_id="codex",
        workspace_root=workspace_root,
    )

    assert resolved.thread_target_id == created.thread_target_id
    assert resolved.workspace_root == str(workspace_root)
    assert service.get_thread_status(created.thread_target_id) is not None


async def test_send_message_creates_conversation_and_execution(tmp_path: Path) -> None:
    harness = _FakeHarness()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target(
        "codex",
        workspace_root,
        repo_id="repo-1",
        display_name="Backlog",
    )

    execution = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="Ship it",
            model="gpt-5",
            reasoning="medium",
            approval_mode="on-request",
        ),
        client_request_id="client-1",
        sandbox_policy={"mode": "workspace-write"},
    )

    refreshed_thread = service.get_thread_target(thread.thread_target_id)
    running = service.get_running_execution(thread.thread_target_id)

    assert harness.ensure_ready_calls == [workspace_root]
    assert harness.new_conversation_calls == [(workspace_root, "Backlog")]
    assert harness.resume_conversation_calls == []
    assert harness.start_turn_calls[0]["conversation_id"] == "backend-conversation-1"
    assert execution.status == "running"
    assert execution.backend_id == "backend-turn-1"
    assert refreshed_thread is not None
    assert refreshed_thread.backend_thread_id == "backend-conversation-1"
    assert running is not None
    assert running.execution_id == execution.execution_id


async def test_send_review_resumes_existing_backend_thread(tmp_path: Path) -> None:
    harness = _FakeHarness(next_conversation_id="unused", next_turn_id="review-turn-1")
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target(
        "codex",
        workspace_root,
        backend_thread_id="backend-existing-1",
        display_name="Review Thread",
    )

    execution = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="Review this patch",
            kind="review",
        )
    )

    assert harness.new_conversation_calls == []
    assert harness.resume_conversation_calls == [(workspace_root, "backend-existing-1")]
    assert harness.start_review_calls[0]["conversation_id"] == "backend-existing-1"
    assert execution.backend_id == "review-turn-1"


async def test_interrupt_thread_uses_harness_and_marks_execution(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)
    await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="Need an answer",
        )
    )

    interrupted = await service.interrupt_thread(thread.thread_target_id)

    assert harness.interrupt_calls == [
        (workspace_root, "backend-conversation-1", "backend-turn-1")
    ]
    assert interrupted.status == "interrupted"


async def test_record_execution_result_updates_execution_state(tmp_path: Path) -> None:
    harness = _FakeHarness()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)
    execution = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="Need an answer",
        )
    )
    completed = service.record_execution_result(
        thread.thread_target_id,
        execution.execution_id,
        status="ok",
        assistant_text="Done",
        backend_turn_id="backend-turn-1",
    )

    assert completed.status == "ok"
    assert completed.output_text == "Done"


def test_builder_wraps_pma_store_with_default_catalog(tmp_path: Path) -> None:
    harness = _FakeHarness()
    descriptors = {"codex": _make_descriptor()}
    service = build_harness_backed_orchestration_service(
        descriptors=descriptors,
        pma_thread_store=PmaThreadStore(tmp_path / "hub"),
        harness_factory=lambda agent_id: harness,
    )

    assert service.get_agent_definition("codex") is not None
    assert isinstance(service.thread_store, PmaThreadExecutionStore)


async def test_thread_service_rejects_flow_targets(tmp_path: Path) -> None:
    harness = _FakeHarness()
    service = _build_service(tmp_path, harness)

    with pytest.raises(
        ValueError, match="Thread orchestration service only handles thread targets"
    ):
        await service.send_message(
            MessageRequest(
                target_id="ticket-flow",
                target_kind="flow",
                message_text="run flow",
            )
        )
