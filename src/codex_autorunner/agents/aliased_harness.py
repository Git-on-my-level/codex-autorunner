from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

from .base import AgentHarness
from .types import (
    AgentId,
    ConversationRef,
    ModelCatalog,
    RuntimeCapabilityReport,
    TerminalTurnResult,
    TranscriptEntry,
    TurnRef,
)


class AliasedAgentHarness(AgentHarness):
    """Wrap a backend harness with a public CAR agent identity."""

    def __init__(
        self,
        harness: AgentHarness,
        *,
        agent_id: str,
        display_name: str,
    ) -> None:
        self._harness = harness
        self.agent_id = AgentId(agent_id)
        self.display_name = display_name
        self.capabilities = harness.capabilities

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._harness.ensure_ready(workspace_root)

    async def runtime_capability_report(
        self, workspace_root: Path
    ) -> RuntimeCapabilityReport:
        return await self._harness.runtime_capability_report(workspace_root)

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        return await self._harness.model_catalog(workspace_root)

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        conversation = await self._harness.new_conversation(workspace_root, title=title)
        return ConversationRef(agent=self.agent_id, id=conversation.id)

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        conversations = await self._harness.list_conversations(workspace_root)
        return [
            ConversationRef(agent=self.agent_id, id=conv.id) for conv in conversations
        ]

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        conversation = await self._harness.resume_conversation(
            workspace_root, conversation_id
        )
        return ConversationRef(agent=self.agent_id, id=conversation.id)

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
        return await self._harness.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
            input_items=input_items,
        )

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
        return await self._harness.start_review(
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        return await self._harness.wait_for_turn(
            workspace_root,
            conversation_id,
            turn_id,
            timeout=timeout,
        )

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        await self._harness.interrupt(workspace_root, conversation_id, turn_id)

    async def transcript_history(
        self,
        workspace_root: Path,
        conversation_id: str,
        *,
        limit: Optional[int] = None,
    ) -> list[TranscriptEntry]:
        return await self._harness.transcript_history(
            workspace_root,
            conversation_id,
            limit=limit,
        )

    def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[Any]:
        return self._harness.stream_events(workspace_root, conversation_id, turn_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._harness, name)
