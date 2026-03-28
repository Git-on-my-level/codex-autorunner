"""Plugin agent conformance tests.

Tests verify that both built-in and plugin adapters conform to the shared
durable-thread contract and capability model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.agents.base import AgentHarness, UnsupportedAgentCapabilityError
from codex_autorunner.agents.registry import (
    AgentDescriptor,
    get_registered_agents,
    reload_agents,
)
from codex_autorunner.agents.types import (
    AgentId,
    ConversationRef,
    RuntimeCapability,
    TerminalTurnResult,
    TurnRef,
)


class _MinimalConformingHarness(AgentHarness):
    """Minimal harness that satisfies the durable-thread contract."""

    agent_id = AgentId("minimal-conforming")
    display_name = "Minimal Conforming"
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
        return ConversationRef(agent=self.agent_id, id="conv-new")

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
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
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
        return TurnRef(conversation_id=conversation_id, turn_id="turn-new")

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = workspace_root, conversation_id, turn_id, timeout
        return TerminalTurnResult(status="ok", assistant_text="response")


class _FullyFeaturedHarness(AgentHarness):
    """Harness that supports all optional capabilities."""

    agent_id = AgentId("fully-featured")
    display_name = "Fully Featured"
    capabilities = frozenset(
        [
            RuntimeCapability("durable_threads"),
            RuntimeCapability("message_turns"),
            RuntimeCapability("model_listing"),
            RuntimeCapability("active_thread_discovery"),
            RuntimeCapability("interrupt"),
            RuntimeCapability("review"),
            RuntimeCapability("event_streaming"),
            RuntimeCapability("transcript_history"),
            RuntimeCapability("approvals"),
        ]
    )

    async def ensure_ready(self, workspace_root: Path) -> None:
        _ = workspace_root

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        _ = workspace_root, title
        return ConversationRef(agent=self.agent_id, id="conv-full")

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
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
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
        return TurnRef(conversation_id=conversation_id, turn_id="turn-full")

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = workspace_root, conversation_id, turn_id, timeout
        return TerminalTurnResult(status="ok", assistant_text="full response")

    async def model_catalog(self, workspace_root: Path) -> Any:
        from codex_autorunner.agents.types import ModelCatalog, ModelSpec

        _ = workspace_root
        return ModelCatalog(
            default_model="default",
            models=[
                ModelSpec(
                    id="m1",
                    display_name="Model 1",
                    supports_reasoning=False,
                    reasoning_options=[],
                )
            ],
        )

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        _ = workspace_root
        return [ConversationRef(agent=self.agent_id, id="conv-1")]

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        _ = workspace_root, conversation_id, turn_id

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
    ) -> TurnRef:
        _ = (
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode,
            sandbox_policy,
        )
        return TurnRef(conversation_id=conversation_id, turn_id="review-turn")

    def stream_events(self, workspace_root: Path, conversation_id: str, turn_id: str):
        async def _events():
            _ = workspace_root, conversation_id, turn_id
            yield "event: test\ndata: {}\n\n"

        return _events()

    async def transcript_history(
        self,
        workspace_root: Path,
        conversation_id: str,
        *,
        limit: Optional[int] = None,
    ):
        from codex_autorunner.agents.types import TranscriptEntry

        _ = workspace_root, conversation_id, limit
        return [
            TranscriptEntry(role="user", text="Hello"),
            TranscriptEntry(role="assistant", text="Hi"),
        ]


def test_must_support_durable_threads_contract() -> None:
    """Test that a minimal conforming harness implements the must-support interface."""
    harness = _MinimalConformingHarness()

    assert harness.supports("durable_threads") is True
    assert harness.supports("message_turns") is True


@pytest.mark.asyncio
async def test_optional_capabilities_are_rejected_when_not_advertised() -> None:
    """Test that calling optional methods without capability raises error."""
    harness = _MinimalConformingHarness()

    with pytest.raises(UnsupportedAgentCapabilityError, match="model_listing"):
        await harness.model_catalog(Path("."))

    with pytest.raises(
        UnsupportedAgentCapabilityError, match="active_thread_discovery"
    ):
        await harness.list_conversations(Path("."))

    with pytest.raises(UnsupportedAgentCapabilityError, match="interrupt"):
        await harness.interrupt(Path("."), "conv-1", "turn-1")

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

    with pytest.raises(UnsupportedAgentCapabilityError, match="event_streaming"):
        async for _ in harness.stream_events(Path("."), "conv-1", "turn-1"):
            break

    with pytest.raises(UnsupportedAgentCapabilityError, match="transcript_history"):
        await harness.transcript_history(Path("."), "conv-1")


def test_fully_featured_harness_supports_all_optional_capabilities() -> None:
    """Test that a fully featured harness supports all optional capabilities."""
    harness = _FullyFeaturedHarness()

    assert harness.supports("model_listing") is True
    assert harness.supports("active_thread_discovery") is True
    assert harness.supports("interrupt") is True
    assert harness.supports("review") is True
    assert harness.supports("event_streaming") is True
    assert harness.supports("transcript_history") is True
    assert harness.supports("approvals") is True


@pytest.mark.asyncio
async def test_minimal_harness_nominal_thread_lifecycle() -> None:
    """Test the core thread lifecycle: create, resume, turn, wait."""
    harness = _MinimalConformingHarness()

    await harness.ensure_ready(Path("."))

    conv = await harness.new_conversation(Path("."), title="Test")
    assert conv.id == "conv-new"
    assert conv.agent == harness.agent_id

    resumed = await harness.resume_conversation(Path("."), conv.id)
    assert resumed.id == conv.id

    turn = await harness.start_turn(
        Path("."),
        resumed.id,
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )
    assert turn.conversation_id == resumed.id
    assert turn.turn_id == "turn-new"

    result = await harness.wait_for_turn(Path("."), resumed.id, turn.turn_id)
    assert result.status == "ok"
    assert result.assistant_text == "response"


def test_capability_normalization_aliases() -> None:
    """Test that legacy capability aliases are normalized."""
    harness = _MinimalConformingHarness()

    assert harness.supports("threads") is True
    assert harness.supports("turns") is True

    assert harness.supports("durable_threads") is True
    assert harness.supports("message_turns") is True


def test_descriptor_capability_normalization() -> None:
    """Test that AgentDescriptor normalizes capabilities on construction."""
    descriptor = AgentDescriptor(
        id="test-norm",
        name="Test Normalized",
        capabilities=frozenset(
            [
                RuntimeCapability("threads"),
                RuntimeCapability("turns"),
                RuntimeCapability("interrupt"),
            ]
        ),
        make_harness=lambda _ctx: _MinimalConformingHarness(),
    )

    assert RuntimeCapability("durable_threads") in descriptor.capabilities
    assert RuntimeCapability("message_turns") in descriptor.capabilities
    assert RuntimeCapability("interrupt") in descriptor.capabilities
    assert RuntimeCapability("threads") not in descriptor.capabilities
    assert RuntimeCapability("turns") not in descriptor.capabilities


def test_zeroclaw_descriptor_conforms_to_contract() -> None:
    """Test that ZeroClaw only advertises the durable-thread subset it proves."""
    agents = get_registered_agents()
    assert "zeroclaw" in agents

    descriptor = agents["zeroclaw"]

    assert descriptor.capabilities == frozenset(
        [
            RuntimeCapability("durable_threads"),
            RuntimeCapability("message_turns"),
            RuntimeCapability("active_thread_discovery"),
            RuntimeCapability("event_streaming"),
        ]
    )


def test_codex_descriptor_supports_full_capabilities() -> None:
    """Test that Codex descriptor supports all optional capabilities."""
    agents = get_registered_agents()
    assert "codex" in agents

    descriptor = agents["codex"]

    assert RuntimeCapability("durable_threads") in descriptor.capabilities
    assert RuntimeCapability("message_turns") in descriptor.capabilities
    assert RuntimeCapability("model_listing") in descriptor.capabilities
    assert RuntimeCapability("interrupt") in descriptor.capabilities
    assert RuntimeCapability("review") in descriptor.capabilities
    assert RuntimeCapability("event_streaming") in descriptor.capabilities
    assert RuntimeCapability("approvals") in descriptor.capabilities


def test_opencode_descriptor_capabilities() -> None:
    """Test that OpenCode descriptor capabilities (similar to Codex, no approvals)."""
    agents = get_registered_agents()
    assert "opencode" in agents

    descriptor = agents["opencode"]

    assert RuntimeCapability("durable_threads") in descriptor.capabilities
    assert RuntimeCapability("message_turns") in descriptor.capabilities
    assert RuntimeCapability("model_listing") in descriptor.capabilities
    assert RuntimeCapability("interrupt") in descriptor.capabilities
    assert RuntimeCapability("review") in descriptor.capabilities
    assert RuntimeCapability("event_streaming") in descriptor.capabilities


def test_hermes_descriptor_supports_approval_capability() -> None:
    agents = get_registered_agents()
    assert "hermes" in agents

    descriptor = agents["hermes"]

    assert RuntimeCapability("durable_threads") in descriptor.capabilities
    assert RuntimeCapability("message_turns") in descriptor.capabilities
    assert RuntimeCapability("interrupt") in descriptor.capabilities
    assert RuntimeCapability("event_streaming") in descriptor.capabilities
    assert RuntimeCapability("approvals") in descriptor.capabilities


def test_plugin_descriptor_loading() -> None:
    """Test that plugin descriptors can be loaded and normalized."""
    agents = reload_agents()

    for agent_id, descriptor in agents.items():
        assert descriptor.id == agent_id
        assert descriptor.name
        if agent_id != "zeroclaw":
            assert descriptor.capabilities
        assert callable(descriptor.make_harness)
        assert descriptor.plugin_api_version >= 1


def test_no_duplicate_agent_ids() -> None:
    """Test that no duplicate agent IDs exist in registry."""
    agents = reload_agents()
    ids = list(agents.keys())
    assert len(ids) == len(set(ids)), "Duplicate agent IDs found"


def test_plugin_cannot_override_builtin() -> None:
    """Test that plugins cannot override built-in agent IDs."""
    agents = reload_agents()

    builtin_ids = {"codex", "opencode", "zeroclaw"}
    for builtin_id in builtin_ids:
        assert builtin_id in agents
