from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.agents.acp import ACPMissingSessionError
from codex_autorunner.agents.hermes.harness import HermesHarness
from codex_autorunner.agents.registry import get_registered_agents
from codex_autorunner.agents.types import TerminalTurnResult
from codex_autorunner.core.orchestration import FreshConversationRequiredError


class _StubSupervisor:
    def __init__(self) -> None:
        self.created: list[tuple[Path, str | None]] = []
        self.resumed: list[tuple[Path, str]] = []
        self.started: list[tuple[Path, str, str, str | None, str | None]] = []
        self.waited: list[tuple[Path, str, str, float | None]] = []
        self.interrupted: list[tuple[Path, str, str | None]] = []
        self.streamed: list[tuple[Path, str, str]] = []
        self.ready_workspace: Path | None = None
        self.snapshot_turn_ids: list[str] = []
        self.resume_error: Exception | None = None

    async def ensure_ready(self, workspace_root: Path) -> None:
        self.ready_workspace = workspace_root

    async def create_session(
        self,
        workspace_root: Path,
        *,
        title: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ):
        _ = metadata
        self.created.append((workspace_root, title))
        return type(
            "Session",
            (),
            {
                "session_id": "hermes-session-1",
                "title": title,
                "summary": "fresh conversation",
                "raw": {"summary": "fresh conversation"},
            },
        )()

    async def resume_session(self, workspace_root: Path, session_id: str):
        self.resumed.append((workspace_root, session_id))
        if self.resume_error is not None:
            raise self.resume_error
        return type(
            "Session",
            (),
            {
                "session_id": session_id,
                "title": "Resumed Hermes session",
                "summary": "resumed summary",
                "raw": {"summary": "resumed summary"},
            },
        )()

    async def list_sessions(self, workspace_root: Path):
        _ = workspace_root
        return [
            type(
                "Session",
                (),
                {
                    "session_id": "hermes-session-1",
                    "title": "First Hermes session",
                    "summary": "first summary",
                    "raw": {"summary": "first summary"},
                },
            )(),
            type(
                "Session",
                (),
                {
                    "session_id": "hermes-session-2",
                    "title": "Second Hermes session",
                    "raw": {"subtitle": "second summary"},
                },
            )(),
        ]

    async def start_turn(
        self,
        workspace_root: Path,
        session_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        approval_mode: Optional[str] = None,
    ) -> str:
        self.started.append((workspace_root, session_id, prompt, model, approval_mode))
        return "hermes-turn-1"

    async def wait_for_turn(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
        *,
        timeout: float | None = None,
    ) -> TerminalTurnResult:
        self.waited.append((workspace_root, session_id, turn_id, timeout))
        return TerminalTurnResult(
            status="completed",
            assistant_text="Hermes reply",
            raw_events=[
                {"method": "prompt/completed", "params": {"status": "completed"}}
            ],
        )

    async def interrupt_turn(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str | None,
    ) -> None:
        self.interrupted.append((workspace_root, session_id, turn_id))

    async def stream_turn_events(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
    ):
        self.streamed.append((workspace_root, session_id, turn_id))
        yield {
            "method": "prompt/progress",
            "params": {
                "sessionId": session_id,
                "turnId": turn_id,
                "delta": "partial",
            },
        }

    async def list_turn_events_snapshot(self, turn_id: str) -> list[dict[str, Any]]:
        self.snapshot_turn_ids.append(turn_id)
        return [{"method": "snapshot"}]


@pytest.mark.asyncio
async def test_hermes_harness_list_progress_events_delegates_to_supervisor_snapshot() -> (
    None
):
    supervisor = _StubSupervisor()
    harness = HermesHarness(supervisor)
    got = await harness.list_progress_events("session-x", "turn-y")
    assert got == [{"method": "snapshot"}]
    assert supervisor.snapshot_turn_ids == ["turn-y"]


@pytest.mark.asyncio
async def test_hermes_harness_reports_capabilities_from_contract() -> None:
    harness = HermesHarness(_StubSupervisor())

    report = await harness.runtime_capability_report(Path("."))

    assert harness.capabilities == get_registered_agents()["hermes"].capabilities
    assert harness.supports("durable_threads") is True
    assert harness.supports("message_turns") is True
    assert harness.supports("active_thread_discovery") is True
    assert harness.supports("interrupt") is True
    assert harness.supports("event_streaming") is True
    assert harness.supports("approvals") is True
    assert harness.supports("review") is False
    assert harness.supports("model_listing") is False
    assert harness.supports("transcript_history") is False
    assert report.capabilities == harness.capabilities


@pytest.mark.asyncio
async def test_hermes_harness_session_lifecycle_and_model_override() -> None:
    supervisor = _StubSupervisor()
    harness = HermesHarness(supervisor)
    workspace_root = Path("/tmp/hermes-workspace")

    await harness.ensure_ready(workspace_root)
    conversation = await harness.new_conversation(workspace_root, title="Hermes Test")
    resumed = await harness.resume_conversation(workspace_root, conversation.id)
    turn = await harness.start_turn(
        workspace_root,
        resumed.id,
        prompt="hello",
        model="anthropic/claude-opus",
        reasoning="high",
        approval_mode="never",
        sandbox_policy=None,
    )
    terminal = await harness.wait_for_turn(
        workspace_root,
        resumed.id,
        turn.turn_id,
        timeout=9.5,
    )
    streamed = [
        event
        async for event in harness.stream_events(
            workspace_root,
            resumed.id,
            turn.turn_id,
        )
    ]
    await harness.interrupt(workspace_root, resumed.id, turn.turn_id)

    assert supervisor.ready_workspace == workspace_root
    assert conversation.id == "hermes-session-1"
    assert conversation.title == "Hermes Test"
    assert conversation.summary == "fresh conversation"
    assert resumed.id == "hermes-session-1"
    assert resumed.title == "Resumed Hermes session"
    assert resumed.summary == "resumed summary"
    assert turn.turn_id == "hermes-turn-1"
    assert terminal.status == "completed"
    assert terminal.assistant_text == "Hermes reply"
    assert supervisor.created == [(workspace_root, "Hermes Test")]
    assert supervisor.resumed == [(workspace_root, "hermes-session-1")]
    assert supervisor.started == [
        (
            workspace_root,
            "hermes-session-1",
            "hello",
            "anthropic/claude-opus",
            "never",
        )
    ]
    assert supervisor.waited == [
        (workspace_root, "hermes-session-1", "hermes-turn-1", 9.5)
    ]
    assert supervisor.interrupted == [
        (workspace_root, "hermes-session-1", "hermes-turn-1")
    ]
    assert streamed[0]["method"] == "prompt/progress"


@pytest.mark.asyncio
async def test_hermes_harness_list_conversations_preserves_titles_and_summaries() -> (
    None
):
    harness = HermesHarness(_StubSupervisor())

    conversations = await harness.list_conversations(Path("."))

    assert [
        (conversation.id, conversation.title, conversation.summary)
        for conversation in conversations
    ] == [
        ("hermes-session-1", "First Hermes session", "first summary"),
        ("hermes-session-2", "Second Hermes session", "second summary"),
    ]


@pytest.mark.asyncio
async def test_hermes_harness_wait_for_turn_requires_turn_id() -> None:
    harness = HermesHarness(_StubSupervisor())

    with pytest.raises(ValueError, match="requires a turn id"):
        await harness.wait_for_turn(Path("."), "hermes-session-1", None)


@pytest.mark.asyncio
async def test_hermes_harness_maps_missing_session_resume_to_fresh_binding_error() -> (
    None
):
    supervisor = _StubSupervisor()
    supervisor.resume_error = ACPMissingSessionError(
        method="session/load",
        code=-32004,
        message="session not found: hermes-session-1",
    )
    harness = HermesHarness(supervisor)

    with pytest.raises(FreshConversationRequiredError) as exc_info:
        await harness.resume_conversation(Path("."), "hermes-session-1")

    exc = exc_info.value
    assert exc.conversation_id == "hermes-session-1"
    assert exc.operation == "resume_conversation"
    assert exc.status_code == -32004
