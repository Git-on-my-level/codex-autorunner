from __future__ import annotations

from pathlib import Path
from typing import Any

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
        self.list_by_turn_calls: list[str] = []

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
        yield {
            "message": {"method": "message.delta", "params": {"text": "hi"}},
        }

    async def list_turn_events_by_turn_id(self, turn_id: str) -> list[dict[str, Any]]:
        self.list_by_turn_calls.append(turn_id)
        return [{"raw_event": "{}", "published_at": "1"}]


@pytest.mark.asyncio
async def test_zeroclaw_harness_list_progress_events_delegates_by_turn_id() -> None:
    supervisor = _StubSupervisor()
    harness = ZeroClawHarness(supervisor)
    got = await harness.list_progress_events("zc-session-1", "zc-turn-9")
    assert got == [{"raw_event": "{}", "published_at": "1"}]
    assert supervisor.list_by_turn_calls == ["zc-turn-9"]


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
async def test_zeroclaw_harness_creates_resumes_and_waits_for_turns(
    tmp_path: Path,
) -> None:
    supervisor = _StubSupervisor()
    harness = ZeroClawHarness(supervisor)
    workspace = tmp_path / "workspace"

    conversation = await harness.new_conversation(workspace, title="Test")
    resumed = await harness.resume_conversation(workspace, conversation.id)
    turn = await harness.start_turn(
        workspace,
        resumed.id,
        prompt="hello",
        model="openrouter/gpt-5",
        reasoning="high",
        approval_mode="never",
        sandbox_policy=None,
    )
    terminal = await harness.wait_for_turn(
        workspace,
        resumed.id,
        turn.turn_id,
    )

    assert conversation.id == "zc-session-1"
    assert resumed.id == "zc-session-1"
    assert turn.turn_id == "zc-turn-1"
    assert terminal.status == "completed"
    assert terminal.assistant_text == "wrapper-backed reply"
    assert supervisor.created == [(workspace, "Test")]
    assert supervisor.attached == [(workspace, "zc-session-1")]
    assert supervisor.started == [
        (workspace, "zc-session-1", "hello", "openrouter/gpt-5")
    ]


@pytest.mark.asyncio
async def test_zeroclaw_harness_lists_conversations_and_streams_events(
    tmp_path: Path,
) -> None:
    supervisor = _StubSupervisor()
    harness = ZeroClawHarness(supervisor)
    workspace = tmp_path / "workspace"

    conversations = await harness.list_conversations(workspace)
    events = [
        event
        async for event in harness.stream_events(
            workspace,
            "zc-session-1",
            "zc-turn-1",
        )
    ]

    assert [conversation.id for conversation in conversations] == [
        "zc-session-1",
        "zc-session-2",
    ]
    assert len(events) == 1
    assert events[0]["message"]["method"] == "message.delta"
