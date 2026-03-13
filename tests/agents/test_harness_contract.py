from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.agents.base import AgentHarness, UnsupportedAgentCapabilityError
from codex_autorunner.agents.types import (
    AgentId,
    ConversationRef,
    RuntimeCapability,
    TerminalTurnResult,
    TurnRef,
)


class _MinimalHarness(AgentHarness):
    agent_id = AgentId("minimal")
    display_name = "Minimal"
    capabilities = frozenset(
        [
            RuntimeCapability("durable_threads"),
            RuntimeCapability("message_turns"),
        ]
    )

    async def ensure_ready(self, workspace_root: Path) -> None:
        _ = workspace_root

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        _ = workspace_root, title
        return ConversationRef(agent=self.agent_id, id="conv-1")

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        _ = workspace_root
        return ConversationRef(agent=self.agent_id, id=conversation_id)

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
    ) -> TurnRef:
        _ = (
            workspace_root,
            prompt,
            model,
            reasoning,
            approval_mode,
            sandbox_policy,
        )
        return TurnRef(conversation_id=conversation_id, turn_id="turn-1")

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = workspace_root, conversation_id, turn_id, timeout
        return TerminalTurnResult(status="ok", assistant_text="assistant reply")


@pytest.mark.asyncio
async def test_agent_harness_required_contract_and_capability_report() -> None:
    harness = _MinimalHarness()

    report = await harness.runtime_capability_report(Path("."))
    conversation = await harness.new_conversation(Path("."), title="Test")
    resumed = await harness.resume_conversation(Path("."), conversation.id)
    turn = await harness.start_turn(
        Path("."),
        resumed.id,
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )
    terminal = await harness.wait_for_turn(Path("."), resumed.id, turn.turn_id)

    assert harness.supports("durable_threads") is True
    assert harness.supports("turns") is True
    assert harness.supports("interrupt") is False
    assert report.capabilities == harness.capabilities
    assert resumed.id == "conv-1"
    assert terminal.assistant_text == "assistant reply"


@pytest.mark.asyncio
async def test_agent_harness_optional_helpers_are_capability_gated() -> None:
    harness = _MinimalHarness()

    with pytest.raises(UnsupportedAgentCapabilityError, match="model_listing"):
        await harness.model_catalog(Path("."))
    with pytest.raises(
        UnsupportedAgentCapabilityError, match="active_thread_discovery"
    ):
        await harness.list_conversations(Path("."))
    with pytest.raises(UnsupportedAgentCapabilityError, match="review"):
        await harness.start_review(
            Path("."),
            "conv-1",
            prompt="review",
            model=None,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )
    with pytest.raises(UnsupportedAgentCapabilityError, match="interrupt"):
        await harness.interrupt(Path("."), "conv-1", "turn-1")
    with pytest.raises(UnsupportedAgentCapabilityError, match="transcript_history"):
        await harness.transcript_history(Path("."), "conv-1")
    with pytest.raises(UnsupportedAgentCapabilityError, match="event_streaming"):
        async for _event in harness.stream_events(Path("."), "conv-1", "turn-1"):
            pass
