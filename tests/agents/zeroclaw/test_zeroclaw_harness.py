from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.agents.registry import get_registered_agents
from codex_autorunner.agents.types import TerminalTurnResult
from codex_autorunner.agents.zeroclaw.harness import ZeroClawHarness


class _StubSupervisor:
    def __init__(self) -> None:
        self.created: list[tuple[Path, str | None]] = []
        self.attached: list[tuple[Path, str]] = []
        self.started: list[tuple[Path, str, str, str | None]] = []
        self.waited: list[tuple[Path, str, str, float | None]] = []
        self.streamed: list[tuple[Path, str, str]] = []

    async def create_session(
        self, workspace_root: Path, *, title: str | None = None
    ) -> str:
        self.created.append((workspace_root, title))
        return "zc-session-1"

    async def list_sessions(self, workspace_root: Path) -> list[str]:
        return ["zc-session-1", "zc-session-2"]

    async def attach_session(self, workspace_root: Path, session_id: str) -> str:
        self.attached.append((workspace_root, session_id))
        return session_id

    async def start_turn(
        self,
        workspace_root: Path,
        session_id: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> str:
        self.started.append((workspace_root, session_id, prompt, model))
        return "zc-turn-1"

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
            assistant_text="wrapper-backed reply",
            errors=[],
        )

    async def stream_turn_events(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
    ):
        self.streamed.append((workspace_root, session_id, turn_id))
        yield 'event: zeroclaw\ndata: {"message":{"method":"message.delta","params":{"text":"hi"}}}\n\n'


@pytest.mark.asyncio
async def test_zeroclaw_harness_reports_capabilities_from_contract() -> None:
    harness = ZeroClawHarness(_StubSupervisor())

    report = await harness.runtime_capability_report(Path("."))

    assert harness.capabilities == get_registered_agents()["zeroclaw"].capabilities
    assert harness.supports("durable_threads") is True
    assert harness.supports("message_turns") is True
    assert harness.supports("active_thread_discovery") is True
    assert harness.supports("event_streaming") is True
    assert harness.supports("interrupt") is False
    assert harness.supports("model_listing") is False
    assert report.capabilities == harness.capabilities


@pytest.mark.asyncio
async def test_zeroclaw_harness_creates_resumes_and_waits_for_turns() -> None:
    supervisor = _StubSupervisor()
    harness = ZeroClawHarness(supervisor)

    conversation = await harness.new_conversation(Path("/tmp/workspace"), title="Test")
    resumed = await harness.resume_conversation(Path("/tmp/workspace"), conversation.id)
    turn = await harness.start_turn(
        Path("/tmp/workspace"),
        resumed.id,
        prompt="hello",
        model="openrouter/gpt-5",
        reasoning="high",
        approval_mode="never",
        sandbox_policy=None,
    )
    terminal = await harness.wait_for_turn(
        Path("/tmp/workspace"),
        resumed.id,
        turn.turn_id,
    )

    assert conversation.id == "zc-session-1"
    assert resumed.id == "zc-session-1"
    assert turn.turn_id == "zc-turn-1"
    assert terminal.status == "completed"
    assert terminal.assistant_text == "wrapper-backed reply"
    assert supervisor.created == [(Path("/tmp/workspace"), "Test")]
    assert supervisor.attached == [(Path("/tmp/workspace"), "zc-session-1")]
    assert supervisor.started == [
        (Path("/tmp/workspace"), "zc-session-1", "hello", "openrouter/gpt-5")
    ]


@pytest.mark.asyncio
async def test_zeroclaw_harness_lists_conversations_and_streams_events() -> None:
    supervisor = _StubSupervisor()
    harness = ZeroClawHarness(supervisor)

    conversations = await harness.list_conversations(Path("/tmp/workspace"))
    events = [
        event
        async for event in harness.stream_events(
            Path("/tmp/workspace"),
            "zc-session-1",
            "zc-turn-1",
        )
    ]

    assert [conversation.id for conversation in conversations] == [
        "zc-session-1",
        "zc-session-2",
    ]
    assert len(events) == 1
    assert "message.delta" in events[0]
