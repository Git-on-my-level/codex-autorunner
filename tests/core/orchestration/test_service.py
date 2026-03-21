from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.agents.types import TerminalTurnResult
from codex_autorunner.core.orchestration import (
    FreshConversationRequiredError,
    HarnessBackedOrchestrationService,
    MappingAgentDefinitionCatalog,
    MessageRequest,
    OrchestrationBindingStore,
    PausedFlowTarget,
    PmaThreadExecutionStore,
    SurfaceThreadMessageRequest,
)
from codex_autorunner.core.orchestration.models import FlowTarget
from codex_autorunner.core.orchestration.runtime_bindings import (
    clear_runtime_thread_bindings_for_hub_root,
)
from codex_autorunner.core.orchestration.service import (
    build_harness_backed_orchestration_service,
    build_surface_orchestration_ingress,
    get_surface_orchestration_ingress,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.orchestration.transcript_mirror import TranscriptMirrorStore
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
    capabilities: frozenset[str] = frozenset(
        ["durable_threads", "message_turns", "interrupt", "review"]
    )
    next_conversation_id: str = "backend-conversation-1"
    resumed_conversation_id: Optional[str] = None
    resume_conversation_error: Optional[Exception] = None
    backend_runtime_instance_id_value: Optional[str] = None
    next_turn_id: str = "backend-turn-1"
    ensure_ready_error: Optional[Exception] = None
    start_turn_errors: dict[str, Exception] = field(default_factory=dict)
    start_review_errors: dict[str, Exception] = field(default_factory=dict)
    ensure_ready_calls: list[Path] = field(default_factory=list)
    new_conversation_calls: list[tuple[Path, Optional[str]]] = field(
        default_factory=list
    )
    resume_conversation_calls: list[tuple[Path, str]] = field(default_factory=list)
    start_turn_calls: list[dict[str, Any]] = field(default_factory=list)
    start_review_calls: list[dict[str, Any]] = field(default_factory=list)
    interrupt_calls: list[tuple[Path, str, Optional[str]]] = field(default_factory=list)
    interrupt_error: Optional[Exception] = None

    async def ensure_ready(self, workspace_root: Path) -> None:
        self.ensure_ready_calls.append(workspace_root)
        if self.ensure_ready_error is not None:
            raise self.ensure_ready_error

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> _FakeConversation:
        self.new_conversation_calls.append((workspace_root, title))
        return _FakeConversation(id=self.next_conversation_id)

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> _FakeConversation:
        self.resume_conversation_calls.append((workspace_root, conversation_id))
        if self.resume_conversation_error is not None:
            raise self.resume_conversation_error
        return _FakeConversation(id=self.resumed_conversation_id or conversation_id)

    async def backend_runtime_instance_id(self, workspace_root: Path) -> Optional[str]:
        _ = workspace_root
        return self.backend_runtime_instance_id_value

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
        error = self.start_turn_errors.pop(conversation_id, None)
        if error is not None:
            raise error
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
        error = self.start_review_errors.pop(conversation_id, None)
        if error is not None:
            raise error
        return _FakeTurn(turn_id=self.next_turn_id)

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        self.interrupt_calls.append((workspace_root, conversation_id, turn_id))
        if self.interrupt_error is not None:
            raise self.interrupt_error

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = workspace_root, conversation_id, turn_id, timeout
        return TerminalTurnResult(status="ok", assistant_text="Done")

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
        capabilities=(
            capabilities
            if capabilities is not None
            else frozenset(["threads", "turns", "review", "approvals"])
        ),
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


def test_service_supports_agent_workspace_thread_targets(tmp_path: Path) -> None:
    harness = _FakeHarness()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "runtimes" / "zeroclaw" / "zc-main"
    workspace_root.mkdir(parents=True)

    created = service.create_thread_target(
        "codex",
        workspace_root,
        resource_kind="agent_workspace",
        resource_id="zc-main",
        display_name="Workspace Backlog",
    )
    listed = service.list_thread_targets(
        resource_kind="agent_workspace",
        resource_id="zc-main",
    )

    assert created.resource_kind == "agent_workspace"
    assert created.resource_id == "zc-main"
    assert created.repo_id is None
    assert [thread.thread_target_id for thread in listed] == [created.thread_target_id]


def test_service_persists_thread_context_profile(tmp_path: Path) -> None:
    harness = _FakeHarness()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    created = service.create_thread_target(
        "codex",
        workspace_root,
        repo_id="repo-1",
        context_profile="car_core",
    )

    assert created.context_profile == "car_core"


def test_service_preserves_thread_context_profile_from_metadata(tmp_path: Path) -> None:
    harness = _FakeHarness()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    created = service.create_thread_target(
        "codex",
        workspace_root,
        repo_id="repo-1",
        metadata={"context_profile": "car_core"},
    )

    assert created.context_profile == "car_core"


def test_create_thread_target_supports_durable_zeroclaw_agent_workspace(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(
        capabilities=frozenset(
            [
                "durable_threads",
                "message_turns",
                "active_thread_discovery",
                "event_streaming",
            ]
        )
    )
    descriptors = {
        "zeroclaw": _make_descriptor(
            "zeroclaw",
            name="ZeroClaw",
            capabilities=frozenset(
                [
                    "durable_threads",
                    "message_turns",
                    "active_thread_discovery",
                    "event_streaming",
                ]
            ),
        )
    }
    catalog = MappingAgentDefinitionCatalog(descriptors)
    store = PmaThreadExecutionStore(PmaThreadStore(tmp_path / "hub"))
    service = HarnessBackedOrchestrationService(
        definition_catalog=catalog,
        thread_store=store,
        harness_factory=lambda agent_id: harness,
    )
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    created = service.create_thread_target("zeroclaw", workspace_root)

    assert created.agent_id == "zeroclaw"
    assert created.workspace_root == str(workspace_root)


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
            input_items=[
                {"type": "text", "text": "Ship it"},
                {"type": "localImage", "path": str(tmp_path / "diagram.png")},
            ],
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
    assert harness.start_turn_calls[0]["input_items"] == [
        {"type": "text", "text": "Ship it"},
        {"type": "localImage", "path": str(tmp_path / "diagram.png")},
    ]
    assert execution.request_kind == "message"
    assert execution.status == "running"
    assert execution.backend_id == "backend-turn-1"
    assert refreshed_thread is not None
    assert refreshed_thread.backend_thread_id == "backend-conversation-1"
    assert refreshed_thread.last_execution_id == execution.execution_id
    assert refreshed_thread.last_message_preview == "Ship it"
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
    assert execution.request_kind == "review"
    assert execution.backend_id == "review-turn-1"

    persisted = service.get_execution(thread.thread_target_id, execution.execution_id)
    assert persisted is not None
    assert persisted.request_kind == "review"


async def test_send_message_persists_canonical_resumed_conversation_id(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(resumed_conversation_id="backend-canonical-2")
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target(
        "codex",
        workspace_root,
        backend_thread_id="backend-existing-1",
        display_name="Canonical Thread",
    )

    execution = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="Continue the session",
        )
    )

    refreshed_thread = service.get_thread_target(thread.thread_target_id)

    assert harness.resume_conversation_calls == [(workspace_root, "backend-existing-1")]
    assert harness.start_turn_calls[0]["conversation_id"] == "backend-canonical-2"
    assert execution.backend_id == "backend-turn-1"
    assert refreshed_thread is not None
    assert refreshed_thread.backend_thread_id == "backend-canonical-2"


async def test_send_message_starts_fresh_when_resume_conversation_is_missing(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(resume_conversation_error=RuntimeError("missing thread"))
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target(
        "codex",
        workspace_root,
        backend_thread_id="backend-existing-1",
    )

    execution = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="hello again",
        )
    )

    refreshed_thread = service.get_thread_target(thread.thread_target_id)

    assert execution.status == "running"
    assert harness.resume_conversation_calls == [(workspace_root, "backend-existing-1")]
    assert harness.new_conversation_calls == [(workspace_root, None)]
    assert harness.start_turn_calls[0]["conversation_id"] == "backend-conversation-1"
    assert refreshed_thread is not None
    assert refreshed_thread.backend_thread_id == "backend-conversation-1"


async def test_send_message_starts_fresh_when_backend_runtime_instance_is_stale(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(
        next_conversation_id="backend-fresh-2",
        backend_runtime_instance_id_value="runtime-new",
    )
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target(
        "codex",
        workspace_root,
        backend_thread_id="backend-existing-1",
        metadata={"backend_runtime_instance_id": "runtime-old"},
    )

    execution = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="hello again",
        )
    )

    refreshed_thread = service.get_thread_target(thread.thread_target_id)

    assert execution.status == "running"
    assert harness.resume_conversation_calls == []
    assert harness.new_conversation_calls == [(workspace_root, None)]
    assert harness.start_turn_calls[0]["conversation_id"] == "backend-fresh-2"
    assert refreshed_thread is not None
    assert refreshed_thread.backend_thread_id == "backend-fresh-2"
    assert refreshed_thread.backend_runtime_instance_id == "runtime-new"


async def test_send_message_retries_with_fresh_conversation_when_existing_binding_is_invalid(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _FakeHarness(
        next_conversation_id="backend-fresh-2",
        start_turn_errors={
            "backend-existing-1": FreshConversationRequiredError(
                "stale binding",
                conversation_id="backend-existing-1",
                operation="start_turn",
                status_code=400,
            )
        },
    )
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target(
        "codex",
        workspace_root,
        backend_thread_id="backend-existing-1",
    )

    with caplog.at_level(
        logging.INFO,
        logger="codex_autorunner.core.orchestration.service",
    ):
        execution = await service.send_message(
            MessageRequest(
                target_id=thread.thread_target_id,
                target_kind="thread",
                message_text="hello again",
            )
        )

    refreshed_thread = service.get_thread_target(thread.thread_target_id)
    payloads = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "codex_autorunner.core.orchestration.service"
    ]

    assert execution.status == "running"
    assert harness.resume_conversation_calls == [(workspace_root, "backend-existing-1")]
    assert [call["conversation_id"] for call in harness.start_turn_calls] == [
        "backend-existing-1",
        "backend-fresh-2",
    ]
    assert harness.new_conversation_calls == [(workspace_root, None)]
    assert refreshed_thread is not None
    assert refreshed_thread.backend_thread_id == "backend-fresh-2"
    assert any(
        payload.get("event") == "orchestration.thread.refreshing_backend_binding"
        and payload.get("thread_target_id") == thread.thread_target_id
        and payload.get("execution_id") == execution.execution_id
        and payload.get("backend_thread_id") == "backend-existing-1"
        and payload.get("operation") == "start_turn"
        and payload.get("status_code") == 400
        for payload in payloads
    )


async def test_send_review_retries_with_fresh_conversation_when_existing_binding_is_invalid(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _FakeHarness(
        next_conversation_id="backend-fresh-2",
        next_turn_id="review-turn-2",
        start_review_errors={
            "backend-existing-1": FreshConversationRequiredError(
                "stale binding",
                conversation_id="backend-existing-1",
                operation="start_review",
                status_code=400,
            )
        },
    )
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target(
        "codex",
        workspace_root,
        backend_thread_id="backend-existing-1",
    )

    with caplog.at_level(
        logging.INFO,
        logger="codex_autorunner.core.orchestration.service",
    ):
        execution = await service.send_message(
            MessageRequest(
                target_id=thread.thread_target_id,
                target_kind="thread",
                message_text="review this",
                kind="review",
            )
        )

    refreshed_thread = service.get_thread_target(thread.thread_target_id)
    payloads = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "codex_autorunner.core.orchestration.service"
    ]

    assert execution.status == "running"
    assert harness.resume_conversation_calls == [(workspace_root, "backend-existing-1")]
    assert [call["conversation_id"] for call in harness.start_review_calls] == [
        "backend-existing-1",
        "backend-fresh-2",
    ]
    assert harness.new_conversation_calls == [(workspace_root, None)]
    assert refreshed_thread is not None
    assert refreshed_thread.backend_thread_id == "backend-fresh-2"
    assert execution.backend_id == "review-turn-2"
    assert any(
        payload.get("event") == "orchestration.thread.refreshing_backend_binding"
        and payload.get("thread_target_id") == thread.thread_target_id
        and payload.get("execution_id") == execution.execution_id
        and payload.get("backend_thread_id") == "backend-existing-1"
        and payload.get("operation") == "start_review"
        and payload.get("status_code") == 400
        for payload in payloads
    )


async def test_send_message_rehydrates_from_transcripts_after_runtime_binding_restart(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(next_conversation_id="backend-fresh-2")
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)
    execution = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="first question",
        )
    )
    service.record_execution_result(
        thread.thread_target_id,
        execution.execution_id,
        status="ok",
        assistant_text="first answer",
        transcript_turn_id=execution.execution_id,
    )
    TranscriptMirrorStore(tmp_path / "hub").write_mirror(
        turn_id=execution.execution_id,
        metadata={
            "managed_thread_id": thread.thread_target_id,
            "managed_turn_id": execution.execution_id,
            "agent": "codex",
        },
        user_text="first question",
        assistant_text="first answer",
    )

    clear_runtime_thread_bindings_for_hub_root(tmp_path / "hub")
    harness.new_conversation_calls.clear()
    harness.resume_conversation_calls.clear()
    harness.start_turn_calls.clear()

    restarted_service = _build_service(tmp_path, harness)
    next_execution = await restarted_service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="second question",
        )
    )

    prompt = harness.start_turn_calls[0]["prompt"]
    assert next_execution.status == "running"
    assert harness.resume_conversation_calls == []
    assert harness.new_conversation_calls == [(workspace_root, None)]
    assert "Recovered durable conversation state" in prompt
    assert "first question" in prompt
    assert "first answer" in prompt
    assert "second question" in prompt


async def test_start_next_queued_execution_starts_fresh_after_runtime_binding_restart(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(next_conversation_id="backend-fresh-2")
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    running = await service.send_message(
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
        running.execution_id,
        status="ok",
        assistant_text="done",
    )

    clear_runtime_thread_bindings_for_hub_root(tmp_path / "hub")
    harness.new_conversation_calls.clear()
    harness.resume_conversation_calls.clear()
    harness.start_turn_calls.clear()

    restarted_service = _build_service(tmp_path, harness)
    next_execution = await restarted_service.start_next_queued_execution(
        thread.thread_target_id,
        harness=harness,
    )

    assert queued.status == "queued"
    assert next_execution is not None
    assert next_execution.status == "running"
    assert harness.resume_conversation_calls == []
    assert harness.new_conversation_calls == [(workspace_root, None)]
    assert harness.start_turn_calls[0]["conversation_id"] == "backend-fresh-2"


async def test_send_message_queues_when_thread_is_busy_by_default(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    running = await service.send_message(
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

    assert running.status == "running"
    assert queued.status == "queued"
    assert len(harness.start_turn_calls) == 1
    assert service.get_queue_depth(thread.thread_target_id) == 1
    queued_rows = service.list_queued_executions(thread.thread_target_id)
    refreshed_thread = service.get_thread_target(thread.thread_target_id)
    assert [row.execution_id for row in queued_rows] == [queued.execution_id]
    assert refreshed_thread is not None
    assert refreshed_thread.last_execution_id == queued.execution_id
    assert refreshed_thread.last_message_preview == "second"


async def test_send_review_preserves_request_kind_through_queue_claim_and_result(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(next_turn_id="backend-turn-1")
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    running = await service.send_message(
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
            message_text="review second",
            kind="review",
        )
    )

    assert running.request_kind == "message"
    assert queued.status == "queued"
    assert queued.request_kind == "review"

    claimed = service.claim_next_queued_execution_request(thread.thread_target_id)
    assert claimed is None

    completed = service.record_execution_result(
        thread.thread_target_id,
        running.execution_id,
        status="ok",
        assistant_text="done",
        backend_turn_id="backend-turn-1",
    )
    assert completed.request_kind == "message"

    harness.next_turn_id = "backend-review-2"
    claimed = service.claim_next_queued_execution_request(thread.thread_target_id)
    assert claimed is not None
    (
        claimed_thread,
        claimed_execution,
        claimed_request,
        client_request_id,
        sandbox_policy,
    ) = claimed
    assert claimed_thread.thread_target_id == thread.thread_target_id
    assert claimed_execution.request_kind == "review"
    assert claimed_request.kind == "review"
    assert client_request_id is None
    assert sandbox_policy is None

    started = await service._start_execution(
        claimed_thread,
        claimed_request,
        claimed_execution,
        harness=harness,
        workspace_root=workspace_root,
        sandbox_policy=None,
    )
    assert started.request_kind == "review"
    assert harness.start_review_calls[-1]["conversation_id"] == "backend-conversation-1"

    finalized = service.record_execution_result(
        thread.thread_target_id,
        started.execution_id,
        status="ok",
        assistant_text="reviewed",
        backend_turn_id="backend-review-2",
    )
    assert finalized.request_kind == "review"

    with open_orchestration_sqlite(tmp_path / "hub", durable=False) as conn:
        row = conn.execute(
            """
            SELECT request_kind
              FROM orch_thread_executions
             WHERE execution_id = ?
            """,
            (started.execution_id,),
        ).fetchone()
    assert row is not None
    assert row["request_kind"] == "review"


async def test_send_message_interrupts_busy_thread_when_requested(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(next_turn_id="backend-turn-1")
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
    harness.next_turn_id = "backend-turn-2"
    second = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="second",
            busy_policy="interrupt",
        )
    )

    assert first.status == "running"
    assert harness.interrupt_calls == [
        (workspace_root, "backend-conversation-1", "backend-turn-1")
    ]
    assert len(harness.start_turn_calls) == 2
    assert second.status == "running"
    assert second.backend_id == "backend-turn-2"
    assert service.get_queue_depth(thread.thread_target_id) == 0


async def test_interrupt_thread_uses_harness_and_marks_execution(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
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

    with caplog.at_level(
        logging.INFO,
        logger="codex_autorunner.core.orchestration.service",
    ):
        interrupted = await service.interrupt_thread(thread.thread_target_id)

    payloads = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "codex_autorunner.core.orchestration.service"
    ]

    assert harness.interrupt_calls == [
        (workspace_root, "backend-conversation-1", "backend-turn-1")
    ]
    assert interrupted.status == "interrupted"
    assert [
        payload["event"]
        for payload in payloads
        if payload.get("event", "").startswith("orchestration.thread.interrupt_")
    ] == [
        "orchestration.thread.interrupt_requested",
        "orchestration.thread.interrupt_acknowledged",
        "orchestration.thread.interrupt_recorded",
    ]


async def test_interrupt_thread_marks_execution_without_backend_binding(
    tmp_path: Path,
) -> None:
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

    clear_runtime_thread_bindings_for_hub_root(tmp_path / "hub")

    interrupted = await service.interrupt_thread(thread.thread_target_id)

    assert harness.interrupt_calls == []
    assert interrupted.status == "interrupted"
    assert interrupted.execution_id == execution.execution_id
    refreshed_execution = service.get_execution(
        thread.thread_target_id, execution.execution_id
    )
    assert refreshed_execution is not None
    assert refreshed_execution.status == "interrupted"


async def test_stop_thread_marks_interrupted_when_backend_binding_is_missing(
    tmp_path: Path,
) -> None:
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

    clear_runtime_thread_bindings_for_hub_root(tmp_path / "hub")
    outcome = await service.stop_thread(thread.thread_target_id)

    assert harness.interrupt_calls == []
    assert outcome.interrupted_active is True
    assert outcome.recovered_lost_backend is True
    assert outcome.execution is not None
    assert outcome.execution.execution_id == execution.execution_id
    assert outcome.execution.status == "interrupted"
    assert outcome.execution.error is None
    refreshed_thread = service.get_thread_target(thread.thread_target_id)
    assert refreshed_thread is not None
    assert refreshed_thread.backend_thread_id is None
    assert service.get_running_execution(thread.thread_target_id) is None


async def test_stop_thread_marks_interrupted_when_runtime_instance_is_stale_without_interrupt(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(backend_runtime_instance_id_value="runtime-old")
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target(
        "codex",
        workspace_root,
        metadata={"backend_runtime_instance_id": "runtime-old"},
    )
    execution = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="Need an answer",
        )
    )

    harness.backend_runtime_instance_id_value = "runtime-new"

    outcome = await service.stop_thread(thread.thread_target_id)

    assert harness.interrupt_calls == []
    assert outcome.interrupted_active is True
    assert outcome.recovered_lost_backend is True
    assert outcome.execution is not None
    assert outcome.execution.execution_id == execution.execution_id
    assert outcome.execution.status == "interrupted"
    assert outcome.execution.error is None
    refreshed_thread = service.get_thread_target(thread.thread_target_id)
    assert refreshed_thread is not None
    assert refreshed_thread.backend_thread_id is None
    assert refreshed_thread.backend_runtime_instance_id is None
    assert service.get_running_execution(thread.thread_target_id) is None


async def test_stop_thread_marks_interrupted_when_runtime_binding_is_lost_after_restart(
    tmp_path: Path,
) -> None:
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

    clear_runtime_thread_bindings_for_hub_root(tmp_path / "hub")
    restarted_service = _build_service(tmp_path, harness)
    outcome = await restarted_service.stop_thread(thread.thread_target_id)

    assert harness.interrupt_calls == []
    assert outcome.interrupted_active is True
    assert outcome.recovered_lost_backend is True
    assert outcome.execution is not None
    assert outcome.execution.execution_id == execution.execution_id
    assert outcome.execution.status == "interrupted"
    assert outcome.execution.error is None


async def test_cancel_queued_executions_marks_queued_rows_interrupted(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness()
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    running = await service.send_message(
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

    cancelled = service.cancel_queued_executions(thread.thread_target_id)

    assert running.status == "running"
    assert queued.status == "queued"
    assert cancelled == 1
    cancelled_execution = service.get_execution(
        thread.thread_target_id, queued.execution_id
    )
    assert cancelled_execution is not None
    assert cancelled_execution.status == "interrupted"
    assert service.get_queue_depth(thread.thread_target_id) == 0


async def test_send_review_rejects_when_harness_lacks_review_capability(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(capabilities=frozenset(["durable_threads", "message_turns"]))
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    execution = await service.send_message(
        MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text="Review this",
            kind="review",
        )
    )

    assert execution.status == "error"
    assert execution.error == "Agent 'codex' does not support review mode"


async def test_send_message_records_failed_execution_when_runtime_setup_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _FakeHarness(ensure_ready_error=FileNotFoundError("codex"))
    service = _build_service(tmp_path, harness)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target("codex", workspace_root)

    with caplog.at_level(
        logging.WARNING,
        logger="codex_autorunner.core.orchestration.service",
    ):
        execution = await service.send_message(
            MessageRequest(
                target_id=thread.thread_target_id,
                target_kind="thread",
                message_text="Need an answer",
                metadata={"execution_error_message": "Managed thread execution failed"},
            )
        )

    running = service.get_running_execution(thread.thread_target_id)
    payloads = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "codex_autorunner.core.orchestration.service"
    ]
    failure_event = next(
        payload
        for payload in payloads
        if payload.get("event") == "orchestration.thread.start_failed"
    )

    assert harness.ensure_ready_calls == [workspace_root]
    assert harness.new_conversation_calls == []
    assert execution.status == "error"
    assert execution.error == "Managed thread execution failed"
    assert running is None
    assert failure_event["thread_target_id"] == thread.thread_target_id
    assert failure_event["execution_id"] == execution.execution_id
    assert failure_event["request_kind"] == "message"
    assert failure_event["error"] == "codex"
    assert failure_event["error_type"] == "FileNotFoundError"
    assert failure_event["reported_error"] == "Managed thread execution failed"


async def test_interrupt_thread_rejects_when_harness_lacks_interrupt_capability(
    tmp_path: Path,
) -> None:
    harness = _FakeHarness(capabilities=frozenset(["durable_threads", "message_turns"]))
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

    with pytest.raises(RuntimeError, match="does not support interrupt"):
        await service.interrupt_thread(thread.thread_target_id)


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


async def test_surface_ingress_routes_paused_flow_before_thread(tmp_path: Path) -> None:
    ingress = build_surface_orchestration_ingress()
    request = SurfaceThreadMessageRequest(
        surface_kind="discord",
        workspace_root=tmp_path,
        prompt_text="resume this run",
        agent_id="codex",
    )
    thread_calls: list[str] = []

    async def _resolve_paused_flow(
        _request: SurfaceThreadMessageRequest,
    ) -> PausedFlowTarget:
        return PausedFlowTarget(
            flow_target=FlowTarget(
                flow_target_id="ticket_flow",
                flow_type="ticket_flow",
                display_name="ticket_flow",
                workspace_root=str(tmp_path),
            ),
            run_id="run-1",
            status="paused",
            workspace_root=tmp_path,
        )

    async def _submit_flow_reply(
        _request: SurfaceThreadMessageRequest, flow_target: PausedFlowTarget
    ) -> str:
        return flow_target.run_id

    async def _submit_thread_message(
        _request: SurfaceThreadMessageRequest,
    ) -> str:
        thread_calls.append("thread")
        return "thread"

    result = await ingress.submit_message(
        request,
        resolve_paused_flow_target=_resolve_paused_flow,
        submit_flow_reply=_submit_flow_reply,
        submit_thread_message=_submit_thread_message,
    )

    assert result.route == "flow"
    assert result.flow_result == "run-1"
    assert thread_calls == []
    assert [event.event_type for event in result.events] == [
        "ingress.received",
        "ingress.target_resolved",
        "ingress.flow_resumed",
    ]


def test_get_surface_orchestration_ingress_reuses_owner_instance() -> None:
    class _Owner:
        pass

    owner = _Owner()

    first = get_surface_orchestration_ingress(owner)
    second = get_surface_orchestration_ingress(owner)

    assert first is second


def test_service_exposes_binding_queries_when_binding_store_is_configured(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    service = HarnessBackedOrchestrationService(
        definition_catalog=MappingAgentDefinitionCatalog({"codex": _make_descriptor()}),
        thread_store=PmaThreadExecutionStore(PmaThreadStore(hub_root)),
        harness_factory=lambda _agent_id: _FakeHarness(),
        binding_store=OrchestrationBindingStore(hub_root),
    )
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = service.create_thread_target(
        "codex",
        workspace_root,
        repo_id="repo-1",
        display_name="Bound thread",
    )

    binding = service.upsert_binding(
        surface_kind="telegram",
        surface_key="123:root",
        thread_target_id=thread.thread_target_id,
        agent_id="codex",
        repo_id="repo-1",
    )

    assert binding.thread_target_id == thread.thread_target_id
    assert (
        service.get_active_thread_for_binding(
            surface_kind="telegram",
            surface_key="123:root",
        )
        == thread.thread_target_id
    )
    assert (
        service.get_binding(surface_kind="telegram", surface_key="123:root") is not None
    )
    turn = PmaThreadStore(hub_root).create_turn(thread.thread_target_id, prompt="busy")
    summaries = service.list_active_work_summaries(repo_id="repo-1")
    assert len(summaries) == 1
    assert summaries[0].thread_target_id == thread.thread_target_id
    assert summaries[0].execution_id == turn["managed_turn_id"]
    assert summaries[0].execution_status == "running"
